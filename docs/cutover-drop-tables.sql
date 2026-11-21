-- CUTOVER: drop the backtrack family from the Postgres database.
--
-- Run MANUALLY by the owner (psql / Supabase SQL editor) - PostgREST
-- performs no DDL, and this must happen only AFTER:
--   1. the Storage ledger is seeded and the nightly job has published
--      feature rows for at least a week of clean slate days (the daily
--      run reads ML-Daily-Features and touches nothing here);
--   2. the cutover PR (migrations retired + fallback removed) is merged,
--      so the standing migrate job can never re-create these tables.
--
-- Everything here is reproducible from the Storage ledger
-- (ml/ledger/<season>.sqlite.gz) - the database copies are redundant.
--
-- A VACUUM FULL afterwards (or the Supabase dashboard's vacuum) returns
-- the freed space to the project's quota immediately.

DROP TABLE IF EXISTS "Player-Snapshots-backtrack-prod";
DROP TABLE IF EXISTS "Team-Stats-backtrack-prod";
DROP TABLE IF EXISTS "Goalie-Snapshots-backtrack-prod";
DROP TABLE IF EXISTS "Player-Game-Raw-Markers-prod";
-- the empty dev twin (0 rows since creation; the migrate job created it on
-- dev deploys before the family moved to Storage)
DROP TABLE IF EXISTS "Player-Game-Raw-Markers-dev";

VACUUM;

-- DO NOT drop: Player-Snapshots-{dev,prod} (the live archive the site
-- pipeline writes), Picks / Historic-Picks / Working / Metrics /
-- user_preferences (the website's tables), auth.*
