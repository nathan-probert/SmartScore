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
-- Run WITHOUT the VACUUM line if your editor wraps scripts in a
-- transaction (the Supabase SQL editor does): VACUUM cannot run inside
-- one (ERROR 25001) and the failed statement rolls the DROPs back too.

DROP TABLE IF EXISTS "Player-Snapshots-backtrack-prod";
DROP TABLE IF EXISTS "Team-Stats-backtrack-prod";
DROP TABLE IF EXISTS "Goalie-Snapshots-backtrack-prod";
DROP TABLE IF EXISTS "Player-Game-Raw-Markers-prod";
-- the empty dev twin (0 rows since creation; the migrate job created it on
-- dev deploys before the family moved to Storage)
DROP TABLE IF EXISTS "Player-Game-Raw-Markers-dev";

-- Space: Postgres autovacuum returns the freed pages to the project's
-- quota within hours. For an immediate reclaim use the Dashboard
-- (Database -> Vacuum settings) - VACUUM cannot run in the SQL editor.

-- DO NOT drop: Player-Snapshots-{dev,prod} (the live archive the site
-- pipeline writes), Picks / Historic-Picks / Working / Metrics /
-- user_preferences (the website's tables), auth.*
