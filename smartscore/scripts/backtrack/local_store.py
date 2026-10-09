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
===============  =========================================  ====================

Neither is a superset, so both are read and merged into one wide row keyed on
``(player_id, game_id)``. Which endpoint a column came from is recorded in the
``boxscore_fields`` / ``gamelog_fields`` columns, because "this is NULL" and "this
endpoint never carries this field" are different facts and worth telling apart.

Usage::

    uv run python smartscore/scripts/backtrack/local_store.py --build 20232024
    uv run python smartscore/scripts/backtrack/local_store.py --derive 20232024
    uv run python smartscore/scripts/backtrack/local_store.py --publish 20232024
    uv run python smartscore/scripts/backtrack/local_store.py --publish-team 20232024
    uv run python smartscore/scripts/backtrack/local_store.py --stats
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from nhl_client import fetch_boxscore, fetch_game_log, season_games  # noqa: E402
from reconstruct import config  # noqa: E402 - lazy Supabase client, only needed by --publish
from team_map import to_place  # noqa: E402

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
    "shorthandedPoints,otGoals,gameWinningGoals"
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
    ot_goals             INTEGER,
    game_winning_goals   INTEGER,

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
-- otshga - the opponent's shorthanded goals AGAINST per game. Not reconstructable
--   from player_games: the merge stores each player's shorthanded goals but not
--   which of them were scored against the opponent's power play, which is what
--   this number means. Needs api.nhle.com/stats/rest/en/team/penaltykilltime.
--   Left NULL, documented rather than approximated.
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
CREATE TABLE IF NOT EXISTS derived_features (
    season    TEXT NOT NULL,
    player_id INTEGER NOT NULL,
    game_date TEXT NOT NULL,
    gpg       REAL,
    five_gpg  REAL,
    PRIMARY KEY (season, player_id, game_date)
);
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
        ("player_games", "team_goals_for", "INTEGER"),
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
        "boxscore_fields": None,
        "gamelog_fields": GAMELOG_FIELDS,
    }


def _upsert(conn, rows):
    """Merge rows into player_games, filling gaps rather than replacing.

    NOT ``INSERT OR REPLACE``. The two endpoints carry complementary fields - the
    box score has position/sweater_number, the game log has shorthanded and OT
    goals - so replacing on the second pass throws away the first pass's columns.
    That is not hypothetical: with REPLACE, position and sweater_number survived
    only for goalies (whose game logs are empty and so never got overwritten),
    leaving 95% of rows missing them.

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


def build(season, db_path=DB_PATH, delay_seconds=0.0):
    """Populate raw rows for one season from the cached payloads.

    Reads the cache rather than the network, so a rebuild after a schema change is
    instant. Assumes the season has already been crawled; run reconstruct.py first
    if the cache is cold.
    """
    conn = connect(db_path)
    games = season_games(season, delay_seconds=delay_seconds)
    game_season = {g["id"]: season for g in games}
    written = 0

    for game in games:
        records = fetch_boxscore(game["id"], cache_dir=CACHE_DIR)

        rows = []
        for record in records:
            row = _merge_boxscore(record)
            row["season"] = game_season.get(game["id"], season)
            rows.append(row)

        written += _upsert(conn, rows)

    print(f"box scores -> {written} row(s) for {season}")

    # Second pass: the game log supplies the four stat families the box score omits.
    # Progress is logged every 100 players - this loop is ~1,000 commits, so a
    # silent run is indistinguishable from a hung one, which is exactly the
    # visibility gap that made an earlier build impossible to monitor.
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

        log_written += _upsert(conn, rows)

        if index % 100 == 0:
            print(f"  game logs: {index}/{len(player_ids)} players, {log_written} row(s) merged", flush=True)

    print(f"game logs -> {log_written} row(s) merged for {season}")

    conn.close()
    return written, log_written


def compute_derived(conn, season):
    """Fill derived_features.gpg for one season.

    Written as a window function over the raw rows so the arithmetic is visible
    and reviewable: goals and games accumulated STRICTLY BEFORE the row's own game
    (RANGE ... PRECEDING with 1 PRECEDING), then divided. A row therefore reads as
    "entering that game", which is what a pre-game pick needs and what the archive
    stores.

    A player's first game has no games before it, and 0/0 normalises to 0 rather
    than NULL - the archive stores 0 on pre-debut rows (verified against a player
    who scored in his debut, whose stored gpg there still reads 0), so 0 keeps
    this table a drop-in replacement. NULL would only ever appear for rows written
    by an older build; it means "recompute with --derive".
    """
    conn.execute("DELETE FROM derived_features WHERE season = ?", (season,))

    conn.execute(
        """
        INSERT INTO derived_features (season, player_id, game_date, gpg, five_gpg)
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


