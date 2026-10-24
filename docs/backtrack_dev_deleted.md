# dev backtracking snapshot deleted (2026-10-24)

`Player-Snapshots-backtrack-dev` was deleted after verifying
`Player-Snapshots-backtrack-prod` supersedes it. Any script that used to read
the dev backtrack snapshot (language default `ENV=dev` in `config.py`) must be
run with `ENV=prod` / `--env prod` instead. Code reading the deleted table now
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

## What was NOT deleted (and why)

- `Team-Stats-backtrack-dev` (34,300 rows) and `Goalie-Snapshots-backtrack-dev`
  (36,696 rows): their prod counterparts (`Team-Stats-backtrack-prod`,
  `Goalie-Snapshots-backtrack-prod`) are EMPTY (0 rows), so prod does not yet
  supersede dev for those two. They stay until prod publishes exist
  (`local_store --publish-team/--publish-goalies` with `ENV=prod`).

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
