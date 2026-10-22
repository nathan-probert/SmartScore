-- Goalie-Snapshots-backtrack: season-to-date goalie stats reconstructed from NHL game logs.
--
-- WHY A SEPARATE TABLE
-- A goalie's as-of-date stats are properties of the GOALIE, not of the skaters
-- who face him. The live archive denormalises them onto Player-Snapshots-{ENV} as
-- the opp_goalie_* block, so one goalie's numbers are repeated across ~21 skater
-- rows per team-date in every one of those tables. Keying by
-- (season, player_id, date) makes the grain honest: one row per goalie
-- appearance, mirroring Team-Stats-backtrack-{ENV} did for team rates
-- (20261008_add_team_stats_backtrack.sql).
--
-- Join Player-Snapshots-backtrack-{ENV} on (date, opposing team_abbrev) to get the
-- night's goalie next to each pick row - a plain lookup, not a rewrite.
--
-- THE RATES (per goalie_features in the raw store - see the SCHEMA comment in
-- smartscore/scripts/backtrack/local_store.py for the verification)
-- gaa      - goals-against average, official formula 60 * GA / minutes played.
--            NOT GA/games: a pulled goalie's partial night counts less than a
--            full one (Vasilevskiy 2023-24 reproduces club-stats' 2.899242).
-- save_pct - saves / shots against over the window, a fraction (0.905, not 90.5),
--            summed over the window rather than a mean of per-game percentages.
-- record   - "W-L-OTL" text, the archive's own format; O covers both overtime
--            and shootout losses.
-- shutouts - official per-game count summed over the window.
-- games_played - appearances in the window.
--
-- All rates use the same strictly-before cutoff as the player and team tables,
-- so a row reads as the stats ENTERING that night's game. The window is scoped
-- per club: a traded goalie's accumulation resets for the new club (Blackwood
-- 2024-25 reads 19 GP / 3.004 on SJS and 37 GP / 2.328 on COL), matching what
-- club-stats would have answered for the club he was starting for.
--
-- NULL, NOT 0, ON A CLUB DEBUT
-- A goalie's first game with a club has no games before it. club-stats does not
-- list him until he appears, so live left every stat key None; a zero GAA would
-- claim a perfect rate that was never observed. This is the opposite of the
-- player table's debut-0 convention, and deliberately so.
--
-- WHAT IS NOT HERE
-- No status/confirmed columns. Those describe a PRE-GAME PROJECTION (was he
-- confirmed as the starter), not history, and nothing before puck drop is
-- reconstructable from game logs; a published row is post-hoc by definition.
--
-- name comes from the daily starter feed's full spelling ("Ilya Samsonov")
-- resolved to the appearance row, with the box score's initial form
-- ("I. Samsonov") as the fallback, matching publish()'s name policy.
--
-- Let `season` and `team_abbrev` ride every row: a date maps to exactly one
-- season, and the abbreviation is the goalie's stable club identity for that
-- stretch of the season. team_name is denormalised so callers do not need
-- team_map.py to read a place name.
--
-- Idempotent because .github/workflows/deploy.yml pipes every file in this
-- directory through psql on every deploy with no migration history.

CREATE TABLE IF NOT EXISTS "Goalie-Snapshots-backtrack-__ENV__" (
    "season" TEXT NOT NULL,
    "player_id" BIGINT NOT NULL,
    "team_abbrev" TEXT NOT NULL,
    "team_name" TEXT,
    "date" TEXT NOT NULL,
    "name" TEXT NOT NULL,

    -- As-of-date stats, per goalie_features. NULL on the club debut, never 0.
    "gaa" DOUBLE PRECISION,
    "save_pct" DOUBLE PRECISION,
    "record" TEXT,
    "shutouts" INTEGER,
    "games_played" INTEGER,

    CONSTRAINT "Goalie-Snapshots-backtrack-__ENV___pkey" PRIMARY KEY ("season", "player_id", "date")
);

-- The PK covers reads filtered by player_id. This serves the join from
-- Player-Snapshots-backtrack-{ENV}, which filters on date.
CREATE INDEX IF NOT EXISTS "Goalie-Snapshots-backtrack-__ENV___date_idx"
    ON "Goalie-Snapshots-backtrack-__ENV__" ("date");

ALTER TABLE "Goalie-Snapshots-backtrack-__ENV__" ENABLE ROW LEVEL SECURITY;
