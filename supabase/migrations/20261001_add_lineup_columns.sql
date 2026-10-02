-- Migration: store projected starting lineup info recorded by handle_get_lineups.
--
-- Each skater row now carries lineup fields (see merge_lineup_data in
-- smartscore/service.py). Supabase rejects upserts with unknown columns, so
-- these must exist on the Picks and Historic-Picks tables.
--
-- Dev and prod share one Supabase project and differ only by table-name
-- suffix (see f"Picks-{ENV}" in smartscore/utility.py). The table names below
-- therefore use an __ENV__ placeholder rather than repeating the statement for
-- each environment. The migrate job in .github/workflows/deploy.yml
-- substitutes the current environment before piping this file to psql (dev on
-- a deploy-labelled pull request, prod on a merge to main); to apply it by
-- hand, replace the placeholder with dev or prod first.
--
-- Every statement is idempotent (ADD COLUMN IF NOT EXISTS). psql keeps no
-- migration history, so CI re-applies every file on each run.
--
-- lineup_unit holds the forward line only (F1-F4) since the picks table is
-- skater-scoped; defence pairings and goalie designations are parsed from the
-- same source payload but deliberately not stored per skater. pp_unit holds the
-- RotoWire power play unit label ("POWER PLAY #1" / "POWER PLAY #2") and is null
-- for anyone not on a unit. lineup_status is PROJECTED when a forward line
-- matched, UNKNOWN otherwise, so a failed or empty fetch is distinguishable from
-- a genuine non-match.
--
-- These are *projected* lineups, published in the morning. Confirmed goalie
-- designations continue to come from the opp_goalie_* columns added by
-- 20260930_add_starting_goalie_columns.sql.

ALTER TABLE "Picks-__ENV__"
    ADD COLUMN IF NOT EXISTS lineup_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_position_group TEXT,
    ADD COLUMN IF NOT EXISTS pp_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_status TEXT;

ALTER TABLE "Historic-Picks-__ENV__"
    ADD COLUMN IF NOT EXISTS lineup_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_position_group TEXT,
    ADD COLUMN IF NOT EXISTS pp_unit TEXT,
    ADD COLUMN IF NOT EXISTS lineup_status TEXT;
