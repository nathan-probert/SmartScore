# dev backtracking snapshots deleted (2026-10-24)

All three dev backtrack tables are now deleted (row-deleted; the Postgres
shells remain until a manual DROP). Any script that used to read a dev
backtrack table (language default `ENV=dev` in `config.py`) must be run with
`ENV=prod` / `--env prod` instead. Code resolving a deleted dev table name now
fails loudly via `smartscore/scripts/backtrack/deleted_env_tables.py`.


## What was verified before deletion (all read-only)

- Row counts: dev 617,310; prod 623,358.
- The delta is exactly the 6,048 rows with `date > 2026-06-14`
  (2026-09-29..2026-10-24, the 2026-27 season dev never received). No publish
  cadence gap inside the dev range.
- Distinct `(date, player_id)` key sets over the FULL dev range
  (2013-10-01..2026-06-14): identical — 0 dev-only keys, 0 prod-only keys in
  the overlap, no duplicate keys on either side, 0 date-level count diffs.
- 2,000 random overlapping keys: full-row value equality across all 37 columns
  (including the 16 backfilled shot/TOI+identity columns) — 0 mismatches
  (floats <= 1e-9 rel), 0 NULL-parity diffs.
- Column lists: dev and prod identical (37 = 37) — no dev-only column remained
  to migrate.

## Team-Stats / Goalie-Snapshots dev deletions (2026-10-24, second pass)

- `Team-Stats-backtrack-prod` was published from the local store
  (`local_store.py --publish-team`, `ENV=prod`, all 14 seasons incl. 2026-27):
  34,636 rows, 2013-10-01..2026-10-24.
- `Goalie-Snapshots-backtrack-prod` was published the same way
  (`--publish-goalies`): 37,044 rows, 2013-10-01..2026-10-22 (goalies carry no
  2026-10-23/24 rows yet because those games had no box scores at publish time
  — un-played, the known partially-filled-today source pattern).
- Random 500-key spot-checks per table (value-compare against freshly computed
  local values, floats <=1e-9 rel / ints+text exact / NULL-parity): 0
  mismatches. Full key-set check: prod is a strict superset of dev — 0
  dev-only keys; prod-only keys are exactly the 336 team / 348 goalie 2026-27
  rows. The Player-table precedent (6,048 extra 2026-27 rows) replicates.
- After verification, `Team-Stats-backtrack-dev` (34,300 rows) and
  `Goalie-Snapshots-backtrack-dev` (36,696 rows) were row-deleted (year-chunked
  PostgREST deletes, per-table backup SHA256 verified first); prod counts were
  re-checked stable (34,636 / 37,044). Pre-delete gzip backups live in
  `data/backups/` with SHA256s in `MANIFEST.md`.

## Remaining manual steps

PostgREST cannot DROP TABLE, so the three empty shells still exist in the
Supabase project. In the dashboard SQL editor:

1. `DROP TABLE "Player-Snapshots-backtrack-dev";`
2. `DROP TABLE "Team-Stats-backtrack-dev";`
3. `DROP TABLE "Goalie-Snapshots-backtrack-dev";`

Then, to also reclaim the disk, run `VACUUM FULL` on the (now smaller)
remaining backtrack prod tables, or pause/restore the project (a brief
outage — scheduled maintenance, not a data path blocker).

## Restore / re-derivation paths

- Backups: `data/backups/backtrack-dev-Player-Snapshots-backtrack-dev.csv.gz`
  (528 MB gz? see MANIFEST.md for row count + SHA256) plus the other two dev
  backtrack tables as separate gzips. Restorable per table by re-upserting the
  CSV batches (`on_conflict=date,player_id`).
- Everything the tables were derived from also lives in
  `data/raw_nhl.sqlite` (692,514 player_games, 13.4M shifts), so a full
  re-derivation is possible with `local_store.py --build --derive --publish`.

## Shrinking the Supabase disk

Postgres only reclaims disk after a VACUUM:
run `VACUUM FULL "Player-Snapshots-backtrack-dev";` (or delete+recreate the
table) in the dashboard SQL editor, or pause/restore the project. Without that,
the freed 617k rows show as dead tuples and the dashboard size will not drop.
Also drop the empty-named table if it should disappear from the dashboard
listing (PostgREST cannot DROP).
