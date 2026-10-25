# Keeping the pending Nova marketplace submission current

The fork's **Keep Nova submission current** workflow runs every 30 minutes and can
also be dispatched manually. It maintains xAI PR #814 on `add-nova-hiring-room`.
The workflow and helper live only on this fork's `main`; they are not shipped in
the plugin or included in the xAI catalog PR.

When upstream `main` advances, the workflow merges it into the submission branch,
resolves catalog/index conflicts by preserving the reviewed Nova entry alongside
the current upstream catalog, regenerates the component index with the upstream
generator, and runs both official validators before pushing. Nova is placed away
from the catalog's append-only tail to reduce collisions with new submissions.

The workflow preserves Nova's existing source SHA and version. It refuses changes
to other catalog entries, unexpected conflicts or an unexpected PR branch. It
uses ordinary fast-forward pushes and checks again for concurrent author updates
before publishing. It never force-pushes, auto-merges, posts comments or changes
the source release.

Queued/running catalog validation prevents a refresh. A successful validation on
a mergeable head is left ready for maintainer review; only a new conflict or an
outdated-branch requirement permits another refresh. The state is checked again
after the lengthy index generation, before any push. Closed/merged submissions
are skipped. No new commit is created when there is nothing to update.

Manual dispatch's `revalidate` option runs generation/checks even if upstream has
not advanced; it keeps the same review and concurrent-update guards. Normal
scheduled runs skip the expensive index rebuild when already current.

The fork's built-in `GITHUB_TOKEN` writes only this fork. Public xAI PR/run metadata
is read through the API. xAI's maintainer approval of external workflows remains
their responsibility. The inherited daily **Bump plugin SHAs** workflow is not
needed on this fork and should stay disabled; xAI runs it in the upstream repo.

Run the local conflict/concurrency tests with:

```sh
python3 -m unittest discover -s .github/automation -p 'test_*.py' -v
```
