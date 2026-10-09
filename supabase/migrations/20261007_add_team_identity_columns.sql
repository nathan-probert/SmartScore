-- Migration: unambiguous team + opponent identity for Player-Snapshots.
--
-- team_name alone cannot tell the Islanders from the Rangers: the NHL schedule
-- reports one placeName, "New York", for both, so it is fine for display and
-- for the postponed-game delete (which is all the pipeline uses it for) but it
-- is not a key. team_abbr is that key, and the archive used to carry one:
-- every Mongo row written through 2025-01-24 has it - smartscore-api's
-- change_team_name_to_abbrev backfilled the whole history - but the #113 port
-- dropped it, on the strength of a note in
-- 20261002_add_player_snapshots_table.sql that claimed no row had ever carried
-- one (wrong: 49,993 prod rows did). Rows from the 2025-01-25 parse-lambda
-- refactor through 2026-10-01 have no team identity at all, which orphaned the
-- team-level stats (tgpg, otga, otshga, home) sitting next to them.
--
-- Columns:
--   team_abbr        - the row's own team, NHL abbreviation (ARI, NYR, ...).
--   opponent_abbr    - the opponent that date, from that date's schedule.
--   opponent_name    - the opponent's schedule placeName, same convention as
--                      team_name (so "New York" stays ambiguous on purpose -
--                      the abbr is the disambiguator).
--
-- team_name keeps its meaning: the NHL schedule place name, and what the
-- postponed-game delete matches on. Nothing here rewrites it except the
-- backfill below filling in era-1 and era-2 rows that never had one.
--
-- No index is added: nothing reads these columns yet, and the PK already
-- covers a lookup by (date, player_id). Add one when a consumer needs it.
--
-- Backfilled by smartscore/scripts/backfill_team_identity.py (era 1 takes
-- team_abbr from Mongo, eras 2-3 scrape the NHL schedule/boxscore APIs; run
-- --scrape first, then --dry-run style report, then --apply).
--
-- All statements are idempotent: .github/workflows/deploy.yml pipes every file
-- in this directory through psql on each run with no migration history, and the
-- __ENV__ placeholder is substituted per environment (dev on a deploy-labelled
-- pull request, prod on a merge to main).

ALTER TABLE "Player-Snapshots-__ENV__"
    ADD COLUMN IF NOT EXISTS team_abbr TEXT,
    ADD COLUMN IF NOT EXISTS opponent_abbr TEXT,
    ADD COLUMN IF NOT EXISTS opponent_name TEXT;
