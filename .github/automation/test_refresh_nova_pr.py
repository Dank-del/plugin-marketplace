import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import refresh_nova_pr as refresh


def entry(name, sha="a" * 40, version="1.0.0"):
    return {
        "name": name,
        "version": version,
        "source": {
            "source": "url",
            "url": "https://example.test/plugin.git",
            "sha": sha,
        },
    }


def pr(head="b" * 40, state="open", mergeable="dirty"):
    return {
        "state": state,
        "mergeable_state": mergeable,
        "head": {
            "sha": head,
            "ref": refresh.BRANCH,
            "repo": {"full_name": refresh.FORK},
        },
    }


def catalog_run(head="b" * 40, status="in_progress", conclusion=None):
    return {
        "head_sha": head,
        "name": "Validate catalog",
        "status": status,
        "conclusion": conclusion,
    }


class GuardTests(unittest.TestCase):
    def test_skip_closed_and_active_validation(self):
        self.assertIsNotNone(refresh.skip_reason(pr(state="closed"), []))
        for status in refresh.ACTIVE:
            with self.subTest(status=status):
                self.assertIsNotNone(
                    refresh.skip_reason(pr(), [catalog_run(status=status)])
                )

    def test_old_head_and_unrelated_checks_do_not_block_refresh(self):
        self.assertIsNone(refresh.skip_reason(pr(), [catalog_run(head="c" * 40)]))
        run = catalog_run()
        run["name"] = "Socket Security"
        self.assertIsNone(refresh.skip_reason(pr(), [run]))

    def test_keep_validated_head_unless_it_cannot_merge(self):
        runs = [catalog_run(status="completed", conclusion="success")]
        self.assertIsNotNone(refresh.skip_reason(pr(mergeable="blocked"), runs))
        for state in ("dirty", "behind", "unknown"):
            self.assertIsNone(refresh.skip_reason(pr(mergeable=state), runs))

    def test_old_success_does_not_hide_a_new_failed_validation(self):
        passed = {**catalog_run(status="completed", conclusion="success"), "id": 1}
        failed = {**catalog_run(status="completed", conclusion="failure"), "id": 2}
        self.assertIsNone(
            refresh.skip_reason(pr(mergeable="blocked"), [failed, passed])
        )

    def test_refuse_an_unexpected_pr_branch(self):
        value = pr()
        value["head"]["ref"] = "someone-elses-work"
        with self.assertRaises(RuntimeError):
            refresh.skip_reason(value, [])

    def test_insert_preserves_upstream_and_keeps_nova_away_from_tail(self):
        base = {"plugins": [entry("octen"), entry("videogen")]}
        own = entry("nova-room", "c" * 40, "0.0.4")
        own["description"] = 'Quoted "text", braces {} and Unicode 订单'
        output = refresh.insert_nova(json.dumps(base, indent=2) + "\n", own)
        parsed = json.loads(output)
        self.assertEqual(parsed["plugins"], [own, *base["plugins"]])
        with self.assertRaises(RuntimeError):
            refresh.insert_nova(output, own)

    def test_scope_rejects_pin_changes_and_changes_to_other_plugins(self):
        own = entry("nova-room", "c" * 40, "0.0.4")
        base = {"plugins": [entry("octen")]}
        catalog = {"plugins": [own, *base["plugins"]]}
        base_index = {"plugins": {"octen": {"sha": "a" * 40}}}
        index = {
            "plugins": {
                **base_index["plugins"],
                "nova-room": {"sha": "c" * 40, "version": "0.0.4"},
            }
        }
        refresh.verify_scope(base, catalog, base_index, index, own)
        bad = copy.deepcopy(index)
        bad["plugins"]["nova-room"]["sha"] = "d" * 40
        with self.assertRaises(RuntimeError):
            refresh.verify_scope(base, catalog, base_index, bad, own)
        bad = copy.deepcopy(index)
        bad["plugins"]["octen"]["sha"] = "d" * 40
        with self.assertRaises(RuntimeError):
            refresh.verify_scope(base, catalog, base_index, bad, own)


GENERATOR = """import json
from pathlib import Path
data=json.loads(Path('.grok-plugin/marketplace.json').read_text())
index={'plugins': {p['name']: {'sha': p['source']['sha'], 'version': p['version']} for p in data['plugins']}}
output=json.dumps(index,indent=2)+'\\n'
path=Path('.grok-plugin/plugin-index.json')
import sys
if '--check' in sys.argv:
    assert path.read_text()==output
else:
    path.write_text(output)
"""


