-- Player-Snapshots-backtrack: shot/TOI rolling block + club identity (16 new columns).
--
-- SPEC: docs/supabase_shot_columns.md (§1 Block A, §2 Block B, §4 sketch).
-- Regressed in from the migration sketch there; the owner-approved option 1
-- ("train purely from Supabase") turns the sketch into this file. Backfill of
-- the values is a one-shot job (smartscore/scripts/backtrack/publish_shot_cols.py)
-- run after this migration lands; publish() then carries the same keys daily.
--
-- WHY (Block A - the 14 shot/TOI features, model.py SHOT_FEATURES at c82b2e7)
-- The ensemble's shot/TOI/rolling block (sd_*, r5/r10_*, ewm_*, career_*) is
-- computed against data/raw_nhl.sqlite by model.py shot_features(). SQLite is
-- not reachable from the in-cloud ML Lambda, so those 14 features are the only
-- DATASET_FEATURES inputs missing from Supabase. This migration adds the
-- columns holding their precomputed values; the strictly-prior-games semantics
-- ("entering game D", sorted (player_id, date, season), shift(1)) are the
-- same shape Player-Snapshots-backtrack already has - one row per player per
-- game date, values prefixed by games strictly before D.
--
-- WHY (Block B - the 2 club-abbreviation columns)
-- club_map() joins player_games.team_abbrev / opponent_abbrev per
-- (date, player_id) because the snapshot's team_name is a place name that
-- renders both New York clubs as "New York", never resolvable by name merge.
-- Publishing the abbreviations makes club := team_abbrev and
-- opp := opponent_abbrev available without SQLite. Never join by team_name.
--
-- No PK change: all 16 columns ride the existing (date, player_id) primary
-- key through upsert. No Team-Stats-backtrack / Goalie-Snapshots-backtrack
-- change: their rates already cover the team and opposing-goalie features
-- joined at read time. Down-migration is a plain DROP COLUMN of these 16.
ALTER TABLE "Player-Snapshots-backtrack-__ENV__"
    ADD COLUMN IF NOT EXISTS "sd_shots_pg"      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "sd_goals_pg"      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "sd_toi_pg"        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "sd_sh_pct"        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r5_shots_pg"      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r5_goals_pg"      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r10_shots_pg"     DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r10_goals_pg"     DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r10_toi_pg"       DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "ewm_goals"        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "ewm_shots"        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "career_games"     INTEGER,
    ADD COLUMN IF NOT EXISTS "career_goals_pg"  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "career_shots_pg"  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "team_abbrev"      TEXT,
    ADD COLUMN IF NOT EXISTS "opponent_abbrev"  TEXT;

-- Optional: serves the (date, club) join that Team-Stats lookups prefer.
-- The existing PK (date, player_id) already leads with date, so this index is
-- only worth adding if the planner churns on it in practice.
-- CREATE INDEX IF NOT EXISTS "Player-Snapshots-backtrack-__ENV___club_idx"
--     ON "Player-Snapshots-backtrack-__ENV__" ("team_abbrev", "date");
