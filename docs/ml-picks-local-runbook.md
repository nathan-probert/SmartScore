# Running the ML picks locally (backtrack data off Supabase)

> Status: **prepared, not cut over.** Merging `feat/backtrack-local-sqlite`
> changes nothing on its own: the default backend is still Supabase, CI and
> the `ml-picks.yml` workflow run untouched. This document is the
> activation, parity, and cutover sequence.

## Why

The published backtrack family (`Player-Snapshots-backtrack-prod` ~624k
rows, `Team-Stats-backtrack-prod`, `Goalie-Snapshots-backtrack-prod`,
`Player-Game-Raw-Markers-prod`) is ~560 MB JSON-encoded - over the Supabase
free tier's 500 MB database cap - and the daily run re-reads the whole
ledger through PostgREST ~15 times per slate date, so database size AND
egress both scale with it. The data only lived in Supabase because the
daily picks run executed on GitHub Actions runners, which cannot reach this
machine. The raw layer has always been local (`data/raw_nhl.sqlite`, named
the source of truth by the migrations); the Supabase tables are derived
copies.

With the run local, the derived tables live next to the raw store and
Supabase keeps only what the website touches: Picks, Historic-Picks,
Working, Metrics, Player-Snapshots-{env}, user_preferences, auth, and the
Storage bucket that holds the training artifact.

## The two switches

| env var | values | meaning |
|---|---|---|
| `ML_HISTORY_BACKEND` | `supabase` (default) / `sqlite` | where the history reads/writes go |
| `ML_HISTORY_DUAL_WRITE` | `0` (default) / `1` | sqlite writes ALSO mirror to Supabase (parity window only) |

`data/ml_history.sqlite` holds the four local tables
(`ml_history_store.TABLE_MAP` maps the Supabase names to the local ones).
Schema mirrors the migrations verbatim, including the PKs; the one dtype
difference is booleans (`home`) as INTEGER 0/1 - truthiness-identical, and
the parity check compares values.

## Activation

```bash
# 1. one-shot copy from Supabase (~560 MB, minutes; idempotent, re-runnable)
uv run python smartscore/scripts/backtrack/ml_history_store.py --bootstrap

# 2. receipt: row counts + date spans must MATCH
uv run python smartscore/scripts/backtrack/ml_history_store.py --verify

# 3. training cache reads the local store too (same CSV cache afterwards)
ML_HISTORY_BACKEND=sqlite uv run python smartscore/scripts/backtrack/model.py ... --refresh

# 4. install the local scheduler (replaces the ml-picks.yml triggers)
cp ops/systemd/smartscore-ml-picks.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now smartscore-ml-picks.timer
```

The service unit pins `ML_HISTORY_BACKEND=sqlite` and
`ML_HISTORY_DUAL_WRITE=1`, and sources the repo's `.env` (must carry
`ENV`, `SUPABASE_URL`, `SUPABASE_API_KEY`,
`SUPABASE_SERVICE_ROLE_KEY`). The live snapshot read
(`today_live_rows` → `Player-Snapshots-prod`), the Picks stat patch, the
Historic-Picks write, and the artifact fetch from Supabase Storage stay on
Supabase in BOTH backends - those are the website's tables.

## Parity week

With the timer live and dual-write on, every local pass writes SQLite
first and mirrors to Supabase. Meanwhile the GitHub Actions workflow can
still run (it reads Supabase) - compare its published `stat` values against
the local pass's for the same date:

```bash
sqlite3 data/ml_history.sqlite \
  "SELECT player_id, name, date FROM player_snapshots_backtrack WHERE date = '<yesterday>' LIMIT 5"
```

(A small diff script against `Picks-prod` lives in git history if you want
it mechanised; `fidelity_tracker.py` is the pattern.) Accept cutover after
5-7 game days of matching stats. Dual-write mirror failures are LOUD but
never fatal - they leave the Supabase copy behind, which matters only while
the GH Actions path is still alive, and they are the first thing to check
if a parity day disagrees.

## Cutover

1. Disable the GitHub workflow (Actions → `ml-picks.yml` → disable, or
   remove the workflow file in a follow-up PR).
2. Flip `ML_HISTORY_DUAL_WRITE=0` in the service unit
   (`systemctl --user edit smartscore-ml-picks.service` or edit the file +
   `daemon-reload`).
3. Watch one more game day, then retire the Supabase copies:

   ```sql
   DROP TABLE IF EXISTS "Player-Snapshots-backtrack-prod";
   DROP TABLE IF EXISTS "Team-Stats-backtrack-prod";
   DROP TABLE IF EXISTS "Goalie-Snapshots-backtrack-prod";
   DROP TABLE IF EXISTS "Player-Game-Raw-Markers-prod";
   DROP TABLE IF EXISTS "Player-Game-Raw-Markers-dev";
   ```

4. **Ordering matters:** pin/retire the backtrack migration files
   (`20261008`, `20261011` shared-ledger files, `20261015`, `20261024`,
   `20261027`) BEFORE dropping - the standing migrate job re-applies every
   file on every deploy, so an unpinned migration resurrects the tables.
   The `BACKTRACK-PROD-PINNED` guard tests
   (`tests/unit/test_backtrack_deleted_env_tables.py`) are the pattern for
   whatever pin form the cleanup PR takes.
5. Update `docs/backtrack_dev_deleted.md` and the
   `docs/ml-picks-runbook.md` sections that describe the Supabase ledger.

## Rollback

At any point before step 1 of cutover: disable the timer and nothing has
changed - the GH Actions path never stopped working, and dual-write kept
the Supabase copies current.
