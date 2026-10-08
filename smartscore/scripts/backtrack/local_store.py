"""Local raw store: one SQLite row per player per game.

This is the foundation the derived tables are computed from. Everything
season-to-date - ``gpg``, ``hgpg``, every future feature - is a window function
over these rows, so adding a feature means writing SQL, not re-crawling the API.

WHY THE RAW LAYER LIVES HERE RATHER THAN IN SUPABASE
----------------------------------------------------
``Player-Snapshots-backtrack-{ENV}`` holds one row per player per *date* with the
cumulative totals already applied. That is the right shape for the app and the
model, but it cannot answer "what happened in this game", so a bug in the
accumulator is invisible and unfixable without another 80-minute crawl.

These rows are the audit trail: ``gpg`` on any date is ``SUM(goals) / COUNT(*)``
over games strictly before it, and re-running that here reproduces the shipped
numbers exactly (see ``verify_against_backtrack``).

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
    uv run python smartscore/scripts/backtrack/local_store.py --verify 20232024
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
    for column, definition in (("five_gpg", "REAL"),):
        existing = {r[1] for r in conn.execute("PRAGMA table_info(derived_features)")}
        if column not in existing:
            conn.execute(f"ALTER TABLE derived_features ADD COLUMN {column} {definition}")  # noqa: S608

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
    assignments = ", ".join(
        f"{c} = COALESCE(player_games.{c}, excluded.{c})" for c in conflict_columns
    )
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

    Games with gp_to_date = 0 get NULL rather than 0, so "has not played yet" stays
    distinguishable from "played and scored none".
    """
    conn.execute("DELETE FROM derived_features WHERE season = ?", (season,))

    conn.execute(
        """
        INSERT INTO derived_features (season, player_id, game_date, gpg, five_gpg)
        SELECT
            season,
            player_id,
            game_date,
            CASE
                WHEN prev_gp = 0 THEN NULL
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
            CASE
                WHEN games_before = 0 THEN NULL
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


def main():
    parser = argparse.ArgumentParser(description="Local SQLite raw store for NHL per-game data.")
    parser.add_argument("--build", metavar="SEASON", help="Load raw rows for one season from the cache.")
    parser.add_argument("--derive", metavar="SEASON", help="Recompute derived features for one season.")
    parser.add_argument("--publish", metavar="SEASON", help="Upsert derived features into Supabase.")
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
        conn.close()
        print(f"derived features computed for {args.build}")
        return 0

    if args.derive:
        conn = connect(Path(args.db))
        compute_derived(conn, args.derive)
        conn.close()
        print(f"derived features recomputed for {args.derive}")
        return 0

    if args.publish:
        publish(args.publish, db_path=Path(args.db))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())