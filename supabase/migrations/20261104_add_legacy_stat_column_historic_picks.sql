-- Migration: add legacy_stat column to Historic-Picks.

-- Historic-Picks now carries the site's legacy weighted-average prediction
-- alongside the ML stat, mirroring Picks (see 20261103_add_legacy_stat_column.sql).
-- The strip approach is reverted; legacy_stat passes through to Historic-Picks.

-- Dev and prod share one Supabase project and differ only by table-name
-- suffix (see f"Historic-Picks-{ENV}" in smartscore/utility.py). The table
-- name below therefore uses an __ENV__ placeholder. Every statement is
-- idempotent (ADD COLUMN IF NOT EXISTS). psql keeps no migration history, so
-- CI re-applies every file on each run.

-- No SQL backfill: existing rows populate on the next site publish.

ALTER TABLE "Historic-Picks-__ENV__"
    ADD COLUMN IF NOT EXISTS "legacy_stat" DOUBLE PRECISION;
