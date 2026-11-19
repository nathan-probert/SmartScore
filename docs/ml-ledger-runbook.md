# The ML ledger: per-season files in Storage, features in a small table

> What this is: the full backtrack history moves out of the Supabase
> DATABASE (it was ~560 MB JSON-encoded against the free plan's 500 MB
> cap) into per-season compressed SQLite files in Supabase STORAGE, and
> the daily picks run reads a small precomputed feature table instead of
> the big tables. Everything runs on servers: one nightly GitHub Actions
> job + the existing kicks. The owner's machine is only for training
> (as always).
>
> Status: **prepared, self-activating.** The default code paths are
> unchanged; the daily run switches to the small table automatically on
> the first nightly publish. Cutover (dropping the big tables) is a
> documented follow-up.

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

## Cutover (a follow-up PR, AFTER a week of clean feature-path runs)

1. Delete the picks job's Stage-2 gap-fill step (the nightly backfill owns
   it now).
2. Pin/retire the backtrack-family migration files BEFORE dropping anything
   - the standing migrate job re-applies every file and would resurrect
   the tables (the `BACKTRACK-PROD-PINNED` guard tests are the pattern).
3. `DROP TABLE` the four `-prod` backtrack tables + the empty
   `Player-Game-Raw-Markers-dev`; keep the `-dev` live-table twins until
   dev deploys are retired too.
4. Remove the full-ledger fallback from `run_daily_picks.py` (the code
   path that reads the dropped tables) in the same PR.

## Rollback

Any time before step 3 of cutover: the fallback path still works against
the intact Supabase tables. Disabling the `nightly` job changes nothing
for the running site.
