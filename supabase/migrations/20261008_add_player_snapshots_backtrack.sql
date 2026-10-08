-- Player-Snapshots-backtrack: season-to-date stats reconstructed from NHL game logs.
--
-- WHY THIS EXISTS
-- Player-Snapshots-{ENV} stores one row per player per date, captured live as the
-- pipeline runs. Two properties of that capture limit it:
--
--   1. Sparse. Rows exist only for pick-relevant dates, not every game a player
--      played, so the table cannot answer "what were this player's stats entering
--      game X" for an arbitrary X.
--   2. Lossy on early rows. Rows captured in the first weeks of a season store
--      rates at 2 decimal places (0.31, 0.33) where later rows carry 6
--      (0.320755). The rounding is baked in at capture time and cannot be undone.
--
-- This table is built by walking the NHL per-game box scores forward and
-- accumulating, so every date is present and every rate is full precision. It is
-- meant to REPLACE the live archive, not supplement it - the player set comes from
-- the games themselves (see smartscore/scripts/backtrack/reconstruct.py), so a
-- player picked for the first time appears without anyone adding them.
--
-- THE DERIVATION
-- For a season and a date D, a player's totals are the sum over that player's
-- games with game_date STRICTLY BEFORE D:
--
--     gp_to_date  = count(games before D)
--     gpg         = goals_to_date / gp_to_date        (null when gp_to_date = 0)
--
-- Strictly-before is the cutoff that reproduces the stored values: the stored
-- 2024-03-02 row for Brett Kulak is 2/57, and 3/58 appears on 2024-03-03 once
-- that night's goal lands. A row therefore reads as "entering the game on D".
--
-- Scope is regular season only (NHL gameTypeId 2), matching the stored values.
-- Kulak's 25-game, 1-goal 2024 playoff run would otherwise inflate both numerator
-- and denominator.
--
-- WHY THERE ARE NO COUNTER COLUMNS HERE
-- gp_to_date, goals_to_date and friends used to live in this table. They are gone,
-- and deliberately so: they are the raw layer, and the raw layer now lives in
-- data/raw_nhl.sqlite as one row per player per GAME. That store is the audit
-- trail - any rate here is recomputable from it with a window function, and it is
-- where a new feature gets added without re-crawling the API. Holding a partial
-- copy of the counters here meant two places to drift.
--
-- Consequence, stated plainly: once the counters are gone, a rate in this table
-- cannot be recomputed or audited from Supabase alone. It requires the SQLite file.
-- That is the intended trade - SQLite is the source of truth.
--
-- COLUMN SET
-- The columns below are exactly Player-Snapshots-{ENV}'s, so this table is a
-- drop-in replacement and anything reading the archive reads this unchanged. Today
-- only date/player_id/name/team_name/home/gpg are populated. The rest are declared
-- so the shape matches, and are filled by later passes:
--
--   * hgpg, hppg  - 3-year windows per smartscore_info_client's get_hgpg(years=3),
--                   which needs seasonTotals from the landing payload. NOT
--                   reconstructable from one season of game logs.
--   * five_gpg    - derivable from the last 5 games in the raw store.
--   * tgpg, otga, otshga - definitions not established; deliberately left null
--                   rather than filled with a same-season ratio under the same name.
--   * injury_*, tims, opp_goalie_*, lineup_*, pp_unit, scored
--                   - not in the game-log feed at all. They come from the injury,
--                     lineup and goalie endpoints. The live archive remains their
--                     source until those passes exist.
--
-- Null here means "not reconstructed yet", never "not applicable".
--
-- `date` is TEXT, matching Player-Snapshots-{ENV}. write_historic_db compares dates
-- as strings and joins across these tables, so both sides have to agree.
--
-- Idempotent because .github/workflows/deploy.yml pipes every file in this
-- directory through psql on every deploy with no migration history.

CREATE TABLE IF NOT EXISTS "Player-Snapshots-backtrack-__ENV__" (
    -- Key and identity
    "date" TEXT NOT NULL,
    "player_id" BIGINT NOT NULL,
    "name" TEXT NOT NULL,
    "team_name" TEXT,
    "home" BOOLEAN,

    -- Goal-scoring rates. gpg is season-to-date, pre-game.
    "gpg" DOUBLE PRECISION,
    "hgpg" DOUBLE PRECISION,
    "five_gpg" DOUBLE PRECISION,
    "hppg" DOUBLE PRECISION,
    "tgpg" DOUBLE PRECISION,
    "otga" DOUBLE PRECISION,
    "otshga" DOUBLE PRECISION,

    -- Injury report. Not in the game-log feed.
    "injury_status" TEXT,
    "injury_desc" TEXT,

    -- Tims' team projection.
    "tims" INTEGER,

    -- Opposing goalie. Not in the game-log feed; goalies are excluded from this
    -- table as players, so this describes the opponent, not a row subject.
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

    -- Lineup. Not in the game-log feed.
    "lineup_unit" TEXT,
    "lineup_position_group" TEXT,
    "pp_unit" TEXT,
    "lineup_status" TEXT,

    -- Training label. null = not graded, matching Player-Snapshots-{ENV}.
    "scored" INTEGER,

    CONSTRAINT "Player-Snapshots-backtrack-__ENV___pkey" PRIMARY KEY ("date", "player_id")
);

-- The PK covers `WHERE date = ?`. This serves the per-player rebuild and the diff
-- against Player-Snapshots-{ENV}, which both filter on player_id.
CREATE INDEX IF NOT EXISTS "Player-Snapshots-backtrack-__ENV___player_idx"
    ON "Player-Snapshots-backtrack-__ENV__" ("player_id", "date");

-- Same reasoning as the live archive: the training set is not publicly readable.
ALTER TABLE "Player-Snapshots-backtrack-__ENV__" ENABLE ROW LEVEL SECURITY;