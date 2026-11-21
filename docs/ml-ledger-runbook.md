# The ML ledger: per-season files in Storage, features in a small table

> What this is: the full backtrack history moves out of the Supabase
> DATABASE (it was ~560 MB JSON-encoded against the free plan's 500 MB
> cap) into per-season compressed SQLite files in Supabase STORAGE, and
> the daily picks run reads a small precomputed feature table instead of
> the big tables. Everything runs on servers: one nightly GitHub Actions
> job + the existing kicks. The owner's machine is only for training
> (as always).
>
> Status: **cutover prepared.** PR #155 merged (ledger in Storage, small
> feature table, self-activating daily path); #156/#157/#158 fixed the
> first live runs. THIS branch executes the cutover: migrations retired,
> the run's fallback removed, the picks job's gap-fill step deleted. The
> only remaining manual step is `docs/cutover-drop-tables.sql` (owner-run
> psql) after this PR merges and a final clean slate day confirms the
> features path.

## The quota math (free plan, verified)

| Quota | Limit | This design uses |
|---|---|---|
| Database size | 500 MB | small tables only (website + `ML-Daily-Features`, ~150k rows/season of 20 doubles) |
| File storage | 1 GB, **50 MB max per file** | 14 season files, **4-6.1 MB each** (measured: 71.9 MB total) |
| Egress | 5 GB + 5 GB cached / month | nightly: ~72 MB download (all seasons - career features span them) + ~6 MB upload (only the current season changes) ≈ **2.4 GB/month** |
| Existing bucket usage | - | model artifacts ~7.9 MB (3 × ~2.6 MB joblib) |

## The pieces

| Piece | Role |
|---|---|
| `ml_ledger.py` | the Storage ledger: `seed` (one-time Supabase→bucket copy), `pull` (bucket → materialized working SQLite), `push` (re-split + upload changed files), `verify`. Season split via `season_of` (the COVID-exception rule `run_daily_picks.season_label` delegates to). |
| `ml_history_store.py` | the SQLite schema/upsert layer the ledger files are built on (schema mirrors the migrations; `ML_HISTORY_BACKEND=sqlite` routes the run's history reads at a local store). |
| `publish_daily_features.py` | the nightly job's script: pull → backfill yesterday into the ledger → compute tomorrow's slate features (the run's OWN pipeline: `history_rows` → `shot_block_from_history` → `build_frame` → `add_history_features`, over the ledger) → upsert `ML-Daily-Features-prod` → push. |
| `ML-Daily-Features-{env}` | the small table: one row per likely slate player per date with the 20 history-derived features (14 shot/TOI + prev2/prev3 goals + games_since_goal + days_since_game + club/opp rest days) + club/opp/name (migration `20261118_add_ml_daily_features.sql`). |
| `run_daily_picks.py` | TWO paths, chosen by data: features rows exist → read live snapshot + the small table ONLY; rows absent → the original full-ledger path (the transition fallback, until the first publish lands). |

## Flow

```
nightly (GitHub Actions, 09:30 UTC / 04:30 ET, cron in ml-picks.yml):
  Storage: ml/ledger/<season>.sqlite.gz (14 files, write-once for history)
    -> pull: materialize the working ledger (~183 MB SQLite on the runner)
    -> backfill: yesterday's FINAL boxscores into the ledger
    -> compute: tomorrow's slate features (exact shared pipeline)
    -> publish: ML-Daily-Features-prod upsert (~700 rows)
    -> push: re-split; upload only the season files whose bytes changed

daily kicks (unchanged schedule, ~15/day):
  Player-Snapshots-prod (live) + ML-Daily-Features-prod (small)
    -> build_frame + add_projected_lineup_features + add_rank_features
    -> ensemble -> stat patch on Picks-{env}
```

## What moved where vs the old flow

| Was (Supabase DB) | Now |
|---|---|
| `Player-Snapshots-backtrack-prod` (~629k rows) | `ml/ledger/<season>.sqlite.gz` files |
| `Team-Stats-backtrack-prod` (34,636) | same |
| `Goalie-Snapshots-backtrack-prod` (37,044) | same |
| `Player-Game-Raw-Markers-prod` (24) | same (the markers ride the ledger) |
| daily-run's full-ledger PostgREST reads | the small features table read |
| nightly compute (was inline in the kicks' backfill step) | the `nightly` job |

## Activation

1. Merge (this PR). Nothing changes: no season files exist yet, so the
   daily run keeps using the full-ledger path.
2. Apply the migration (deploy.yml's migrate job does it on merge).
3. Seed the bucket once (one-time, ~10 minutes, ~130 MB of PostgREST
   reads - idempotent, re-runnable):
   ```bash
   uv run python smartscore/scripts/backtrack/ml_ledger.py seed
   uv run python smartscore/scripts/backtrack/ml_ledger.py verify
   ```
4. Run the nightly job once by hand (Actions → ML Daily Picks → nightly →
   run workflow): it publishes the first `ML-Daily-Features` rows.
5. The next kick picks the features path automatically ("features path:"
   in its log). Watch one slate day for parity (stat values vs the
   fallback path's), then let the cron own it.

## Cutover (THIS branch + one manual SQL step)

1. ~~Delete the picks job's Stage-2 gap-fill step~~ — done in this PR.
2. ~~Pin/retire the backtrack-family migration files~~ — done in this PR
   (the 6 backtrack DDL files are deleted; the standing migrate job
   re-applies what remains, which never names a backtrack table - the
   `test_backtrack_migrations_are_retired` guard enforces it).
3. `DROP TABLE` — `docs/cutover-drop-tables.sql` (owner-run psql, after
   this PR merges + one clean slate day). Keeps the `-dev` live-table
   twins until dev deploys retire.
4. ~~Remove the full-ledger fallback from `run_daily_picks.py`~~ — done in
   this PR (the path is gone; a missing nightly publish fails LOUD naming
   the job).

## Known follow-ups (non-blocking)

- The offline top-up tools (`publish_shot_cols.py`, `topup_raw_game_cols.py`,
  `reconstruct.py`) still name the Supabase tables in their write paths -
  they were the one-shot builders of the pre-cutover copies. Post-DROP they
  fail if run; the ledger is maintained by the nightly backfill now.
  Rebuilding them against the working SQLite/ledger is future work.
- The daily kicks' parity with the fallback path was validated live
  (2026-11-21 dry dispatch: full features-path run, 3 picks assembled).

## Rollback

Merging this PR is safe to sit on: nothing breaks until the DROP SQL runs,
and until then the Storage ledger + ML-Daily-Features keep serving the
site. If you must fully revert the design, the ledger's season files
recreate the tables (ml_history_store.SCHEMA + the dumps) - the data is
not lost, only relocated.
