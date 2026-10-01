-- Migration: store projected starting lineup info recorded by handle_get_lineups.
--
-- Each skater row gains lineup fields (see merge_lineup_data in
-- smartscore/service.py). Supabase rejects upserts with unknown columns, so these
-- must exist on the Picks and Historic-Picks tables (dev and prod).
--
-- How to apply: run this file in the Supabase dashboard SQL editor
-- (there is no migration runner wired into CI). It is idempotent.
--
-- lineup_unit holds the forward line only (F1-F4) since the picks table is
-- skater-scoped; defence pairings and goalie designations are parsed from the same
-- source payload but deliberately not stored per skater. pp_unit holds the RotoWire
-- power play unit label ("POWER PLAY #1" / "POWER PLAY #2") and is null for anyone
-- not on a unit. lineup_status is PROJECTED when a forward line matched, UNKNOWN
-- otherwise, so a failed or empty fetch is distinguishable from a genuine miss.
--
-- These are *projected* lineups, published in the morning. Confirmed goalie
-- designations continue to come from the opp_goalie_* columns added by
-- 20260930_add_starting_goalie_columns.sql.

ALTER TABLE "Picks-dev"
    ADD COLUMN IF NOT EXISTS lineup_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_position_group TEXT,
    ADD COLUMN IF NOT EXISTS pp_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_status TEXT;

ALTER TABLE "Picks-prod"
    ADD COLUMN IF NOT EXISTS lineup_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_position_group TEXT,
    ADD COLUMN IF NOT EXISTS pp_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_status TEXT;

ALTER TABLE "Historic-Picks-dev"
    ADD COLUMN IF NOT EXISTS lineup_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_position_group TEXT,
    ADD COLUMN IF NOT EXISTS pp_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_status TEXT;

ALTER TABLE "Historic-Picks-prod"
    ADD COLUMN IF NOT EXISTS lineup_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_position_group TEXT,
    ADD COLUMN IF NOT EXISTS pp_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_status TEXT;