class GitRefreshTests(unittest.TestCase):
    """Real divergent Git branches prove conflict resolution and push behavior."""

    def command(self, root, *args):
        return subprocess.run(
            args, cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def git(self, root, *args):
        return self.command(root, "git", *args)

    def write_catalog(self, root, plugins):
        (root / refresh.CATALOG).write_text(
            json.dumps({"plugins": plugins}, indent=2) + "\n"
        )
        self.command(root, sys.executable, "scripts/generate-plugin-index.py")
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "Update catalog")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.upstream, self.fork, self.writer, self.topic = [
            root / name for name in ("upstream.git", "fork.git", "writer", "topic")
        ]
        self.git(root, "init", "--bare", str(self.upstream))
        self.git(root, "init", "--bare", str(self.fork))
        self.git(root, "init", "-b", "main", str(self.writer))
        self.git(self.writer, "config", "user.name", "Test")
        self.git(self.writer, "config", "user.email", "test@example.test")
        (self.writer / ".grok-plugin").mkdir()
        (self.writer / "scripts").mkdir()
        (self.writer / "scripts/generate-plugin-index.py").write_text(GENERATOR)
        (self.writer / "scripts/validate-catalog.py").write_text(
            "import json\nfrom pathlib import Path\njson.loads(Path('.grok-plugin/marketplace.json').read_text())\n"
        )
        self.base = [entry("octen"), entry("stripe")]
        self.write_catalog(self.writer, self.base)
        for remote in (self.upstream, self.fork):
            self.git(self.writer, "push", str(remote), "main")
        self.git(root, "clone", "-b", "main", str(self.fork), str(self.topic))
        self.git(self.topic, "checkout", "-b", refresh.BRANCH)
        self.git(self.topic, "config", "user.name", "Test")
        self.git(self.topic, "config", "user.email", "test@example.test")
        self.own = entry("nova-room", "c" * 40, "0.0.4")
        self.write_catalog(self.topic, [*self.base, self.own])
        self.git(self.topic, "push", "origin", refresh.BRANCH)
        self.head = self.git(self.topic, "rev-parse", "HEAD")
        self.write_catalog(self.writer, [*self.base, entry("videogen")])
        self.git(self.writer, "push", str(self.upstream), "main")
        self.upstream_head = self.git(self.writer, "rev-parse", "HEAD")

    def test_resolve_competing_tail_additions_and_push_without_losing_pin(self):
        with (
            patch.object(refresh, "UPSTREAM_URL", str(self.upstream)),
            patch.object(refresh, "review_state", return_value=(pr(self.head), [])),
        ):
            result = refresh.refresh(self.topic)
        self.assertIn("Refreshed https://github.com/", result)
        new_head = self.git(self.topic, "rev-parse", "HEAD")
        self.assertNotEqual(new_head, self.head)
        self.git(self.topic, "merge-base", "--is-ancestor", self.head, new_head)
        self.git(
            self.topic, "merge-base", "--is-ancestor", self.upstream_head, new_head
        )
        self.assertEqual(self.git(self.fork, "rev-parse", refresh.BRANCH), new_head)
        changes = set(
            self.git(self.topic, "diff", "--name-only", self.upstream_head).splitlines()
        )
        self.assertEqual(changes, refresh.ALLOWED)
        data = json.loads((self.topic / refresh.CATALOG).read_text())
        self.assertEqual(
            next(p for p in data["plugins"] if p["name"] == "nova-room"), self.own
        )
        with (
            patch.object(refresh, "UPSTREAM_URL", str(self.upstream)),
            patch.object(refresh, "review_state", return_value=(pr(new_head), [])),
        ):
            self.assertIn("no push needed", refresh.refresh(self.topic))

    def test_maintainer_starting_validation_during_build_prevents_push(self):
        states = [(pr(self.head), []), (pr(self.head), [catalog_run(self.head)])]
        with (
            patch.object(refresh, "UPSTREAM_URL", str(self.upstream)),
            patch.object(refresh, "review_state", side_effect=states),
        ):
            self.assertIn("without pushing", refresh.refresh(self.topic))
        self.assertEqual(self.git(self.fork, "rev-parse", refresh.BRANCH), self.head)

    def test_concurrent_author_commit_is_not_overwritten(self):
        states = [(pr(self.head), []), (pr("d" * 40), [])]
        with (
            patch.object(refresh, "UPSTREAM_URL", str(self.upstream)),
            patch.object(refresh, "review_state", side_effect=states),
            self.assertRaisesRegex(RuntimeError, "head changed"),
        ):
            refresh.refresh(self.topic)
        self.assertEqual(self.git(self.fork, "rev-parse", refresh.BRANCH), self.head)


if __name__ == "__main__":
    unittest.main()
