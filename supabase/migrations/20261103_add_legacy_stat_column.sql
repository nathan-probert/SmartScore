-- Migration: preserve the site's legacy weighted-average prediction on Picks.

-- The site's own publish writes its weighted-average score into `stat`; the
-- ML overlay later overwrites `stat` with the ML rank percentile (0-1). To
-- keep the site's original value available alongside, store it in
-- `legacy_stat` (same 0-1 scale). Written by the site publish only; the ML
-- overlay never touches it.

-- Dev and prod share one Supabase project and differ only by table-name
-- suffix (see f"Picks-{ENV}" in smartscore/utility.py). The table name below
-- therefore uses an __ENV__ placeholder. Every statement is idempotent
-- (ADD COLUMN IF NOT EXISTS). psql keeps no migration history, so CI
-- re-applies every file on each run.

-- No SQL backfill: existing rows populate on the next site publish. Only the
-- Picks table is touched; Historic-Picks and the ML log payload are out of
-- scope for this migration.

ALTER TABLE "Picks-__ENV__"
    ADD COLUMN IF NOT EXISTS "legacy_stat" DOUBLE PRECISION;
