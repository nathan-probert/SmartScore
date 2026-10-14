"""Local raw store: one SQLite row per player per game.

This is the foundation the derived tables are computed from. Everything
season-to-date - ``gpg``, ``hgpg``, every future feature - is a window function
over these rows, so adding a feature means writing SQL, not re-crawling the API.

WHY THE RAW LAYER LIVES HERE RATHER THAN IN SUPABASE
----------------------------------------------------
``Player-Snapshots-backtrack-{ENV}`` holds one row per player per *date* with the
cumulative totals already applied. That is the right shape for the app and the
model, but it cannot answer "what happened in this game", so a bug in the
accumulator is invisible and unfixable without another crawl of the API (about
twenty minutes per season, cold).

These rows are the audit trail: ``gpg`` on any date is ``SUM(goals) / COUNT(*)``
over games strictly before it, and re-running that here reproduces the shipped
numbers exactly (``validate.py`` compares the result against the archive).

TWO SOURCE PAYLOADS, ONE TABLE
------------------------------
The NHL exposes the same facts through two endpoints with *different* fields:

===============  =========================================  ====================
field boxscore   extras                                    game-log extras
===============  =========================================  ====================
position         from                                     -
sweater_number   from                                     -
plus_minus       from                                     plus_minus
shots (sog)      from                                     shots
                 -                                        shorthanded goals/points
                 -                                        overtime goals
                 -                                        game-winning goals
                 -                                        power-play points
home/road        from                                     home/road flag
                 -                                        goalie: goals against,
                                                          shots against, shutouts,
                                                          decision, games started
===============  =========================================  ====================

Neither is a superset, so both are read and merged into one wide row keyed on
``(player_id, game_id)``. Which endpoint a column came from is recorded in the
``boxscore_fields`` / ``gamelog_fields`` columns, because "this is NULL" and "this
endpoint never carries this field" are different facts and worth telling apart.

Usage::

    uv run python smartscore/scripts/backtrack/local_store.py --build 20232024
    uv run python smartscore/scripts/backtrack/local_store.py --derive 20232024
    uv run python smartscore/scripts/backtrack/local_store.py --feeds 20232024
    uv run python smartscore/scripts/backtrack/local_store.py --publish 20232024
    uv run python smartscore/scripts/backtrack/local_store.py --publish-team 20232024
    uv run python smartscore/scripts/backtrack/local_store.py --stats
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from goalie_client import goalies_map  # noqa: E402
from lineup_reconstruct import reconstruct_game  # noqa: E402
from nhl_client import (  # noqa: E402
    DEFAULT_DELAY_SECONDS,
    PLAYOFFS,
    fetch_boxscore,
    fetch_game_log,
    fetch_shiftcharts,
    season_games,
)
from reconstruct import config  # noqa: E402 - lazy Supabase client, only needed by --publish
from team_map import to_place  # noqa: E402
from tims_client import tims_map  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "raw_nhl.sqlite"
CACHE_DIR = Path(__file__).resolve().parent / "cache"

# Which endpoint contributed which field, recorded per row. Kept as named constants
# because the lists are the payload contract: if the NHL adds or drops a field,
# these are what has to be revisited.
BOXSCORE_FIELDS = (
    "player_id,game_id,game_date,name,position,sweater_number,team_abbrev,home,"
    "goals,assists,points,shots,pim,toi,plus_minus,shifts,power_play_goals"
)

GAMELOG_FIELDS = (
    "gameId,gameDate,teamAbbrev,opponentAbbrev,homeRoadFlag,goals,assists,points,"
    "shots,pim,toi,plusMinus,shifts,powerPlayGoals,powerPlayPoints,shorthandedGoals,"
    "shorthandedPoints,otGoals,gameWinningGoals,"
    "goalsAgainst,shotsAgainst,savePctg,shutouts,decision,gamesStarted"
)

# One row per player-game. Every column is nullable because the two endpoints
# disagree on which fields exist; see the module docstring.
SCHEMA = """
CREATE TABLE IF NOT EXISTS player_games (
    player_id      INTEGER NOT NULL,
    game_id        INTEGER NOT NULL,
    season         TEXT,
    game_date      TEXT,
    name           TEXT,
    position       TEXT,
    sweater_number TEXT,

    team_abbrev      TEXT,
    opponent_abbrev  TEXT,
    home             INTEGER,
    -- Official goals scored by this player's team in this game, from the box
    -- score's awayTeam/homeTeam block. NOT the sum of this table's goals for the
    -- team: shootout game-winning goals and goalie empty-net goals count toward
    -- the team but appear in no skater's boxscore row, so summing rows
    -- undercounts. team goal totals must read this column.
    team_goals_for   INTEGER,

    goals                INTEGER,
    assists              INTEGER,
    points               INTEGER,
    shots                INTEGER,
    pim                  INTEGER,
    toi                  TEXT,
    plus_minus           INTEGER,
    shifts               INTEGER,
    power_play_goals     INTEGER,
    power_play_points    INTEGER,
    shorthanded_goals    INTEGER,
    shorthanded_points   INTEGER,
    ot_goals            INTEGER,
    game_winning_goals  INTEGER,

    -- Goalie-only appearance stats, from the game log's goalie block. NULL on
    -- skater rows (a skater's log entry carries none of these keys) and on
    -- dressed-but-unused backup rows (the box score lists them, the game log
    -- does not - about half of all position='G' rows). goals_against IS NOT
    -- NULL is therefore the appearance marker compute_goalie_features filters
    -- on; toi alone is not enough, because a backup's box-score toi is 00:00.
    goals_against  INTEGER,
    shots_against  INTEGER,
    -- Official saves, derived at merge time as round(savePctg * shots_against).
    -- NOT shots_against - goals_against: club-stats is internally saves-based
    -- and its season totals exceed sa - ga by a save or two (Stolarz 2022-23:
    -- saves 444, sa - ga 443), and save_pct must reproduce the saves-based
    -- percentage live's club-stats scrape carried. Per-game savePctg * shots
    -- reproduces those season totals exactly (verified vs club-stats).
    saves          INTEGER,
    shutouts       INTEGER,
    -- W / L / O only; O covers both overtime and shootout losses, the same
    -- lump the NHL's W-L-OTL record uses. No decision on a skater row.
    decision       TEXT,
    games_started  INTEGER,

    boxscore_fields TEXT,
    gamelog_fields  TEXT,

    PRIMARY KEY (player_id, game_id)
);

CREATE INDEX IF NOT EXISTS player_games_season_date_idx
    ON player_games (season, game_date);
-- five_gpg needs the previous five rows per player, so this index serves both
-- the cumulative and the rolling window without a sort.
CREATE INDEX IF NOT EXISTS player_games_player_date_idx
    ON player_games (player_id, season, game_date, game_id);

-- Derived features live in their own table rather than as generated columns, so
-- adding one is an INSERT into here plus a view that reads it, and the raw table
-- above never has to be rewritten.
--
-- Team-level features, one row per team per game.
--
-- These are TEAM attributes, not player stats: every player on a club carries the
-- same tgpg for a given date. The archive denormalises them onto Player-Snapshots,
-- which multiplies one number across ~21 rows per team-date and makes the table
-- awkward to reason about. They live here instead, keyed by team-date, and are
-- joined onto player rows when a caller needs them together.
--
-- Grain: (season, team_abbrev, game_date). One row per team appearance, so a team
-- that plays on a date has exactly one row for it.
--
-- tgpg - team's own goals FOR per game, season to date. Derived by summing the
--   goals of that team's players in each of its games, then dividing by games
--   played. Computed from player_games; no extra API call.
--
-- otga  - the OPPONENT's goals AGAINST per game, i.e. how many goals this team
--   scores ON THAT OPPONENT. This is an offence measure, not a defence one:
--   goals scored against the opponent are, by definition, the opponent's goals
--   against, so the two are the same quantity read from opposite ends.
--
--   Verified against the archive on 2024-02-17 (Anaheim at Toronto):
--   opponent goals against 165 / 52 games = 3.17308, archive says 3.17.
--   An earlier version of this table computed the team's OWN goals against
--   instead and matched almost nothing (107 of 16,301 rows) - the two are
--   close but unrelated, and reading the client's opponent_id argument as "my
--   team" is the trap here.
--
--   Note this is why the value is opponent-specific: two Anaheim players facing
--   different opponents on the same night carry different otga values, unlike
--   tgpg which is one number per team.
--
-- otshga - the opponent's shorthanded goals AGAINST per game: how often this
--   team has scored while the opponent was shorthanded, read off the opponent's
--   row like otga. A goal scored on the power play IS a shorthanded goal against
--   - the same event seen from opposite ends, exactly the otga argument - so
--   summing this team's power_play_goals over the games shared with that
--   opponent gives their shorthanded goals against directly, no separate
--   penalty-kill tally needed. Verified against
--   api.nhle.com/stats/rest/en/team/penaltykilltime for 2023-24: all 32 teams
--   match exactly on season shorthanded-goals-against and games played, once
--   the store includes playoff games (the endpoint's totals always did - the
--   earlier 16/16 split was entirely this store stopping at the regular
--   season).
--
--   First game of the season is NULL like tgpg/otga. Later games read 0.0 when
--   nothing has been scored: only gp = 0 is "no rate yet", a 0/4 is a rate, and
--   the archive stores zeros there (2,434 zero rows).
CREATE TABLE IF NOT EXISTS derived_team_stats (
    season     TEXT NOT NULL,
    team_abbrev TEXT NOT NULL,
    game_date  TEXT NOT NULL,
    game_id    INTEGER NOT NULL,
    tgpg       REAL,
    otga       REAL,
    otshga     REAL,
    PRIMARY KEY (season, team_abbrev, game_id)
);

CREATE INDEX IF NOT EXISTS derived_team_stats_date_idx
    ON derived_team_stats (season, game_date);

-- Per-player features, one row per player per game.
--
-- five_gpg follows get_five_gpg in smartscore_info_client in WINDOW (the last 5
-- games) but not in DIVISOR. The client divides by a hardcoded 5 regardless of how
-- many games the player has played, so a rookie with a goal in his only game reads
-- 0.2 - identical to a cold player with no goals in five, and a hot start diluted
-- into looking like a cold one. Here the divisor is the number of games in the
-- window, so that debut goal reads 1.0.
--
-- This is an intentional divergence from Player-Snapshots-{ENV}, which stores the
-- divided-by-5 version. It means early-season five_gpg will not match the archive,
-- and the archive's own values confirm the problem: 11 distinct values, all
-- multiples of 0.2, which is what a fixed divisor produces. Keeping the quirk would
-- make the column reproduce a bug rather than the intent.
--
-- ppg - power play goals per game: power play goals to date / games to date,
--   strictly-before like gpg, 0 on the pre-debut row. The single-season rate.
--
-- hgpg / hppg - the archive's 3-year windows per smartscore_info_client's
--   get_hgpg(years=3) / get_hppg(years=3): goals (power play goals) over the
--   season and the two before it, divided by games over the same window, filled
--   by compute_historic. The current season's share is strictly-before the
--   row's own game - the frame gpg was validated against the archive at 99.93% -
--   while the two prior seasons contribute their complete totals. Both prior
--   seasons must exist in this store: a partial window would divide by a
--   silently small denominator and read as a real rate, so 20212022 and
--   20222023 stay null until 20192020 and 20202021 are crawled (NULL means
--   "window unavailable", not "no games" - 0 is the no-games value).
CREATE TABLE IF NOT EXISTS derived_features (
    season    TEXT NOT NULL,
    player_id INTEGER NOT NULL,
    game_date TEXT NOT NULL,
    gpg       REAL,
    five_gpg  REAL,
    ppg       REAL,
    hgpg      REAL,
    hppg      REAL,
    PRIMARY KEY (season, player_id, game_date)
);

-- Actual line / power-play assignments reconstructed from shift charts, one
-- row per skater per game. See lineup_reconstruct for the method; the shift
-- cache is the raw source and this table is rebuildable from it. Separate
-- from player_games because the grain is deployment, not stats.
--
-- lineup_unit           F1-F4 / D1-D3 by that game's deployment rank, or NULL
-- lineup_position_group "F" / "D" (goalies are excluded despite appearing in
--                       shift data - see lineup_reconstruct)
-- pp_unit               PP1 / PP2, NULL for skaters under the PP-time floor
-- lineup_status         ACTUAL - reconstructed from what happened, unlike the
--                       live archive's PROJECTED morning-scrape value
CREATE TABLE IF NOT EXISTS player_lineups (
    game_id               INTEGER NOT NULL,
    player_id             INTEGER NOT NULL,
    lineup_unit           TEXT,
    lineup_position_group TEXT,
    pp_unit               TEXT,
    lineup_status         TEXT,
    PRIMARY KEY (player_id, game_id)
);

-- Challenge pick groups and graded outcomes, one row per listed player per
-- date, from the helper API's history endpoint (see tims_client). Stored here
-- as well as the JSON cache so the raw layer answers "who was in group N on
-- date D, and did they score" without re-parsing payload files - the SQL is
-- the re-parseable record, the cache is just what the fetch came back as.
--
-- group_id 1-3, the challenge's three daily groups. scored 1/0 is the helper's
-- own grading of whether that player scored that night; the publish's scored
-- label comes from the box score instead (it covers unlisted skaters too) -
-- this column keeps the helper's view for cross-checking.
CREATE TABLE IF NOT EXISTS tims_daily (
    season     TEXT NOT NULL,
    game_date  TEXT NOT NULL,
    player_id  INTEGER NOT NULL,
    group_id   INTEGER NOT NULL,
    scored     INTEGER,
    PRIMARY KEY (game_date, player_id)
);

CREATE INDEX IF NOT EXISTS tims_daily_season_date_idx
    ON tims_daily (season, game_date);

-- Projected starting goalies per team per date, from Rotowire's
-- projected-goalies table (see goalie_client). Stored for the same reason as
-- tims_daily: the publish reads these tables, and any future re-parse reads
-- SQL rather than the cache files.
CREATE TABLE IF NOT EXISTS goalies_daily (
    season      TEXT NOT NULL,
    game_date   TEXT NOT NULL,
    team_abbrev TEXT NOT NULL,
    goalie_name TEXT NOT NULL,
    status      TEXT,
    PRIMARY KEY (game_date, team_abbrev)
);

CREATE INDEX IF NOT EXISTS goalies_daily_season_date_idx
    ON goalies_daily (season, game_date);

-- Opponent-goalie season-to-date stats, one row per goalie appearance.
--
-- The archive's opp_goalie_gaa/save_pct/record/shutouts/games_played came from
-- same-day club-stats scrapes in live (service.get_goalie_stats_for_team),
-- which has no date parameter and only answers for the in-progress season.
-- These columns reconstruct the same numbers for history by walking each
-- goalie's game log forward - strictly-before the row's own game, the same
-- frame gpg uses, and the same as-of-morning view a live scrape would have
-- captured. See compute_goalie_features for the formulas.
--
-- TEAM-SCOPED, LIKE CLUB-STATS ITSELF
-- -----------------------------------
-- club-stats is fetched per team (/v1/club-stats/{team}/{season}/{type}), and a
-- traded goalie's totals are scoped to the club being asked about: Blackwood
-- 2024-25 reads 19 GP / 3.004 GAA on SJS and 37 GP / 2.328 on COL, not a
-- combined 56. Live therefore stored team-split numbers for a traded goalie,
-- so this window partitions on (player_id, team_abbrev) - a mid-season trade
-- resets the accumulation for the new club, matching what club-stats/COL would
-- have answered while he was starting there.
--
-- NULL, NOT 0, ON A CLUB DEBUT
-- -----------------------------
-- A goalie's first game with a club has no games before it. club-stats does
-- not list him until he appears, so live left every stat key None; a zero GAA
-- would claim a perfect rate that was never observed. This is the opposite of
-- gpg's debut-0 convention, and deliberately so - the archive stores 0 there
-- and NULL here.
--
-- Grain: (season, player_id, game_date). One row per appearance; the window
-- reads raw player_games, so a rebuild is --derive after --build, no crawl.
CREATE TABLE IF NOT EXISTS goalie_features (
    season       TEXT NOT NULL,
    player_id    INTEGER NOT NULL,
    team_abbrev  TEXT NOT NULL,
    game_date    TEXT NOT NULL,
    -- Goals-against average, official formula: 60 * GA / minutes played.
    -- NOT GA / games - a pulled goalie's partial night counts less than a
    -- full one. Verified: accumulating Vasilevskiy 2023-24 gives 2.89924,
    -- club-stats returns goalsAgainstAverage 2.899242.
    gaa          REAL,
    -- Saves / shots against over the window, a fraction (0.905, not 90.5),
    -- ratio of window totals rather than a mean of per-game percentages.
    -- saves is the official per-game count summed (see player_games.saves),
    -- not shots - goals: club-stats' savePercentage is saves/shots and its
    -- saves exceed sa - ga by a save or two on some goalies.
    save_pct     REAL,
    -- "W-L-OTL" text, the archive's own format; O covers both overtime and
    -- shootout losses, the same lump the NHL record uses.
    record       TEXT,
    shutouts     INTEGER,
    games_played INTEGER,
    PRIMARY KEY (season, player_id, game_date)
);

CREATE INDEX IF NOT EXISTS goalie_features_team_date_idx
    ON goalie_features (season, team_abbrev, game_date);
"""


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    # CREATE TABLE IF NOT EXISTS will not add a column to a database built before
    # that column existed, so the schema changes need an explicit ALTER. Additive
    # only - a rename or type change is a rebuild, and there is no such change
    # pending.
    # Additive column migrations for databases built before a column existed.
    # CREATE TABLE IF NOT EXISTS will not add one. Only additions are handled - a
    # rename or a type change needs a rebuild, and no such change is pending.
    additive_columns = (
        ("derived_features", "five_gpg", "REAL"),
        ("derived_features", "ppg", "REAL"),
        ("derived_features", "hgpg", "REAL"),
        ("derived_features", "hppg", "REAL"),
        ("player_games", "team_goals_for", "INTEGER"),
        ("player_games", "goals_against", "INTEGER"),
        ("player_games", "shots_against", "INTEGER"),
        ("player_games", "saves", "INTEGER"),
        ("player_games", "shutouts", "INTEGER"),
        ("player_games", "decision", "TEXT"),
        ("player_games", "games_started", "INTEGER"),
    )

    for table, column, definition in additive_columns:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")  # noqa: S608

    conn.commit()
    return conn


def _merge_boxscore(record):
    """One box score appearance -> a raw row."""
    return {
        "player_id": record["player_id"],
        "game_id": record["game_id"],
        "season": None,  # filled by the caller from the game list
        "game_date": record.get("game_date"),
        "name": record.get("name"),
        "position": record.get("position"),
        "sweater_number": record.get("sweater_number"),
        "team_abbrev": record.get("team_abbrev"),
        "opponent_abbrev": None,  # box score has no per-player opponent
        # Official team total, not this player's goals. See the SCHEMA comment on
        # team_goals_for - summing per-player goals loses shootout winners and
        # goalie ENGs.
        "team_goals_for": record.get("team_goals_for"),
        "home": 1 if record.get("home") else 0,
        "goals": record.get("goals"),
        "assists": record.get("assists"),
        "points": record.get("points"),
        "shots": record.get("shots"),
        "pim": record.get("pim"),
        "toi": record.get("toi"),
        "plus_minus": record.get("plus_minus"),
        "shifts": record.get("shifts"),
        "power_play_goals": record.get("power_play_goals"),
        "power_play_points": None,
        "shorthanded_goals": None,
        "shorthanded_points": None,
        "ot_goals": None,
        "game_winning_goals": None,
        # Goalie block: game-log only; NULL here so a box-score-first insert
        # does not shadow the merge that fills them.
        "goals_against": None,
        "shots_against": None,
        "saves": None,
        "shutouts": None,
        "decision": None,
        "games_started": None,
        "boxscore_fields": BOXSCORE_FIELDS,
        "gamelog_fields": None,
    }


def _merge_gamelog(game, season):
    """One game-log entry -> a raw row."""
    return {
        "player_id": None,  # filled by the caller
        "game_id": game.get("gameId"),
        "season": season,
        "game_date": game.get("gameDate"),
        "name": None,  # the game log carries club names, not the player's
        "position": None,
        "sweater_number": None,
        "team_abbrev": game.get("teamAbbrev"),
        "opponent_abbrev": game.get("opponentAbbrev"),
        "home": 1 if game.get("homeRoadFlag") == "H" else 0,
        "goals": game.get("goals"),
        "assists": game.get("assists"),
        "points": game.get("points"),
        "shots": game.get("shots"),
        "pim": game.get("pim"),
        "toi": game.get("toi"),
        "plus_minus": game.get("plusMinus"),
        "shifts": game.get("shifts"),
        "power_play_goals": game.get("powerPlayGoals"),
        "power_play_points": game.get("powerPlayPoints"),
        "shorthanded_goals": game.get("shorthandedGoals"),
        "shorthanded_points": game.get("shorthandedPoints"),
        "ot_goals": game.get("otGoals"),
        "game_winning_goals": game.get("gameWinningGoals"),
        # Goalie block: present only on a goalie's log entry, so a skater's
        # merge writes NULLs here and COALESCE leaves the box-score row alone.
        "goals_against": game.get("goalsAgainst"),
        "shots_against": game.get("shotsAgainst"),
        # Official per-game saves: savePctg is saves/shots at 6dp, so the
        # round-trip lands on the integer saves exactly (max error sa * 5e-7,
        # far from a .5 boundary). This is the number club-stats sums - see
        # the SCHEMA comment on saves.
        "saves": (
            round(game["savePctg"] * game["shotsAgainst"])
            if game.get("savePctg") is not None and game.get("shotsAgainst")
            else None
        ),
        "shutouts": game.get("shutouts"),
        "decision": game.get("decision"),
        "games_started": game.get("gamesStarted"),
        "boxscore_fields": None,
        "gamelog_fields": GAMELOG_FIELDS,
    }


def _upsert(conn, rows):
    """Merge rows into player_games, filling gaps rather than replacing.

    NOT ``INSERT OR REPLACE``. The two endpoints carry complementary fields - the
    box score has position/sweater_number, the game log has shorthanded and OT
    goals - so replacing on the second pass throws away the first pass's columns.
    That is not hypothetical: with REPLACE, position and sweater_number survived
    only for rows whose game logs were empty and so never got overwritten, leaving
    95% of rows missing them.

    Each column is written with COALESCE(existing, incoming), so whichever payload
    populated a field first keeps it and the second only fills what is NULL.
    """
    if not rows:
        return 0

    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)

    # S608 (suppressed on the statement below): the identifiers interpolated here
    # are rows[0].keys(), i.e. the keys of this module's own literal row dicts in
    # _merge_boxscore / _merge_gamelog. They never come from input, and every value
    # is a bound parameter. Validated against SCHEMA's column list would be stricter
    # but the dicts are defined two functions above and cannot drift from SCHEMA
    # without a failing INSERT.
    conflict_columns = [c for c in columns if c not in ("player_id", "game_id")]
    assignments = ", ".join(f"{c} = COALESCE(player_games.{c}, excluded.{c})" for c in conflict_columns)
    statement = (
        f"INSERT INTO player_games ({', '.join(columns)}) VALUES ({placeholders}) "  # noqa: S608
        f"ON CONFLICT(player_id, game_id) DO UPDATE SET {assignments}"
    )

    conn.executemany(statement, [tuple(r[c] for c in columns) for r in rows])
    conn.commit()

    return len(rows)


def build(season, db_path=DB_PATH, delay_seconds=DEFAULT_DELAY_SECONDS):
    """Populate raw rows for one season from the cache, fetching on a cache miss.

    Reads the cache rather than the network when it is warm, so a rebuild after a
    schema change is instant. A cold cache fetches as it goes - schedule window,
    box scores, and per-player game logs - and the default delay is the client's
    throttle, because a cold run is over a thousand game-log requests and an
    unthrottled burst invites rate limiting.
    """
    conn = connect(db_path)
    games = season_games(season, delay_seconds=delay_seconds)
    game_season = {g["id"]: season for g in games}
    written = 0

    for game in games:
        records = fetch_boxscore(game["id"], cache_dir=CACHE_DIR)
        # Shift charts alongside the box score: same per-game grain, and a
        # cold run fetches them here so the lineup pass below is cache-only.
        fetch_shiftcharts(game["id"], delay_seconds=delay_seconds, cache_dir=CACHE_DIR)

        rows = []
        for record in records:
            row = _merge_boxscore(record)
            row["season"] = game_season.get(game["id"], season)
            rows.append(row)

        written += _upsert(conn, rows)

    print(f"box scores -> {written} row(s) for {season}")

    log_written = merge_gamelogs(conn, season, delay_seconds=delay_seconds)

    lineup_written = compute_lineups(conn, season, delay_seconds=delay_seconds)
    print(f"lineups -> {lineup_written} row(s) reconstructed for {season}")

    feed_written = ingest_challenge_feeds(conn, season, delay_seconds=delay_seconds)
    print(f"feeds -> {feed_written} row(s) ingested for {season}")

    conn.close()
    return written, log_written


def merge_gamelogs(conn, season, delay_seconds=DEFAULT_DELAY_SECONDS):
    """Second build pass: the game log supplies the stat families the box omits.

    Progress is logged every 100 players - this loop is ~1,000 commits, so a
    silent run is indistinguishable from a hung one, and that visibility gap is
    what earlier made a build impossible to monitor.

    Playoffs are a separate feed under the same season id (gameType 3), so the
    players who appeared in playoff box scores need that log fetched too -
    otherwise their playoff rows miss the gamelog stat families and the
    strictly-before windows over them are built on partial rows. Everyone else's
    request would come back empty and be cached as empty.

    Standalone (``--gamelogs``) as well as part of ``build``: a schema or
    formula change to the goalie columns (saves, save_pct) only needs this
    pass plus ``--derive`` - box scores, lineups, and feeds are unaffected.
    """
    playoff_ids = {
        r[0]
        for r in conn.execute("SELECT DISTINCT player_id, game_id FROM player_games WHERE season = ?", (season,))
        # Game ids carry the type as digits 5-6: 202303xxxx is a 2024 playoff
        # game, 202302xxxx a regular-season one.
        if str(r[1])[4:6] == "03"
    }

    log_written = 0
    player_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT player_id FROM player_games WHERE season = ? ORDER BY player_id",
            (season,),
        )
    ]

    for index, player_id in enumerate(player_ids, start=1):
        game_log = fetch_game_log(player_id, season, delay_seconds=delay_seconds)

        rows = []
        for game in game_log:
            row = _merge_gamelog(game, season)
            row["player_id"] = player_id
            rows.append(row)

        if player_id in playoff_ids:
            for game in fetch_game_log(player_id, season, game_type=PLAYOFFS, delay_seconds=delay_seconds):
                row = _merge_gamelog(game, season)
                row["player_id"] = player_id
                rows.append(row)

        log_written += _upsert(conn, rows)

        if index % 100 == 0:
            print(f"  game logs: {index}/{len(player_ids)} players, {log_written} row(s) merged", flush=True)

    print(f"game logs -> {log_written} row(s) merged for {season}")
    return log_written


def compute_lineups(conn, season, delay_seconds=DEFAULT_DELAY_SECONDS):
    """Fill player_lineups for one season from the shift-chart cache.

    Reads every game the raw store has rows for. fetch_shiftcharts answers
    from disk once a build has crawled it, so this is pure local computation
    on a warm cache - rerunnable after a reconstruction fix without touching
    the network.
    """
    conn.execute(
        "DELETE FROM player_lineups WHERE game_id IN (SELECT DISTINCT game_id FROM player_games WHERE season = ?)",
        (season,),
    )

    game_ids = [
        r[0]
        for r in conn.execute("SELECT DISTINCT game_id FROM player_games WHERE season = ? ORDER BY game_id", (season,))
    ]

    written = 0
    for game_id in game_ids:
        shifts = fetch_shiftcharts(game_id, delay_seconds=delay_seconds, cache_dir=CACHE_DIR)
        if not shifts:
            continue

        positions = {
            r["player_id"]: r["position"]
            for r in conn.execute("SELECT player_id, position FROM player_games WHERE game_id = ?", (game_id,))
        }

        assignments = reconstruct_game(shifts, positions)
        rows = [
            {
                "game_id": game_id,
                "player_id": player_id,
                "lineup_unit": entry["lineup_unit"],
                "lineup_position_group": entry["lineup_position_group"],
                "pp_unit": entry["pp_unit"],
                "lineup_status": entry["lineup_status"],
            }
            for player_id, entry in assignments.items()
        ]

        if rows:
            conn.executemany(
                "INSERT INTO player_lineups (game_id, player_id, lineup_unit, lineup_position_group,"
                " pp_unit, lineup_status) VALUES (:game_id, :player_id, :lineup_unit,"
                " :lineup_position_group, :pp_unit, :lineup_status)"
                " ON CONFLICT(player_id, game_id) DO UPDATE SET"
                " lineup_unit = excluded.lineup_unit,"
                " lineup_position_group = excluded.lineup_position_group,"
                " pp_unit = excluded.pp_unit,"
                " lineup_status = excluded.lineup_status",
                rows,
            )
            conn.commit()
            written += len(rows)

    return written


def ingest_challenge_feeds(conn, season, delay_seconds=DEFAULT_DELAY_SECONDS):
    """Fill tims_daily and goalies_daily for one season from the helper APIs.

    Reads through tims_map/goalies_map, which answer from the JSON cache once a
    build has crawled it, so a re-ingest on a warm cache is pure local SQL.
    Rebuild semantics match compute_lineups: the season's rows are deleted and
    re-inserted, so a source-shape change can be re-ingested without leftovers.
    """
    conn.execute("DELETE FROM tims_daily WHERE season = ?", (season,))
    conn.execute("DELETE FROM goalies_daily WHERE season = ?", (season,))

    written = 0

    for date_str, players in tims_map(season, delay_seconds=delay_seconds).items():
        conn.executemany(
            "INSERT INTO tims_daily (season, game_date, player_id, group_id, scored)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(game_date, player_id) DO UPDATE SET"
            " group_id = excluded.group_id, scored = excluded.scored",
            [(season, date_str, player_id, entry["group"], entry["scored"]) for player_id, entry in players.items()],
        )
        written += len(players)

    for date_str, teams in goalies_map(season, delay_seconds=delay_seconds).items():
        conn.executemany(
            "INSERT INTO goalies_daily (season, game_date, team_abbrev, goalie_name, status)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(game_date, team_abbrev) DO UPDATE SET"
            " goalie_name = excluded.goalie_name, status = excluded.status",
            [(season, date_str, team, info["name"], info["status"]) for team, info in teams.items()],
        )
        written += len(teams)

    conn.commit()
    return written


def compute_derived(conn, season):
    """Fill derived_features.gpg/five_gpg/ppg for one season.

    Written as a window function over the raw rows so the arithmetic is visible
    and reviewable: goals and games accumulated STRICTLY BEFORE the row's own game
    (RANGE ... PRECEDING with 1 PRECEDING), then divided. A row therefore reads as
    "entering that game", which is what a pre-game pick needs and what the archive
    stores. ppg reads the same frame off power_play_goals - the single-season
    rate, not the archive's 3-year hppg (see the SCHEMA comment).

    A player's first game has no games before it, and 0/0 normalises to 0 rather
    than NULL - the archive stores 0 on pre-debut rows (verified against a player
    who scored in his debut, whose stored gpg there still reads 0), so 0 keeps
    this table a drop-in replacement. NULL would only ever appear for rows written
    by an older build; it means "recompute with --derive".
    """
    conn.execute("DELETE FROM derived_features WHERE season = ?", (season,))

    conn.execute(
        """
        INSERT INTO derived_features (season, player_id, game_date, gpg, five_gpg, ppg)
        SELECT
            season,
            player_id,
            game_date,
            -- gp_to_date = 0 on a player's first game: 0, not NULL, matching the
            -- archive's pre-debut rows (see the docstring above).
            CASE
                WHEN prev_gp = 0 THEN 0
                ELSE 1.0 * prev_goals / prev_gp
            END,
            -- Goals per game over the last 5 games, divided by the number of
            -- games actually in that window (the w5 frame caps it at 5).
            --
            -- smartscore_info_client's get_five_gpg divides by a hardcoded 5
            -- regardless of how many games the player has played, so a rookie
            -- with a goal in his only game reads 0.2 and a cold player with no
            -- goals in five also reads 0.0 - two opposite situations collapsed
            -- onto one value, and a hot start diluted into looking like a cold
            -- one. Divided by games played instead, that debut goal reads 1.0.
            --
            -- This is a deliberate divergence from the archive, so early-season
            -- rows will not match Player-Snapshots-{ENV}. Correctness wins: a
            -- feature fed to the model should not be deflated by a player's lack
            -- of games.
            --
            -- On a player's first game there is no window yet, and that reads 0
            -- rather than NULL - matching the archive's pre-debut rows.
            CASE
                WHEN games_before = 0 THEN 0
                ELSE 1.0 * goals_last5 / games_before
            END,
            -- Power play goals per game over the same strictly-before frame,
            -- 0 on the pre-debut row like gpg. Single-season by construction -
            -- the archive's 3-year hppg is compute_historic's column, written
            -- right after this INSERT.
            CASE
                WHEN prev_gp = 0 THEN 0
                ELSE 1.0 * prev_pp / prev_gp
            END
        FROM (
            SELECT
                season,
                player_id,
                game_date,
                SUM(COALESCE(goals, 0)) OVER w  AS goals_through,
                COUNT(*)              OVER w  AS gp_through,
                SUM(COALESCE(goals, 0)) OVER w2 AS prev_goals,
                COUNT(*)              OVER w2 AS prev_gp,
                SUM(COALESCE(power_play_goals, 0)) OVER w2 AS prev_pp,
                -- Rolling window over the five games strictly before this one.
                -- More than five rows once a player is established; the frame is
                -- a row count so "last 5 games" is exactly that, not "games in
                -- the last 5 days".
                SUM(COALESCE(goals, 0)) OVER w5 AS goals_last5,
                COUNT(*)              OVER w5 AS games_before
            FROM player_games
            WHERE season = ?
            WINDOW
                -- ROWS, not RANGE: RANGE works on value ranges and cannot express
                -- a row offset once the ordering has two expressions (SQLite
                -- rejects an offset frame with a multi-column ORDER BY).
                --
                -- w2 is UNBOUNDED PRECEDING TO 1 PRECEDING, i.e. every row
                -- strictly before this one. It is NOT "1 PRECEDING TO 1
                -- PRECEDING" - that frame spans a single row, so COUNT(*) would
                -- return 1 and gpg would collapse to 0 or 1.
                w  AS (PARTITION BY season, player_id ORDER BY game_date, game_id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
                w2 AS (PARTITION BY season, player_id ORDER BY game_date, game_id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
                w5 AS (PARTITION BY season, player_id ORDER BY game_date, game_id
                       ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING)
        )
        """,
        (season,),
    )
    conn.commit()

    # hgpg/hppg read raw rows from the two prior seasons as well, so they cannot
    # ride the single-season INSERT above - see compute_historic.
    compute_historic(conn, season)

    # Goalie stats are a separate table off the same raw rows - see
    # compute_goalie_features. Called here so --derive covers them and a
    # publish after a derive cannot ship NULL goalie columns by accident.
    compute_goalie_features(conn, season)


def _previous_season(season):
    """One season back by id arithmetic: '20232024' -> '20222023'.

    Mirrors get_previous_season in smartscore_info_client, which walks the
    seasonTotals entries the same way (start year - 1, end year - 1).
    """
    start = int(season[:4])
    end = int(season[4:8])
    return f"{start - 1:04d}{end - 1:04d}"


def compute_historic(conn, season):
    """Fill derived_features.hgpg/hppg for one season: the archive's 3-year windows.

    get_hgpg(years=3) in smartscore_info_client sums goals (get_hppg: power play
    goals) and gamesPlayed over the seasonTotals entries for the season and the
    two before it, then divides ONCE - a ratio of window totals, not an average
    of per-season rates. The same shape here:

    * the two prior seasons contribute their complete totals (SUM/COUNT over
      player_games for those season ids, any date);
    * the current season contributes games strictly BEFORE this row's own game -
      the frame gpg was validated against the archive at 99.93%, and landing
      scraped on a given morning held that season's totals only through the
      previous day's games anyway.

    Both prior seasons must exist in the store before a window is computed. A
    partial window (say 20222023 missing 20202021) would divide by a smaller
    denominator than the archive did and read as a real - wrong - rate, so when
    a prior season is absent the columns stay NULL: "window unavailable".
    0 is reserved for the archive's no-games convention (get_hgpg returns 0.0
    when games == 0), which is what a rookie's debut row gets.

    Order matters only in that this runs after compute_derived has INSERTed the
    rows; it reads raw player_games, not derived_features, so the prior seasons
    need only their raw rows - their own derived pass is irrelevant.
    """
    window = [_previous_season(season), _previous_season(_previous_season(season))]
    crawled = {r[0] for r in conn.execute("SELECT DISTINCT season FROM player_games")}

    if not set(window) <= crawled:
        # NULL already (compute_derived's INSERT omits the columns), but be
        # explicit so a re-run after a schema change cannot leave stale values.
        conn.execute("UPDATE derived_features SET hgpg = NULL, hppg = NULL WHERE season = ?", (season,))
        conn.commit()
        print(f"{season}: hgpg/hppg left null - prior season(s) not in store: {sorted(set(window) - crawled)}")
        return 0

    prior = {
        r["player_id"]: (r["goals"], r["gp"], r["pp"])
        for r in conn.execute(
            """
            SELECT player_id,
                   SUM(COALESCE(goals, 0))              AS goals,
                   COUNT(*)                             AS gp,
                   SUM(COALESCE(power_play_goals, 0))   AS pp
            FROM player_games
            WHERE season IN (?, ?)
            GROUP BY player_id
            """,
            (window[0], window[1]),
        )
    }

    # Same strictly-before frame as compute_derived's w2: every row of this
    # player's season before this one. One UPDATE per (player, date) row of
    # derived_features, which is exactly the grain player_games has.
    updates = []
    for r in conn.execute(
        """
        SELECT player_id, game_date,
               SUM(COALESCE(goals, 0))            OVER w AS goals_before,
               COUNT(*)                           OVER w AS gp_before,
               SUM(COALESCE(power_play_goals, 0)) OVER w AS pp_before
        FROM player_games
        WHERE season = ?
        WINDOW w AS (PARTITION BY player_id ORDER BY game_date, game_id
                     ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
        """,
        (season,),
    ):
        prior_goals, prior_gp, prior_pp = prior.get(r["player_id"], (0, 0, 0))
        # SUM over an empty frame (a player's first game of the season) is NULL,
        # COUNT is 0 - coalesce so the debut adds the prior seasons, not NULL.
        gp = (r["gp_before"] or 0) + prior_gp
        goals = (r["goals_before"] or 0) + prior_goals
        pp = (r["pp_before"] or 0) + prior_pp
        updates.append(
            (
                1.0 * goals / gp if gp else 0.0,
                1.0 * pp / gp if gp else 0.0,
                season,
                r["player_id"],
                r["game_date"],
            )
        )

    conn.executemany(
        "UPDATE derived_features SET hgpg = ?, hppg = ? WHERE season = ? AND player_id = ? AND game_date = ?",
        updates,
    )
    conn.commit()
    print(f"{season}: hgpg/hppg computed for {len(updates)} row(s) over window {window + [season]}")
    return len(updates)


def compute_goalie_features(conn, season):
    """Fill goalie_features for one season: as-of-date opponent-goalie stats.

    Reconstructs what live's same-day ``club-stats/{team}/now`` scrape would
    have answered on each date, from the game logs already cached on disk. The
    window is strictly-before the row's own game (the frame gpg was validated
    on), so a row reads "entering that game" - the as-of-morning view a live
    scrape captured. ``_merge_gamelog`` must have run first (via --build) for
    the raw goalie columns to be populated.

    Formulas, matching club-stats' own fields at full precision:

    * gaa       - ``60 * GA / minutes``, the official per-60 formula. NOT
      ``GA / games``: a goalie pulled halfway through a 6-goal night has played
      30 minutes, and counting that as a full game understates his rate.
      Verified against club-stats: accumulating Vasilevskiy's 2023-24 log gives
      2.89924; the endpoint returns goalsAgainstAverage 2.899242.
    * save_pct  - ``saves / shots`` as a ratio of window totals (0.905, not
      90.5), not a mean of per-game percentages. saves is the official
      per-game figure (``savePctg * shots_against`` rounded, merged into its
      own column), NOT ``shots_against - goals_against``: club-stats sums its
      saves field, which can exceed sa - ga by a save or two (Stolarz
      2022-23: saves 444 vs sa - ga 443), and savePercentage is
      saves/shots. The sa - ga shortcut reads ~0.002 low on such goalies and
      fails a club-stats comparison; validated across all four seasons.
    * record    - ``W-L-OTL`` text; decision O covers both overtime and
      shootout losses, the same lump the NHL's record uses.
    * shutouts / games_played - window sums / counts.

    The window partitions on ``(player_id, team_abbrev)`` because club-stats
    is team-scoped: a traded goalie's numbers restart with his new club (see
    the SCHEMA comment). Rows with ``goals_against IS NULL`` - dressed backups
    who never played - are excluded entirely; counting them would inflate
    games_played with nights the goalie never tended net.

    NULL on a club debut, not 0: club-stats does not list a goalie until he
    appears for that club, so live left the stat keys None there. Opposite of
    gpg's debut-0 convention, and deliberately so. Once listed, a 0-shot
    window reads 0.0 for save_pct - club-stats pairs shotsAgainst 0 with
    savePercentage 0.0, not null.
    """
    conn.execute("DELETE FROM goalie_features WHERE season = ?", (season,))

    # Every appearance must have merged its official saves: a NULL here with
    # shots_against > 0 means the game-log re-merge has not run since the
    # column was added, and save_pct would silently read 0 (or drift low).
    stale = conn.execute(
        """
        SELECT COUNT(*) FROM player_games
        WHERE season = ? AND position = 'G' AND goals_against IS NOT NULL
          AND shots_against > 0 AND saves IS NULL
        """,
        (season,),
    ).fetchone()[0]
    if stale:
        raise RuntimeError(
            f"{season}: {stale} appearance row(s) have shots_against but no saves - "
            "re-run the game-log merge (--gamelogs or --build) before deriving"
        )

    conn.execute(
        """
        INSERT INTO goalie_features (
            season, player_id, team_abbrev, game_date,
            gaa, save_pct, record, shutouts, games_played
        )
        SELECT
            season,
            player_id,
            team_abbrev,
            game_date,
            -- NULL on a club debut (prev_gp = 0): not listed, not zero.
            CASE WHEN prev_gp = 0 OR prev_toi_sec = 0 THEN NULL
                 ELSE 3600.0 * prev_ga / prev_toi_sec END,
            -- NULL only on a club debut (not listed at all). Once listed, a
            -- window with 0 shots reads 0.0 - club-stats returns
            -- savePercentage 0.0 alongside shotsAgainst 0 (seen in
            -- 2022-2023 TOR: one 1:10 stint, no shots), not null.
            CASE WHEN prev_gp = 0 THEN NULL
                 WHEN prev_sa = 0 THEN 0.0
                 ELSE 1.0 * prev_saves / prev_sa END,
            CASE WHEN prev_gp = 0 THEN NULL
                 ELSE prev_w || '-' || prev_l || '-' || prev_o END,
            CASE WHEN prev_gp = 0 THEN NULL ELSE prev_so END,
            CASE WHEN prev_gp = 0 THEN NULL ELSE prev_gp END
        FROM (
            SELECT
                season, player_id, team_abbrev, game_date,
                SUM(COALESCE(goals_against, 0)) OVER w AS prev_ga,
                SUM(COALESCE(shots_against, 0)) OVER w AS prev_sa,
                SUM(COALESCE(saves, 0))         OVER w AS prev_saves,
                SUM(COALESCE(shutouts, 0))      OVER w AS prev_so,
                COUNT(*)                         OVER w AS prev_gp,
                SUM(CASE WHEN decision = 'W' THEN 1 ELSE 0 END) OVER w AS prev_w,
                SUM(CASE WHEN decision = 'L' THEN 1 ELSE 0 END) OVER w AS prev_l,
                SUM(CASE WHEN decision = 'O' THEN 1 ELSE 0 END) OVER w AS prev_o,
                -- toi is "MM:SS" from the game log; club-stats reports TOI in
                -- seconds and GAA off it, so parse once here. Every appearance
                -- row carries toi (the WHERE below is goals_against IS NOT
                -- NULL, which is a real log entry, never a dressed backup).
                SUM(
                    CAST(substr(toi, 1, instr(toi, ':') - 1) AS INTEGER) * 60
                    + CAST(substr(toi, instr(toi, ':') + 1) AS INTEGER)
                ) OVER w AS prev_toi_sec
            FROM player_games
            WHERE season = ?
              AND position = 'G'
              AND goals_against IS NOT NULL
            WINDOW w AS (
                PARTITION BY season, player_id, team_abbrev
                ORDER BY game_date, game_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            )
        )
        """,
        (season,),
    )
    conn.commit()

    written = conn.execute("SELECT COUNT(*) FROM goalie_features WHERE season = ?", (season,)).fetchone()[0]
    print(f"{season}: goalie_features computed for {written} appearance row(s)")
    return written


def compute_derived_team(conn, season):
    """Fill derived_team_stats for one season: tgpg, otga and otshga per team-game.

    Built in one pass because all three rates fall out of a single per-(team, game)
    collapse of player_games, once each row knows what its opponent scored:

        NSH  3   against TBL  5
        TBL  5   against NSH  3

    tgpg - own goals for, over this team's games played before this one.
    otga - the OPPONENT's goals against, over the opponent's games before this
           one. Goals scored against an opponent equal that opponent's goals
           against, so summing the opponent's own goals_for in those shared games
           gives the number directly - no separate concessions tally needed.
    otshga - the OPPONENT's shorthanded goals against, over the opponent's games
           before this one. A power play goal this team scored is a goal the
           opponent allowed while shorthanded - the same mirror as otga, so the
           opponent's shorthanded goals against are this team's power_play_goals
           summed over the shared games.

    All use the strictly-before frame so a row for game N carries the rate
    entering it, matching gpg. A team's first game of the season is NULL, not 0:
    a zero would claim a team averages no goals per game, which the archive
    does emit (Winnipeg, Anaheim, Dallas and Carolina all show tgpg = 0 at the
    start of the current season). otshga's zeros after game 1 stay 0.0 - see the
    SCHEMA comment.
    """
    conn.execute("DELETE FROM derived_team_stats WHERE season = ?", (season,))

    # Collapse player_games to one row per (team, game), then attach the
    # opponent's tally for that game. The self-join is 1:1 - every game has
    # exactly two teams, which is asserted by the count below rather than assumed.
    #
    # goals_for comes from team_goals_for (the box score's official team score),
    # NOT from SUM(goals). Summing player rows undercounts: a shootout
    # game-winning goal and a goalie empty-net goal both count toward the team but
    # appear in no skater's boxscore row. PIT 2023-12-13 scored 4 and its skaters
    # account for 3; that missing goal is a shootout winner. MAX is safe because
    # every player row for a team-game carries the same official value.
    conn.execute(
        """
        CREATE TEMP TABLE team_game_goals AS
        SELECT season, game_id, team_abbrev, game_date,
               MAX(team_goals_for) AS goals_for,
               -- The team's own power play goals. Rows are box-score backed
               -- (team_goals_for IS NOT NULL filters gamelog-only rows), and a
               -- power play goal always has its scorer in the box score, so no
               -- PP goal is missed by that filter.
               SUM(COALESCE(power_play_goals, 0)) AS pp_goals
        FROM player_games
        WHERE season = ? AND team_abbrev IS NOT NULL AND team_goals_for IS NOT NULL
        GROUP BY season, game_id, team_abbrev
        """,
        (season,),
    )

    orphans = conn.execute(
        "SELECT COUNT(*) FROM team_game_goals t WHERE NOT EXISTS ("
        "  SELECT 1 FROM team_game_goals o"
        "  WHERE o.game_id = t.game_id AND o.team_abbrev <> t.team_abbrev)",
    ).fetchone()[0]

    if orphans:
        # Silently dropping these would make otga/otshga wrong for the affected
        # games rather than absent, so it is worth refusing.
        raise ValueError(f"{orphans} team-game row(s) have no opponent; otga would be wrong")

    # Step 2: cumulative per team. gf/gp drive tgpg; ga (goals conceded, i.e. the
    # sum of what opponents scored) drives the team's OWN goals-against rate; shga
    # (the opponents' power play goals, i.e. what opponents scored while this team
    # was shorthanded) drives the team's own shorthanded-goals-against total. All
    # use the strictly-before frame so a row for game N carries the rate entering
    # it, matching gpg.
    conn.execute(
        """
        CREATE TEMP TABLE team_cum AS
        SELECT
            t.season,
            t.game_id,
            t.team_abbrev,
            t.game_date,
            SUM(t.goals_for) OVER w AS gf,
            COUNT(*)           OVER w AS gp,
            SUM(o.goals_for)   OVER w AS ga,
            SUM(o.pp_goals)    OVER w AS shga
        FROM team_game_goals t
        JOIN team_game_goals o
          ON o.season = t.season
         AND o.game_id = t.game_id
         AND o.team_abbrev <> t.team_abbrev
        WINDOW
            -- Ordered by (game_date, game_id) rather than date alone so the
            -- ordering is total and deterministic.
            w AS (PARTITION BY t.season, t.team_abbrev ORDER BY t.game_date, t.game_id
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
        """
    )

    # Step 3: otga and otshga are OPPONENT-relative, so they are read off the
    # other team's row for the same game - not off this team's own ga/shga. Those
    # are different quantities: a team's ga is what IT conceded, while otga is what
    # the club it is facing concedes, i.e. this team's scoring rate against that
    # specific opponent. Same mirror for otshga: o.shga is what the opponent
    # allowed while shorthanded, which is this team's power play scoring against
    # them.
    #
    # The guards differ on purpose. otga nulls when the numerator is 0 (standing
    # behaviour, unchanged). otshga nulls only when o.gp = 0 - a first game has no
    # rate yet - because 0 shorthanded goals against over games played is a real,
    # common value and the archive stores it (2,434 zero rows).
    conn.execute(
        """
        INSERT INTO derived_team_stats (season, team_abbrev, game_date, game_id, tgpg, otga, otshga)
        SELECT
            c.season,
            c.team_abbrev,
            c.game_date,
            c.game_id,
            CASE WHEN c.gp = 0 THEN NULL ELSE 1.0 * c.gf / c.gp END,
            CASE WHEN o.ga = 0 THEN NULL ELSE 1.0 * o.ga / o.gp END,
            CASE WHEN o.gp = 0 THEN NULL ELSE 1.0 * o.shga / o.gp END
        FROM team_cum c
        LEFT JOIN team_cum o
          ON o.season = c.season
         AND o.game_id = c.game_id
         AND o.team_abbrev <> c.team_abbrev
        """
    )
    conn.commit()
    conn.execute("DROP TABLE IF EXISTS team_cum")
    conn.execute("DROP TABLE IF EXISTS team_game_goals")


def stats(db_path=DB_PATH):
    conn = connect(db_path)

    for row in conn.execute(
        """
        SELECT season,
               COUNT(*)                          AS rows,
               COUNT(DISTINCT player_id)         AS players,
               COUNT(DISTINCT game_id)           AS games,
               MIN(game_date)                    AS first_date,
               MAX(game_date)                    AS last_date,
               SUM(CASE WHEN position <> 'G' THEN 1 ELSE 0 END) AS skaters,
               SUM(CASE WHEN position = 'G' THEN 1 ELSE 0 END)   AS goalies,
               -- The only real coverage gap: a SKATER row the game-log merge
               -- never touched. Goalie rows always show shorthanded_goals NULL
               -- (their log entries carry saves/decision, not the skater stat
               -- families) and are expected - goalies are excluded at publish.
               SUM(CASE WHEN position <> 'G' AND shorthanded_goals IS NULL THEN 1 ELSE 0 END) AS skater_missing_log
        FROM player_games
        GROUP BY season
        ORDER BY season
        """
    ):
        print(
            f"{row['season']}: {row['rows']} rows, {row['players']} players, {row['games']} games "
            f"({row['first_date']} .. {row['last_date']}) "
            f"skaters={row['skaters']} goalies={row['goalies']} "
            f"skater rows missing log merge={row['skater_missing_log']}"
        )

    for row in conn.execute(
        "SELECT season, COUNT(*) AS n, SUM(ppg IS NULL) AS null_ppg,"
        " SUM(hgpg IS NULL) AS null_hgpg"
        " FROM derived_features GROUP BY season ORDER BY season"
    ):
        print(
            f"derived {row['season']}: {row['n']} feature row(s), "
            f"null ppg={row['null_ppg']} null hgpg={row['null_hgpg']}"
        )

    for row in conn.execute(
        """
        SELECT season, COUNT(*) AS rows, COUNT(DISTINCT team_abbrev) AS teams,
               SUM(tgpg IS NULL) AS null_tgpg, SUM(otga IS NULL) AS null_otga,
               SUM(otshga IS NULL) AS null_otshga,
               ROUND(MIN(tgpg), 4) AS min_tgpg, ROUND(MAX(tgpg), 4) AS max_tgpg,
               ROUND(MIN(otga), 4) AS min_otga, ROUND(MAX(otga), 4) AS max_otga,
               ROUND(MIN(otshga), 4) AS min_otshga, ROUND(MAX(otshga), 4) AS max_otshga
        FROM derived_team_stats GROUP BY season ORDER BY season
        """
    ):
        print(
            f"team {row['season']}: {row['rows']} row(s), {row['teams']} team(s), "
            f"null tgpg={row['null_tgpg']} otga={row['null_otga']} otshga={row['null_otshga']} "
            f"tgpg {row['min_tgpg']}..{row['max_tgpg']}  otga {row['min_otga']}..{row['max_otga']}  "
            f"otshga {row['min_otshga']}..{row['max_otshga']}"
        )

    # Lineup and daily-feed ingest counts, per season, so an empty table after
    # an upgrade is visible rather than discovered at publish time. player_lineups
    # is keyed by game_id alone, so the season comes from a join against the raw
    # rows rather than a column of its own.
    for row in conn.execute(
        "SELECT pg.season, COUNT(*) AS n FROM player_lineups pl"
        " JOIN (SELECT DISTINCT game_id, season FROM player_games) pg ON pg.game_id = pl.game_id"
        " GROUP BY pg.season ORDER BY pg.season"
    ):
        print(f"lineups {row['season']}: {row['n']} row(s)")

    for row in conn.execute(
        "SELECT season, COUNT(*) AS n, COUNT(DISTINCT game_date) AS dates"
        " FROM tims_daily GROUP BY season ORDER BY season"
    ):
        print(f"tims {row['season']}: {row['n']} player entries across {row['dates']} date(s)")

    for row in conn.execute(
        "SELECT season, COUNT(*) AS n, COUNT(DISTINCT game_date) AS dates"
        " FROM goalies_daily GROUP BY season ORDER BY season"
    ):
        print(f"goalies {row['season']}: {row['n']} team entries across {row['dates']} date(s)")

    for row in conn.execute(
        """
        SELECT season, COUNT(*) AS rows, COUNT(DISTINCT player_id) AS goalies,
               SUM(gaa IS NULL) AS null_gaa, SUM(save_pct IS NULL) AS null_sv,
               SUM(record IS NULL) AS null_rec
        FROM goalie_features GROUP BY season ORDER BY season
        """
    ):
        print(
            f"goalie_features {row['season']}: {row['rows']} appearance row(s), "
            f"{row['goalies']} goalie(s), "
            f"null gaa={row['null_gaa']} sv%={row['null_sv']} record={row['null_rec']}"
        )

    conn.close()


def _match_goalie(full_name, candidates):
    """Resolve a RotoWire full name to one box-score goalie appearance row.

    Box scores abbreviate to an initial ("J. Johansson"); RotoWire carries the
    full name ("Jonas Johansson"). Match on last name + first initial: exact
    and unambiguous across a whole season's data (2800/2800 on 2023-24), and
    safe to refuse on a miss - a wrong match would attach another goalie's
    season to this row, which is worse than a NULL stat column.

    ``candidates`` is a list of ``(player_id, box_name)``. Returns the
    ``player_id`` or None.
    """
    if not full_name:
        return None

    parts = full_name.split()
    if len(parts) < 2:
        return None
    want_last = parts[-1].lower()
    want_initial = parts[0][:1].lower()

    matched = [player_id for player_id, box_name in candidates if _box_name_matches(box_name, want_last, want_initial)]

    # Ambiguity (two same-surnamed goalies dressed) is as unresolvable as a
    # miss; refuse rather than pick one.
    return matched[0] if len(matched) == 1 else None


def _box_name_matches(box_name, want_last, want_initial):
    """'J. Johansson' against ('johansson', 'j'); handles 'U-P. Luukkonen'."""
    if not box_name or ". " not in box_name:
        return False
    initial, last = box_name.split(". ", 1)
    # A hyphenated first name ("U-P.") contributes multiple initials; the
    # first one is the conventional match key against RotoWire's first name.
    return last.lower() == want_last and initial.split("-")[0][:1].lower() == want_initial


def _archive_names(supabase, table, player_ids):
    """Full names from the archive being replaced, for one set of players.

    The archive stores full names ("Brent Burns") where box scores carry
    initials ("B. Burns"), so its spelling is what a drop-in replacement should
    ship. Chunked ``in_`` filters rather than one call: several hundred ids in
    a single filter make a URL long enough to trip proxy limits, and paging the
    whole table would read a hundred thousand rows for a few hundred names. A
    player the archive never saw simply has no entry, and publish falls back to
    the box-score spelling.
    """
    ids = sorted(player_ids)
    names = {}

    for start in range(0, len(ids), 100):
        page = supabase.table(table).select("player_id,name").in_("player_id", ids[start : start + 100]).execute()
        for record in page.data or []:
            if record.get("name"):
                names.setdefault(record["player_id"], record["name"])

    return names


def publish(season, db_path=DB_PATH):
    """Upsert derived features into Player-Snapshots-backtrack-{ENV}.

    Reads from the raw store rather than re-crawling: that is the point of the raw
    layer. Adding a feature column means re-running --derive and this, and nothing
    else.

    ``name`` prefers the archive's full spelling over the box score's initials -
    see ``_archive_names``.

    Only columns the table actually declares are sent. PostgREST rejects an entire
    batch on an unknown column, so a feature that exists locally but has no column
    in Supabase yet would fail the whole publish rather than just be skipped.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT d.player_id, d.game_date, d.gpg, d.five_gpg, d.ppg, d.hgpg, d.hppg, pg.goals,
               l.lineup_unit, l.lineup_position_group, l.pp_unit, l.lineup_status
        FROM player_games pg
        -- Goalies are excluded, matching reconstruct.py. They have no
        -- meaningful gpg and the live archive never held them (their numbers
        -- live in opp_goalie_* instead). Position comes from the box score's
        -- stat block, so this is the roster's own classification.
        JOIN derived_features d
          ON d.player_id = pg.player_id AND d.game_date = pg.game_date AND d.season = pg.season
        LEFT JOIN player_lineups l
          ON l.player_id = pg.player_id AND l.game_id = pg.game_id
        WHERE pg.season = ? AND pg.position <> 'G' AND d.season = ?
        """,
        (season, season),
    ).fetchall()

    if not rows:
        print(f"{season}: no derived features")
        conn.close()
        return 0

    # name and team_name are per-player and per-player-per-game respectively; the
    # raw rows are the only place either is stored. team_name needs the
    # abbreviation->place mapping, so it is resolved through team_map. The raw
    # name is the box score's abbreviated spelling; the archive's full names
    # override it at payload time, because the archive is the table being
    # replaced and its spelling is what consumers already display.
    raw_names = {
        r["player_id"]: r["name"]
        for r in conn.execute("SELECT DISTINCT player_id, name FROM player_games WHERE name IS NOT NULL")
    }
    teams = {
        (r["player_id"], r["game_date"]): r["team_abbrev"]
        for r in conn.execute("SELECT player_id, game_date, team_abbrev FROM player_games")
    }
    opponents = {
        (r["player_id"], r["game_date"]): r["opponent_abbrev"]
        for r in conn.execute("SELECT player_id, game_date, opponent_abbrev FROM player_games")
    }
    homes = {
        (r["player_id"], r["game_date"]): r["home"]
        for r in conn.execute("SELECT player_id, game_date, home FROM player_games")
    }

    env_name, supabase = config()
    table = f"Player-Snapshots-backtrack-{env_name}"
    full_names = _archive_names(supabase, f"Player-Snapshots-{env_name}", {r["player_id"] for r in rows})

    # Historic pick groups and projected starters, read back from the daily
    # feed tables that build/--feeds populated. Reading SQL rather than the
    # helper APIs keeps publish purely local: the JSON caches and the ingest
    # pass are the only places that ever touch the network.
    tims_by_date = {}
    for r in conn.execute("SELECT game_date, player_id, group_id FROM tims_daily WHERE season = ?", (season,)):
        tims_by_date.setdefault(r["game_date"], {})[r["player_id"]] = r["group_id"]
    mapped = sum(len(groups) for groups in tims_by_date.values())
    print(f"tims: {mapped} player entries across {len(tims_by_date)} game date(s)")

    # Opponent starting goalies, same live-schema denormalization below. The
    # stat columns are reconstructed from goalie_features rather than left
    # NULL: the window is strictly-before each date, the same as-of-morning
    # view a live club-stats scrape captured, so there is no season-end total
    # standing in for an October game.
    goalies_by_date = {}
    for r in conn.execute(
        "SELECT game_date, team_abbrev, goalie_name, status FROM goalies_daily WHERE season = ?", (season,)
    ):
        goalies_by_date.setdefault(r["game_date"], {})[r["team_abbrev"]] = {
            "name": r["goalie_name"],
            "status": r["status"],
        }
    goalie_teams = sum(len(g) for g in goalies_by_date.values())
    print(f"goalies: {goalie_teams} team entries across {len(goalies_by_date)} game date(s)")

    # RotoWire names the starter; the box score lists him under an initial, so
    # resolving him to a player_id needs the appearance rows for that date and
    # club. Only appearances (goals_against IS NOT NULL) are candidates - a
    # dressed backup has a box-score row but no stats to attach.
    goalie_roster = {}
    for r in conn.execute(
        "SELECT game_date, team_abbrev, player_id, name FROM player_games"
        " WHERE season = ? AND position = 'G' AND goals_against IS NOT NULL",
        (season,),
    ):
        goalie_roster.setdefault((r["game_date"], r["team_abbrev"]), []).append((r["player_id"], r["name"]))

    # As-of-date stats per appearance, keyed for lookup after the name match.
    goalie_stats = {}
    for r in conn.execute(
        "SELECT player_id, team_abbrev, game_date, gaa, save_pct, record,"
        " shutouts, games_played FROM goalie_features WHERE season = ?",
        (season,),
    ):
        goalie_stats[(r["player_id"], r["team_abbrev"], r["game_date"])] = r

    payload = []
    for r in rows:
        key = (r["player_id"], r["game_date"])
        # Opponent starter, denormalized per the live schema: status is
        # upper-cased and confirmed is a separate boolean, the same mapping
        # service.denormalize_players_for_db applies. A team with no listed
        # starter carries no entry, so the row gets UNKNOWN/False like live.
        goalie = goalies_by_date.get(r["game_date"], {}).get(opponents.get(key) or "")
        goalie_status = (goalie.get("status") or "").upper() if goalie else "UNKNOWN"
        # Stats follow the RotoWire-named goalie (the identity live joined on),
        # team-scoped like club-stats itself. A name miss leaves every stat
        # NULL while identity still ships, matching live's miss behaviour.
        opp_team = opponents.get(key) if goalie else None
        nhl_id = None
        gaa = save_pct = record = shutouts = games_played = None
        if goalie and opp_team:
            matched_id = _match_goalie(
                goalie.get("name"),
                goalie_roster.get((r["game_date"], opp_team), []),
            )
            if matched_id is not None:
                stats_row = goalie_stats.get((matched_id, opp_team, r["game_date"]))
                if stats_row is not None:
                    nhl_id = matched_id
                    gaa = stats_row["gaa"]
                    save_pct = stats_row["save_pct"]
                    record = stats_row["record"]
                    shutouts = stats_row["shutouts"]
                    games_played = stats_row["games_played"]
        payload.append(
            {
                "date": r["game_date"],
                "player_id": r["player_id"],
                "name": full_names.get(r["player_id"]) or raw_names.get(r["player_id"]) or f"player-{r['player_id']}",
                "team_name": to_place(teams.get(key)),
                "home": bool(homes.get(key)) if homes.get(key) is not None else None,
                "gpg": r["gpg"],
                "five_gpg": r["five_gpg"],
                # Single-season rate, an additional column (see
                # 20261009_add_ppg_player_snapshots_backtrack.sql). hgpg/hppg
                # below are the archive's own 3-year windows.
                "ppg": r["ppg"],
                # 3-year windows per get_hgpg/get_hppg(years=3) - compute_historic.
                # NULL on 20212022/20222023 rows until 20192020/20202021 exist in
                # the store; sending NULL is correct there (it is the value, and
                # the columns are already NULL in the table).
                "hgpg": r["hgpg"],
                "hppg": r["hppg"],
                # 0 for a skater the challenge never listed that day, matching
                # the archive's convention (test fixtures carry tims: 0).
                "tims": tims_by_date.get(r["game_date"], {}).get(r["player_id"], 0),
                # Training label from the game itself: did this skater score
                # in it. The box score's goals column is ground truth and
                # covers every published row; the challenge helper's own
                # graded flag agrees with it on every listed skater checked
                # (159/159 on 2023-10-14), so no second source is needed.
                "scored": 1 if r["goals"] else 0,
                # ACTUAL deployment from shift charts - see lineup_reconstruct.
                "lineup_unit": r["lineup_unit"],
                "lineup_position_group": r["lineup_position_group"],
                "pp_unit": r["pp_unit"],
                "lineup_status": r["lineup_status"],
                "opp_goalie_name": goalie.get("name") if goalie else None,
                "opp_goalie_team": opp_team,
                "opp_goalie_status": goalie_status,
                "opp_goalie_confirmed": bool(goalie) and (goalie.get("status") or "").lower() == "confirmed",
                # Reconstructed as-of-date stats from goalie_features; NULL on
                # a club debut or an unmatched name, never a fabricated 0.
                "opp_goalie_nhl_id": nhl_id,
                "opp_goalie_gaa": gaa,
                "opp_goalie_save_pct": save_pct,
                "opp_goalie_record": record,
                "opp_goalie_shutouts": shutouts,
                "opp_goalie_games_played": games_played,
            }
        )

    written = 0
    batch_size = 500

    for start in range(0, len(payload), batch_size):
        batch = payload[start : start + batch_size]
        supabase.table(table).upsert(batch, on_conflict="date,player_id", returning="minimal").execute()
        written += len(batch)

    conn.close()
    print(f"published {written} row(s) to {table}")

    return written


def publish_team(season, db_path=DB_PATH):
    """Upsert derived_team_stats into Team-Stats-backtrack-{ENV}.

    Separate from publish() because the grain differs: one row per team-game here,
    one row per player-game there. Writing them through the same path would put team
    attributes on player rows and reintroduce the ~21x duplication the table split
    exists to avoid.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT season, team_abbrev, game_date, game_id, tgpg, otga, otshga FROM derived_team_stats WHERE season = ?",
        (season,),
    ).fetchall()

    if not rows:
        print(f"{season}: no derived team stats")
        conn.close()
        return 0

    env_name, supabase = config()
    table = f"Team-Stats-backtrack-{env_name}"

    payload = [
        {
            "season": r["season"],
            "team_abbrev": r["team_abbrev"],
            # Denormalised so a reader does not need team_map.py to get a place name.
            "team_name": to_place(r["team_abbrev"]),
            "date": r["game_date"],
            "game_id": r["game_id"],
            "tgpg": r["tgpg"],
            "otga": r["otga"],
            "otshga": r["otshga"],
        }
        for r in rows
    ]

    written = 0
    batch_size = 500

    for start in range(0, len(payload), batch_size):
        batch = payload[start : start + batch_size]
        supabase.table(table).upsert(batch, on_conflict="season,team_abbrev,game_id", returning="minimal").execute()
        written += len(batch)

    conn.close()
    print(f"published {written} team row(s) to {table}")

    return written


def main():  # noqa: PLR0911 - one return per CLI mode; a dispatch table would obscure the flow
    parser = argparse.ArgumentParser(description="Local SQLite raw store for NHL per-game data.")
    parser.add_argument("--build", metavar="SEASON", help="Load raw rows for one season from the cache.")
    parser.add_argument(
        "--gamelogs",
        metavar="SEASON",
        help="Re-merge only the game-log stat families (incl. goalie saves) for one season.",
    )
    parser.add_argument("--derive", metavar="SEASON", help="Recompute derived features for one season.")
    parser.add_argument(
        "--lineups", metavar="SEASON", help="Recompute player_lineups from the shift-chart cache for one season."
    )
    parser.add_argument(
        "--feeds", metavar="SEASON", help="Re-ingest tims_daily and goalies_daily from the JSON caches."
    )
    parser.add_argument("--publish", metavar="SEASON", help="Upsert player features into Supabase.")
    parser.add_argument("--publish-team", metavar="SEASON", help="Upsert team stats into Supabase.")
    parser.add_argument("--stats", action="store_true", help="Summarise what is stored.")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    if args.stats:
        stats(Path(args.db))
        return 0

    if args.build:
        conn = connect(Path(args.db))
        build(args.build, db_path=Path(args.db))
        compute_derived(conn, args.build)
        # Team stats too: --build without them leaves derived_team_stats empty
        # for the season while derived_features is full, and a publish after a
        # build would silently ship no team rows.
        compute_derived_team(conn, args.build)
        conn.close()
        print(f"derived features computed for {args.build}")
        return 0

    if args.gamelogs:
        conn = connect(Path(args.db))
        merge_gamelogs(conn, args.gamelogs)
        conn.close()
        return 0

    if args.derive:
        conn = connect(Path(args.db))
        compute_derived(conn, args.derive)
        compute_derived_team(conn, args.derive)
        conn.close()
        print(f"derived features recomputed for {args.derive}")
        return 0

    if args.lineups:
        conn = connect(Path(args.db))
        written = compute_lineups(conn, args.lineups)
        conn.close()
        print(f"lineups -> {written} row(s) reconstructed for {args.lineups}")
        return 0

    if args.feeds:
        conn = connect(Path(args.db))
        written = ingest_challenge_feeds(conn, args.feeds)
        conn.close()
        print(f"feeds -> {written} row(s) ingested for {args.feeds}")
        return 0

    if args.publish:
        publish(args.publish, db_path=Path(args.db))
        return 0

    if args.publish_team:
        publish_team(args.publish_team, db_path=Path(args.db))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
