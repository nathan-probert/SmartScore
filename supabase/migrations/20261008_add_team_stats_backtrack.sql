-- Team-Stats-backtrack: team-level rates per team per game.
--
-- WHY A SEPARATE TABLE
-- tgpg and otga are TEAM attributes, not player stats. Every player on a club
-- carries the same tgpg for a given date - all 21 Bruins rows hold 2.25. The live
-- archive denormalises them onto Player-Snapshots-{ENV}, so one number is repeated
-- across ~21 rows per team-date and there is no single place to read a team's
-- numbers from. Keying by (season, team_abbrev, game_id) makes the grain honest and
-- makes otshga trivial to add later without a second pass over player rows.
--
-- Join Player-Snapshots-backtrack-{ENV} on (date, team_name) to get both in one
-- query; that is a plain lookup, not a rewrite.
--
-- THE RATES
-- tgpg  - team's own goals FOR per game, season to date.
-- otga  - the OPPONENT's goals AGAINST per game, i.e. how many goals this team
--         scores ON that opponent. It is an offence measure, not a defence one:
--         goals scored against an opponent are, by definition, that opponent's
--         goals against - the same quantity read from opposite ends. It is NOT
--         this team's own concessions; computing that instead matched 107 of
--         16,301 archive rows (the two are close but unrelated). See the SCHEMA
--         comment in smartscore/scripts/backtrack/local_store.py for the
--         verification, and note the value is therefore opponent-specific: two
--         players on the same club facing different opponents that night carry
--         different otga values, unlike tgpg.
--
-- Both use the same strictly-before cutoff as gpg, so a row for game N carries the
-- rate entering it and a team's first game of the season is NULL rather than 0. A
-- zero would be wrong: a team cannot average zero goals per game, and the archive
-- emits exactly that (Winnipeg, Anaheim, Dallas and Carolina all show tgpg = 0 at
-- the start of the current season).
--
-- otshga - the OPPONENT's shorthanded goals AGAINST per game - the same
--   opponent-relative reading as otga. A goal this team scores on the power play
--   IS a shorthanded goal against the opponent, so summing this team's
--   power_play_goals over the games shared with that opponent gives their
--   shorthanded goals against directly. Verified against
--   api.nhle.com/stats/rest/en/team/penaltykilltime for 2023-24: all 16
--   non-playoff teams match exactly (the endpoint's season totals include playoff
--   games; this table is regular season only, which is the entire difference for
--   the other 16). First game of the season is NULL, and later games read 0.0
--   when nothing has been scored - the archive stores zeros there too.
--
-- The archive's tgpg/otga are sparse and lossy the same way gpg was - Toronto
-- carries tgpg on two dates out of the whole archive - so this table is expected to
-- be denser than what it replaces.
--
-- `season` and `team_abbrev` are stored alongside the date because a given date maps
-- to exactly one season, and the abbreviation is the team's stable identity for that
-- season. team_name is denormalised in so callers do not need team_map.py to read a
-- place name.
--
-- Idempotent because .github/workflows/deploy.yml pipes every file in this directory
-- through psql on every deploy with no migration history.

CREATE TABLE IF NOT EXISTS "Team-Stats-backtrack-__ENV__" (
    "season" TEXT NOT NULL,
    "team_abbrev" TEXT NOT NULL,
    "team_name" TEXT,
    "date" TEXT NOT NULL,
    "game_id" BIGINT NOT NULL,
    "tgpg" DOUBLE PRECISION,
    "otga" DOUBLE PRECISION,
    "otshga" DOUBLE PRECISION,
    CONSTRAINT "Team-Stats-backtrack-__ENV___pkey" PRIMARY KEY ("season", "team_abbrev", "game_id")
);

-- The PK leads with season/team. This serves the per-date read that joins against
-- Player-Snapshots-backtrack-{ENV}, which filters on date.
CREATE INDEX IF NOT EXISTS "Team-Stats-backtrack-__ENV___date_idx"
    ON "Team-Stats-backtrack-__ENV__" ("date");

ALTER TABLE "Team-Stats-backtrack-__ENV__" ENABLE ROW LEVEL SECURITY;
