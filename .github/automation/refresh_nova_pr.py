"""Maintain our pending xAI submission without changing its reviewed source pin."""

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UPSTREAM = "xai-org/plugin-marketplace"
UPSTREAM_URL = f"https://github.com/{UPSTREAM}.git"
FORK = "Dank-del/plugin-marketplace"
BRANCH = "add-nova-hiring-room"
PR = 814
BASE_REF = "refs/remotes/nova-upstream/main"
CATALOG = ".grok-plugin/marketplace.json"
INDEX = ".grok-plugin/plugin-index.json"
ALLOWED = {CATALOG, INDEX}
ACTIVE = {"queued", "in_progress", "requested", "waiting", "pending"}


def api(path):
    # Read only public metadata; the built-in token also avoids anonymous limits.
    request = Request(
        "https://api.github.com/" + path,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "nova-marketplace-pr-refresh",
        },
    )
    if token := os.environ.get("GH_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as error:
        if not token or error.code not in (403, 404):
            raise
        # Installation tokens can be scoped to the fork. Public metadata remains
        # readable without adding a cross-repository secret.
        request.remove_header("Authorization")
        response = urlopen(request, timeout=30)
    with response:
        return json.load(response)


def review_state():
    pr = api(f"repos/{UPSTREAM}/pulls/{PR}")
    query = urlencode(
        {"head_sha": pr["head"]["sha"], "event": "pull_request", "per_page": 100}
    )
    runs = api(f"repos/{UPSTREAM}/actions/runs?{query}")["workflow_runs"]
    return pr, runs


def skip_reason(pr, runs):
    if pr["state"] != "open":
        return "Submission is closed or merged; nothing to refresh."
    if pr["head"]["ref"] != BRANCH or pr["head"]["repo"]["full_name"] != FORK:
        raise RuntimeError("The PR no longer points to the expected fork branch.")
    relevant = [
        r
        for r in runs
        if r["head_sha"] == pr["head"]["sha"] and r["name"] == "Validate catalog"
    ]
    if any(r["status"] in ACTIVE for r in relevant):
        return (
            "Catalog validation is queued or running; preserve the head under review."
        )
    latest = max(
        relevant,
        key=lambda r: (
            r.get("created_at", ""),
            r.get("id", 0),
            r.get("run_attempt", 0),
        ),
        default={},
    )
    if latest.get("conclusion") == "success" and pr.get("mergeable_state") not in {
        "dirty",
        "behind",
        "unknown",
        None,
    }:
        return (
            "Catalog validation passed on a mergeable head; leave it ready for review."
        )
    return None


def run(root, *args, check=True):
    result = subprocess.run(
        args, cwd=root, text=True, capture_output=True, timeout=1200, check=False
    )
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr or result.stdout}")
    return result


def git(root, *args):
    return run(root, "git", *args).stdout.strip()


def at(root, ref, path):
    return git(root, "show", f"{ref}:{path}") + "\n"


