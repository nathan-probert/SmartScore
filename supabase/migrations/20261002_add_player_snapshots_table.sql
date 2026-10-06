-- Player-Snapshots: the long-run historical archive that replaces MongoDB (#113).
--
-- Every other Supabase table in this project is either a rolling window
-- (Historic-Picks keeps DAYS_TO_KEEP_HISTORIC_DATA dates) or a single-day
-- snapshot (Picks is wiped and rewritten). This table is the one that actually
-- accumulates history: one row per player per date, never truncated.
--
-- Keying:
--   PRIMARY KEY (date, player_id). Deliberately NOT the positional `id` column
--   the Picks/Historic-Picks writers assign (`player["id"] = i + 1`), which is a
--   per-request ordinal and is not stable across runs. `player_id` is the NHL
--   player id, which is stable. The primary key makes re-uploading a date an
--   idempotent upsert rather than the append-only duplicate rows the Mongo
--   `insertMany` write path produced (#113).
--
-- `scored` is nullable INTEGER (null = game not graded yet, 0 = no goal,
-- 1 = goal) to match Historic-Picks."Scored" and the training label in
-- scripts/shared.py. Mongo used a boolean with the field absent when ungraded;
-- the null/0/1 triple is the same three states without the ambiguity of
-- "absent" vs "false".
--
-- `date` is TEXT, matching Picks/Historic-Picks. It is deliberately not DATE:
-- write_historic_db compares dates as strings (`date < today`) and joins these
-- rows against Historic-Picks."date", so both sides have to agree.
--
-- All statements are idempotent because .github/workflows/deploy.yml pipes every
-- file in this directory through psql on every deploy with no migration history.
--
-- Note on `home`/`hppg`/`otshga`: these are dropped by save_to_db and
-- update_historical_data because the frontend does not show them, but they are
-- training features (FEATURES in scripts/shared.py). Mongo kept them, so this
-- table keeps them.
--
-- Note on the absence of `team_abbr`: do not add it back. merge_players_and_teams
-- drops team_abbr (TEAM_MERGE_EXCLUDED_FIELDS) before the payload is built, so no
-- row has ever carried one - Mongo included. `team_name` (the NHL schedule place
-- name, e.g. "Toronto") is therefore the only team identity a row has, and the
-- postponed-game delete matches on it. The NHL score feed, which is the only
-- source of a postponed game, reports abbreviations, so service.resolve_team_names
-- translates them through that date's schedule before deleting.

CREATE TABLE IF NOT EXISTS "Player-Snapshots-__ENV__" (
    "date" TEXT NOT NULL,
    "player_id" BIGINT NOT NULL,
    "scored" INTEGER,
    "name" TEXT NOT NULL,
    "team_name" TEXT,
    "home" BOOLEAN,
    "gpg" DOUBLE PRECISION,
    "hgpg" DOUBLE PRECISION,
    "five_gpg" DOUBLE PRECISION,
    "hppg" DOUBLE PRECISION,
    "tgpg" DOUBLE PRECISION,
    "otga" DOUBLE PRECISION,
    "otshga" DOUBLE PRECISION,
    "injury_status" TEXT,
    "injury_desc" TEXT,
    "tims" INTEGER,
    "opp_goalie_name" TEXT,
    "opp_goalie_team" TEXT,
    "opp_goalie_status" TEXT,
    "opp_goalie_confirmed" BOOLEAN,
    "opp_goalie_nhl_id" BIGINT,
    "opp_goalie_gaa" DOUBLE PRECISION,
    "opp_goalie_save_pct" DOUBLE PRECISION,
    "opp_goalie_record" TEXT,
    "opp_goalie_shutouts" INTEGER,
    "opp_goalie_games_played" INTEGER,
    "lineup_unit" TEXT,
    "lineup_position_group" TEXT,
    "pp_unit" TEXT,
    "lineup_status" TEXT,
    CONSTRAINT "Player-Snapshots-__ENV___pkey" PRIMARY KEY ("date", "player_id")
);

-- The PK already covers `WHERE date = ?` lookups (leading column), so this only
-- needs to serve the unscored-dates scan, which is the hot query on the first
-- run of every game day.
CREATE INDEX IF NOT EXISTS "Player-Snapshots-__ENV___unscored_idx"
    ON "Player-Snapshots-__ENV__" ("date")
    WHERE "scored" IS NULL;

-- Service role bypasses RLS, which is the only client smartscore/player_archive.py
-- uses. The anon key (SUPABASE_CLIENT, which the frontend holds) therefore gets
-- nothing, so the training set is not publicly readable. The existing
-- Picks/Historic-Picks tables predate this and have no RLS at all.
ALTER TABLE "Player-Snapshots-__ENV__" ENABLE ROW LEVEL SECURITY;
