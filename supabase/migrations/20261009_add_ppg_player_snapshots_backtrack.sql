-- ppg: power play goals per game, single season, on Player-Snapshots-backtrack.
--
-- WHY A NEW COLUMN
-- The archive's hppg is a 3-year window (smartscore_info_client's
-- get_hppg(years=3), fed from seasonTotals in the landing payload), which cannot
-- be computed from one season of game logs - it stays null here until a historic
-- pass exists. ppg is the single-season rate: power play goals to date / games to
-- date, strictly before the row's game, 0 on the pre-debut row (matching gpg's
-- convention). It is both a usable feature now and the per-season building block
-- that pass will accumulate into hppg, so it must not overwrite hppg's column.
--
-- Computed by compute_derived in smartscore/scripts/backtrack/local_store.py from
-- player_games.power_play_goals (box score) and shipped by publish().
--
-- Idempotent because .github/workflows/deploy.yml pipes every file in this
-- directory through psql on every deploy with no migration history: a plain ADD
-- COLUMN would fail on the second run.

ALTER TABLE "Player-Snapshots-backtrack-__ENV__"
    ADD COLUMN IF NOT EXISTS "ppg" DOUBLE PRECISION;