def compute_derived_team(conn, season):
    """Fill derived_team_stats for one season: tgpg and otga per team-game.

    Built in one pass because both rates fall out of a single per-(team, game)
    collapse of player_games, once each row knows what its opponent scored:

        NSH  3   against TBL 5
        TBL  5   against NSH 3

    tgpg - own goals for, over this team's games played before this one.
    otga - the OPPONENT's goals against, over the opponent's games before this
           one. Goals scored against an opponent equal that opponent's goals
           against, so summing the opponent's own goals_for in those shared games
           gives the number directly - no separate concessions tally needed.

    Both use the strictly-before frame so a row for game N carries the rate
    entering it, matching gpg. A team's first game of the season is NULL, not 0:
    a zero would claim a team averages no goals per game, which the archive
    does emit (Winnipeg, Anaheim, Dallas and Carolina all show tgpg = 0 at the
    start of the current season).
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
               MAX(team_goals_for) AS goals_for
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
        # Silently dropping these would make otga wrong for the affected games
        # rather than absent, so it is worth refusing.
        raise ValueError(f"{orphans} team-game row(s) have no opponent; otga would be wrong")

    # Step 2: cumulative per team. gf/gp drive tgpg; ga (goals conceded, i.e. the
    # sum of what opponents scored) drives the team's OWN goals-against rate. Both
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
            SUM(o.goals_for)   OVER w AS ga
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

    # Step 3: otga is the OPPONENT's goals against per game, so it is read off the
    # other team's row for the same game - not off this team's own ga. Those are
    # different quantities: a team's ga is what IT conceded, while otga is what
    # the club it is facing concedes, i.e. this team's scoring rate against that
    # specific opponent.
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
            NULL
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
               SUM(CASE WHEN position IS NOT NULL THEN 1 ELSE 0 END) AS from_boxscore,
               SUM(CASE WHEN shorthanded_goals IS NOT NULL THEN 1 ELSE 0 END) AS from_gamelog
        FROM player_games
        GROUP BY season
        ORDER BY season
        """
    ):
        print(
            f"{row['season']}: {row['rows']} rows, {row['players']} players, {row['games']} games "
            f"({row['first_date']} .. {row['last_date']}) "
            f"boxscore-backed={row['from_boxscore']} gamelog-backed={row['from_gamelog']}"
        )

    for row in conn.execute("SELECT season, COUNT(*) AS n FROM derived_features GROUP BY season ORDER BY season"):
        print(f"derived {row['season']}: {row['n']} feature row(s)")

    for row in conn.execute(
        """
        SELECT season, COUNT(*) AS rows, COUNT(DISTINCT team_abbrev) AS teams,
               SUM(tgpg IS NULL) AS null_tgpg, SUM(otga IS NULL) AS null_otga,
               ROUND(MIN(tgpg), 4) AS min_tgpg, ROUND(MAX(tgpg), 4) AS max_tgpg,
               ROUND(MIN(otga), 4) AS min_otga, ROUND(MAX(otga), 4) AS max_otga
        FROM derived_team_stats GROUP BY season ORDER BY season
        """
    ):
        print(
            f"team {row['season']}: {row['rows']} row(s), {row['teams']} team(s), "
            f"null tgpg={row['null_tgpg']} otga={row['null_otga']} "
            f"tgpg {row['min_tgpg']}..{row['max_tgpg']}  otga {row['min_otga']}..{row['max_otga']}"
        )

    conn.close()


def publish(season, db_path=DB_PATH):
    """Upsert derived features into Player-Snapshots-backtrack-{ENV}.

    Reads from the raw store rather than re-crawling: that is the point of the raw
    layer. Adding a feature column means re-running --derive and this, and nothing
    else.

    Only columns the table actually declares are sent. PostgREST rejects an entire
    batch on an unknown column, so a feature that exists locally but has no column
    in Supabase yet would fail the whole publish rather than just be skipped.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT d.player_id, d.game_date, d.gpg, d.five_gpg
        FROM derived_features d
        JOIN (
            -- Goalies are excluded, matching reconstruct.py. They have no
            -- meaningful gpg and the live archive never held them (their numbers
            -- live in opp_goalie_* instead). Position comes from the box score's
            -- stat block, so this is the roster's own classification.
            SELECT DISTINCT player_id
            FROM player_games
            WHERE season = ? AND position <> 'G'
        ) skaters ON skaters.player_id = d.player_id
        WHERE d.season = ?
        """,
        (season, season),
    ).fetchall()

    if not rows:
        print(f"{season}: no derived features")
        conn.close()
        return 0

    # name and team_name are per-player and per-player-per-game respectively; the
    # raw rows are the only place either is stored. team_name needs the
    # abbreviation->place mapping, so it is resolved through team_map.
    names = {
        r["player_id"]: r["name"]
        for r in conn.execute("SELECT DISTINCT player_id, name FROM player_games WHERE name IS NOT NULL")
    }
    teams = {
        (r["player_id"], r["game_date"]): r["team_abbrev"]
        for r in conn.execute("SELECT player_id, game_date, team_abbrev FROM player_games")
    }
    homes = {
        (r["player_id"], r["game_date"]): r["home"]
        for r in conn.execute("SELECT player_id, game_date, home FROM player_games")
    }

    env_name, supabase = config()
    table = f"Player-Snapshots-backtrack-{env_name}"

    payload = []
    for r in rows:
        key = (r["player_id"], r["game_date"])
        payload.append(
            {
                "date": r["game_date"],
                "player_id": r["player_id"],
                "name": names.get(r["player_id"]) or f"player-{r['player_id']}",
                "team_name": to_place(teams.get(key)),
                "home": bool(homes.get(key)) if homes.get(key) is not None else None,
                "gpg": r["gpg"],
                "five_gpg": r["five_gpg"],
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


def main():
    parser = argparse.ArgumentParser(description="Local SQLite raw store for NHL per-game data.")
    parser.add_argument("--build", metavar="SEASON", help="Load raw rows for one season from the cache.")
    parser.add_argument("--derive", metavar="SEASON", help="Recompute derived features for one season.")
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

    if args.derive:
        conn = connect(Path(args.db))
        compute_derived(conn, args.derive)
        compute_derived_team(conn, args.derive)
        conn.close()
        print(f"derived features recomputed for {args.derive}")
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
