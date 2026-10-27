# ML picks runbook (STARTER STUB on main — the full runbook ships with PR #146)

> This file on `main` is a placeholder so the ml-picks workflow starter
> (`.github/workflows/ml-picks.yml`, same PR) has a docs anchor. PR #146
> replaces this stub with the full runbook; merge this starter FIRST.

## 3. First-time setup (pre-merge dispatch gating)

**Pre-merge manual-dispatch gating (until PR #146 merges):** GitHub only
registers workflows that live on the default branch, and no workflow can be
manually dispatched before it is registered. This starter workflow on `main`
is that registration: after merging it, the workflow page
(Actions -> "ML Daily Picks") exists, and its "Run workflow" branch selector
can dispatch the PR branch — GitHub executes the workflow file from the
SELECTED ref, so `main` runs this starter's placeholder step, while
`feat/final-clean` runs the REAL pipeline from that branch's copy of the
file, using the repo secrets the owner set. A run with `dry: true`
assembles the picks and skips the Picks write, so no rows are written.
Merge order: (1) merge this starter PR, (2) set the Supabase/S3 secrets and
test-dispatch `feat/final-clean` from the workflow page, (3) merge PR #146,
which replaces this starter's content with the full scheduled /
repository_dispatch-complete workflow.

The full first-time setup steps (Supabase + S3 secrets, artifact refresh,
first `dry: true` smoke run) ship with PR #146's full runbook §3.
