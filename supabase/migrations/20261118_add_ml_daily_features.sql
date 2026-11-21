-- Migration: ML-Daily-Features-{ENV} - the small per-slate-day feature table
-- the daily picks run reads INSTEAD of the full backtrack ledger.
--
-- WHY THIS EXISTS
-- The daily run's only consumers from the 560 MB backtrack family are the
-- 20 history-derived feature columns (the 14 shot/TOI features, prev2/prev3
-- goals, games_since_goal, days_since_game, club/opp rest days) - everything
-- else it reads comes from the live snapshot (Player-Snapshots-{env}, stays
-- in the DB) or is computed at predict time (rank_*). The full ledger moves
-- to per-season files in Supabase Storage (ml_ledger.py); the nightly job
-- computes these rows where the full data lives and publishes ~one row per
-- likely slate player per date. A whole season is ~150k rows of ~20 doubles
-- - megabytes, not hundreds.
--
-- One row per (date, player_id): the values the training core would have
-- computed for a pre-game row ON that date ("entering the game"), strictly
-- prior semantics - the same convention every backtrack row uses.
--
-- club/opp are the abbreviations the publisher resolved from the NHL
-- schedule (the same join keys _map_live_rows/_inject_today_context resolve
-- from the history when the full-ledger path runs); name rides along for
-- human-readable diffs. Nothing here is a training input by itself: the
-- daily run merges these onto the live rows before encoding.
--
-- Service role only (the run's client), no anon reads.
--
-- All statements idempotent: deploy.yml's migrate job re-applies every file.

CREATE TABLE IF NOT EXISTS "ML-Daily-Features-__ENV__" (
    "date" TEXT NOT NULL,
    "player_id" BIGINT NOT NULL,
    "name" TEXT,
    "club" TEXT,
    "opp" TEXT,
    -- the 14 shot/TOI features (model.SHOT_FEATURES), precomputed
    "sd_shots_pg" DOUBLE PRECISION,
    "sd_goals_pg" DOUBLE PRECISION,
    "sd_toi_pg" DOUBLE PRECISION,
    "sd_sh_pct" DOUBLE PRECISION,
    "r5_shots_pg" DOUBLE PRECISION,
    "r5_goals_pg" DOUBLE PRECISION,
    "r10_shots_pg" DOUBLE PRECISION,
    "r10_goals_pg" DOUBLE PRECISION,
    "r10_toi_pg" DOUBLE PRECISION,
    "ewm_goals" DOUBLE PRECISION,
    "ewm_shots" DOUBLE PRECISION,
    "career_games" INTEGER,
    "career_goals_pg" DOUBLE PRECISION,
    "career_shots_pg" DOUBLE PRECISION,
    -- the goal-streak + rest block (add_derived's history-derived set)
    "prev2_goals" DOUBLE PRECISION,
    "prev3_goals" DOUBLE PRECISION,
    "games_since_goal" DOUBLE PRECISION,
    "days_since_game" INTEGER,
    "club_rest_days" INTEGER,
    "opp_rest_days" INTEGER,
    CONSTRAINT "ML-Daily-Features-__ENV___pkey" PRIMARY KEY ("date", "player_id")
);

CREATE INDEX IF NOT EXISTS "ML-Daily-Features-__ENV___date_idx"
    ON "ML-Daily-Features-__ENV__" ("date");

ALTER TABLE "ML-Daily-Features-__ENV__" ENABLE ROW LEVEL SECURITY;
