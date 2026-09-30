-- Migration: store starting goalie info recorded by handle_get_goalies.
--
-- Each skater row now carries opp_goalie_* fields (see merge_goalie_data in
-- smartscore/service.py). Supabase rejects upserts with unknown columns, so
-- these must exist on the Picks and Historic-Picks tables (dev and prod).
--
-- How to apply: run this file in the Supabase dashboard SQL editor
-- (there is no migration runner wired into CI). It is idempotent.
--
-- DailyFaceoff fallback note: if the RotoWire tables endpoints ever break,
-- starters can be scraped from https://www.dailyfaceoff.com/starting-goalies/
-- instead; the column layout here does not need to change.

ALTER TABLE "Picks-dev"
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

ALTER TABLE "Picks-prod"
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

ALTER TABLE "Historic-Picks-dev"
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

ALTER TABLE "Historic-Picks-prod"
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
