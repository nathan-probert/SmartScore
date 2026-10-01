-- Migration: store starting goalie info recorded by handle_get_goalies.
--
-- Each skater row now carries opp_goalie_* fields (see merge_goalie_data in
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
-- DailyFaceoff fallback note: if the RotoWire tables endpoints ever break,
-- starters can be scraped from https://www.dailyfaceoff.com/starting-goalies/
-- instead; the column layout here does not need to change.

ALTER TABLE "Picks-__ENV__"
    ADD COLUMN IF NOT EXISTS opp_goalie_name TEXT,
    ADD COLUMN IF NOT EXISTS opp_goalie_team TEXT,
    ADD COLUMN IF NOT EXISTS opp_goalie_status TEXT,
    ADD COLUMN IF NOT EXISTS opp_goalie_confirmed BOOLEAN,
    ADD COLUMN IF NOT EXISTS opp_goalie_nhl_id BIGINT,
    ADD COLUMN IF NOT EXISTS opp_goalie_gaa DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS opp_goalie_save_pct DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS opp_goalie_record TEXT,
    ADD COLUMN IF NOT EXISTS opp_goalie_shutouts INTEGER,
    ADD COLUMN IF NOT EXISTS opp_goalie_games_played INTEGER;

ALTER TABLE "Historic-Picks-__ENV__"
    ADD COLUMN IF NOT EXISTS opp_goalie_name TEXT,
    ADD COLUMN IF NOT EXISTS opp_goalie_team TEXT,
    ADD COLUMN IF NOT EXISTS opp_goalie_status TEXT,
    ADD COLUMN IF NOT EXISTS opp_goalie_confirmed BOOLEAN,
    ADD COLUMN IF NOT EXISTS opp_goalie_nhl_id BIGINT,
    ADD COLUMN IF NOT EXISTS opp_goalie_gaa DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS opp_goalie_save_pct DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS opp_goalie_record TEXT,
    ADD COLUMN IF NOT EXISTS opp_goalie_shutouts INTEGER,
    ADD COLUMN IF NOT EXISTS opp_goalie_games_played INTEGER;
