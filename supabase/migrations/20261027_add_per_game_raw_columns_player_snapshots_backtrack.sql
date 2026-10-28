-- Player-Snapshots-backtrack: the RAW per-game layer, in-column (stage 2 -
-- exact same-day rolling/career features at picks time).
--
-- WHY (Block C - the raw per-game numbers)
-- The 16 shot/TOI columns of 20261024 are AGGREGATES ("entering game D", one
-- value per published row). They cannot state a game that the backtrack
-- publish has not covered yet, so a run on the morning after games computes
-- every rolling/ewm/career feature WITHOUT those games. Block C adds the
-- numbers themselves, per player per game, so the daily pick pass can extend
-- the per-game ledger itself:
--
--   * the FIRST run of a day backfills YESTERDAY's (and any older uncovered)
--     completed games' per-player raw stats into these columns (run_daily_picks
--     .backfill_raw_game_days, keyed (date, player_id), boxscore-sourced -
--     shots/points per skater per game from gamecenter boxscores, toi parsed
--     to decimal minutes by the same parser shot_features always used);
--   * every run then recomputes today's 14 shot/TOI features with the exact
--     shot_features math (model.shot_features_from_games - imported, never
--     forked) over the per-game history INCLUDING that backfill, instead of
--     attaching last published aggregates (the stage-1 approximation).
--
-- COLUMN CHOICE - exactly what the features consume
-- model.shot_features()/shot_features_from_games read per game exactly
-- goals, shots, toi (MM:SS or minutes; grouped per player and season, and
-- expanded career sums). add_derived's prior-goal/rolling-scored features read
-- the row's `scored` label (existing column). So the block ships:
--     game_shots, game_goals, game_points, game_toi_min
-- game_points is not consumed by any of the 14 (goals are) but is free from the
-- same boxscore and is the per-game provenance the trending rates quote; it
-- ships for the audit trail. game_plusminus / assists / pim / shift counts were
-- checked and are consumed by NO feature today (drop them instead of paying
-- four never-read columns; the source-of-truth raw layer stays
-- data/data dumps + data/raw_nhl.sqlite for anything widened later).
-- `played` needs NO flag: for a completed game, a row carrying non-null
-- game_shots IS the played mark (a boxscore lists exactly the players who
-- appeared); (date, player_id) is the PK.

ALTER TABLE "Player-Snapshots-backtrack-__ENV__"
    ADD COLUMN IF NOT EXISTS "game_shots"   INTEGER,
    ADD COLUMN IF NOT EXISTS "game_goals"   INTEGER,
    ADD COLUMN IF NOT EXISTS "game_points"  INTEGER,
    ADD COLUMN IF NOT EXISTS "game_toi_min" DOUBLE PRECISION;

-- The backfill idempotency markers.
--
-- WHY: the backfill is the day's FIRST run's write step (before the picks
-- compute; run_daily_picks runs it every pass). It must be a no-op on the
-- later passes the same day (the per-game kick architecture fires ~15 runs
-- per slate date) and re-entrant after a partial crawl. A marker ROW per
-- processed date settles both: once a date is fully written the marker is
-- upserted and every subsequent pass skips the date without re-crawling; a
-- date whose writes failed, or whose games had not all ended (FUT/LIVE
-- state), stays UNMARKED and is retried by a later run. games/player_games
-- record what the date held; backfilled_at is the stamp.
--
-- TEXT date: the whole backtrack family keys dates as TEXT
-- (20261008_add_player_snapshots_backtrack.sql); the marker keys must stringed
-- compare the same way.
CREATE TABLE IF NOT EXISTS "Player-Game-Raw-Markers-__ENV__" (
    "date" TEXT NOT NULL,
    "games" INTEGER NOT NULL,
    "player_games" INTEGER NOT NULL,
    "backfilled_at" TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT "Player-Game-Raw-Markers-__ENV___pkey" PRIMARY KEY ("date")
);

-- Same exposure stance as the backtrack family: service role only (the pick
-- run's client), no anon reads.
ALTER TABLE "Player-Game-Raw-Markers-__ENV__" ENABLE ROW LEVEL SECURITY;

-- NO retroactive data backfill of this migration itself: the crawl that built
-- the backtrack family already landed every historical date it covered, and
-- the local raw store (data/dumps/<season>.sql.gz -> data/raw_nhl.sqlite)
-- carries the full per-game precision for 2013-10-01..last dump. So the
-- pre-gap published rows leave Block C NULL and the daily run fills the
-- columns FORWARD (yesterday-forward, per (date, player_id) upserts). A
-- down-migration is a plain DROP of the four columns + the marker table.