def insert_nova(base_text, entry):
    base = json.loads(base_text)
    if any(p["name"] == "nova-room" for p in base["plugins"]):
        raise RuntimeError(
            "Nova is already in upstream; do not replace its published entry."
        )
    # Keep Nova away from the append-only tail where new submissions collide.
    records = list(re.finditer(r'(?m)^    \{\n      "name": "([^"\n]+)"', base_text))
    if not records:
        raise RuntimeError("Unrecognized catalog layout; refusing a broad rewrite.")
    anchor = next((m for m in records if m[1] == "octen"), records[len(records) // 2])
    addition = (
        textwrap.indent(json.dumps(entry, indent=2, ensure_ascii=False), "    ") + ",\n"
    )
    return base_text[: anchor.start()] + addition + base_text[anchor.start() :]


def verify_scope(base_catalog, catalog, base_index, index, entry):
    catalog = json.loads(json.dumps(catalog))
    own = [p for p in catalog["plugins"] if p["name"] == "nova-room"]
    catalog["plugins"] = [p for p in catalog["plugins"] if p["name"] != "nova-room"]
    if own != [entry] or catalog != base_catalog:
        raise RuntimeError(
            "Catalog changed outside Nova or changed its reviewed entry."
        )
    index = json.loads(json.dumps(index))
    own_index = index["plugins"].pop("nova-room", None)
    if index != base_index or own_index is None:
        raise RuntimeError("Generated index changed outside Nova.")
    if (
        own_index.get("sha") != entry["source"]["sha"]
        or own_index.get("version") != entry["version"]
    ):
        raise RuntimeError(
            "Generated index does not match the reviewed source pin/version."
        )


def refresh(root, *, revalidate=False):
    pr, runs = review_state()
    reason = skip_reason(pr, runs)
    if reason:
        return reason
    head = pr["head"]["sha"]
    if git(root, "rev-parse", "HEAD") != head or git(root, "status", "--porcelain"):
        raise RuntimeError("Checkout must be the clean, current PR head.")
    current = json.loads((root / CATALOG).read_text())
    own = [p for p in current["plugins"] if p["name"] == "nova-room"]
    if len(own) != 1:
        raise RuntimeError("Expected exactly one reviewed Nova entry.")
    entry = own[0]
    git(root, "fetch", "--no-tags", UPSTREAM_URL, f"main:{BASE_REF}")
    base = git(root, "rev-parse", BASE_REF)
    needs_merge = (
        run(
            root, "git", "merge-base", "--is-ancestor", base, head, check=False
        ).returncode
        != 0
    )
    if not needs_merge and not revalidate:
        return "Submission already contains current upstream main; no push needed."
    base_text = at(root, base, CATALOG)
    base_catalog = json.loads(base_text)
    base_index = json.loads(at(root, base, INDEX))
    if needs_merge:
        merge = run(root, "git", "merge", "--no-commit", "--no-ff", base, check=False)
        if merge.returncode:
            conflicts = set(
                git(root, "diff", "--name-only", "--diff-filter=U").splitlines()
            )
            if not conflicts or not conflicts <= ALLOWED:
                run(root, "git", "merge", "--abort", check=False)
                raise RuntimeError(
                    f"Merge needs manual resolution outside catalog files: {sorted(conflicts)}"
                )
    (root / CATALOG).write_text(insert_nova(base_text, entry))
    (root / INDEX).write_text(at(root, base, INDEX))
    for script, arguments in [
        ("validate-catalog.py", []),
        ("generate-plugin-index.py", []),
        ("generate-plugin-index.py", ["--check"]),
    ]:
        output = run(
            root, sys.executable, f"scripts/{script}", *arguments
        ).stdout.strip()
        if output:
            print(output, flush=True)
    verify_scope(
        base_catalog,
        json.loads((root / CATALOG).read_text()),
        base_index,
        json.loads((root / INDEX).read_text()),
        entry,
    )
    paths = set(git(root, "diff", "--name-only", base).splitlines())
    if not paths <= ALLOWED:
        raise RuntimeError(
            f"PR would change unrelated files: {sorted(paths - ALLOWED)}"
        )
    git(root, "diff", "--check")
    if not needs_merge and not git(root, "diff", "--name-only", "HEAD"):
        return "Catalog is already current and validates; no commit needed."
    # Maintainers may have approved the queued run during the lengthy index build.
    latest, latest_runs = review_state()
    reason = skip_reason(latest, latest_runs)
    if reason:
        return reason + " Discarding local refresh without pushing."
    if latest["head"]["sha"] != head:
        raise RuntimeError(
            "The PR head changed during validation; retry on the next run."
        )
    git(root, "add", CATALOG, INDEX)
    git(root, "commit", "-m", f"Refresh Nova submission against upstream {base[:12]}")
    # Ordinary fast-forward push: a concurrent author update must never be overwritten.
    git(root, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    return f"Refreshed https://github.com/{UPSTREAM}/pull/{PR} at {git(root, 'rev-parse', 'HEAD')}; reviewed source pin unchanged."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path)
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    if args.inspect:
        pr, runs = review_state()
        reason = skip_reason(pr, runs)
        print(reason or "Open submission is eligible for refresh.")
        if output := os.environ.get("GITHUB_OUTPUT"):
            with open(output, "a") as stream:
                stream.write(f"refresh={str(reason is None).lower()}\n")
        return
    if args.worktree is None:
        parser.error("--worktree is required unless --inspect is used")
    root = args.worktree.resolve()
    git(root, "config", "user.name", "github-actions[bot]")
    git(
        root,
        "config",
        "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
    )
    result = refresh(
        root, revalidate=os.environ.get("REVALIDATE", "").lower() == "true"
    )
    print(result)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as output:
            output.write(result + "\n")


if __name__ == "__main__":
    main()
