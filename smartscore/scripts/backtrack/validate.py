#!/usr/bin/env python3
"""Compare reconstructed features against Player-Snapshots-{ENV}.

WHY THIS EXISTS RATHER THAN A SQL JOIN
---------------------------------------
Three separate validation attempts failed for the same underlying reason: the
archive's ``team_name`` is a PLACE name, and NYI ("New York") and NYR
("New York") share it. Any join through that column silently matches whichever
Islanders/Rangers row the planner picks first, which showed up as ~560 "wrong"
team-stat rows that were really NYI/NYR values swapped between the two clubs.

So the join here is done in Python and keyed on ``team_abbrev``, which is unique
per club. ``team_name`` is never used as a key - it is only ever read for display.

Two shapes are compared:

* per-player features - gpg, five_gpg - joined on (date, player_id), both keys
  natural and unique.
* team features - tgpg, otga - joined on (date, player_id) too, then resolved to
  a team through our own player_games: the archive denormalises each team's
  values onto every one of its player rows, and a player plays for exactly one
  club on a given date. That gives a real team key without ever reading the
  archive's team_name, which is a PLACE name and collides for NYI/NYR ("New
  York"). otga needs no opponent lookup either - the archive stores it on the
  same player's row already resolved to his team's opponent.

The archive only populates tgpg/otga on a window of dates (62 calendar days,
2024-02-16..2024-04-18 in 2023-24); outside it those columns are null and the
row still compares on gpg. Archive rows whose date/player pair has no game in
our store (captures on non-game days, or a player who did not play that night)
cannot resolve a team and are counted separately rather than guessed at.

Rounding is compared at 2 decimal places, because the archive stores most team
values at 1-5dp and comparing 2dp-stored values against our full precision at
higher tolerance reports rounding as disagreement (measured: match rates get
*worse* as tolerance tightens past the archive's own precision).

Usage::

    uv run python smartscore/scripts/backtrack/validate.py
    uv run python smartscore/scripts/backtrack/validate.py --env prod
    uv run python smartscore/scripts/backtrack/validate.py --season 20232024 --tol 3
"""

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

# config.py lives in smartscore/, two levels up from this script. It is imported
# at module scope (not lazily) because the archive read happens throughout; the
# ENV override below rebinds this module's ENV after import, since the table name
# is built from it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import ENV, SUPABASE_ADMIN_CLIENT  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "raw_nhl.sqlite"
PAGE_SIZE = 1000


def archive_table():
    return f"Player-Snapshots-{ENV}"


def fetch_archive(start_date, end_date):
    """All archive rows in a window, paged, keyed ``(date, player_id) -> row``.

    The rows carry gpg, five_gpg and - on a subset of dates - the denormalised
    team columns tgpg/otga. team_name is fetched for display only and is never
    used as a join key.
    """
    client = SUPABASE_ADMIN_CLIENT.table(archive_table())

    by_player = {}
    offset = 0

    while True:
        response = (
            client.select("date,player_id,gpg,five_gpg,tgpg,otga,team_name")
            .gte("date", start_date)
            .lte("date", end_date)
            .order("date")
            .order("player_id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = response.data or []

        if not batch:
            break

        for row in batch:
            by_player[(row["date"], row["player_id"])] = row

        if len(batch) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    return by_player


def load_derived(db_path=DB_PATH, season=None):
    """Reconstructed features from the local store, keyed for lookup.

    Returns ``(players, teams, player_team)``:

    * ``players`` - ``(game_date, player_id) -> derived_features row``.
    * ``teams`` - ``(game_date, team_abbrev) -> derived_team_stats row``.
    * ``player_team`` - ``(game_date, player_id) -> team_abbrev``, from the raw
      rows. This is the bridge from an archive player row to a team, and it is
      why the team comparison never needs the archive's team_name.
    """
    import sqlite3  # noqa: PLC0415

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    where = "WHERE season = ?" if season else ""
    params = (season,) if season else ()

    # S608: `where` is a fixed literal above; the value it binds is `season`.
    players = {
        (r["game_date"], r["player_id"]): r
        for r in conn.execute(f"SELECT game_date, player_id, gpg, five_gpg FROM derived_features {where}", params)  # noqa: S608
    }

    teams = {
        (r["game_date"], r["team_abbrev"]): r
        for r in conn.execute(f"SELECT game_date, team_abbrev, tgpg, otga FROM derived_team_stats {where}", params)  # noqa: S608
    }

    player_team = {
        (r["game_date"], r["player_id"]): r["team_abbrev"]
        for r in conn.execute(f"SELECT game_date, player_id, team_abbrev FROM player_games {where}", params)  # noqa: S608
        if r["team_abbrev"] is not None
    }
    conn.close()

    return players, teams, player_team


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env", default=None, help="Override ENV.")
    parser.add_argument("--season", default=None, help="Limit the local side to one season.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--tol", type=int, default=2, help="Decimal places to compare at.")
    args = parser.parse_args()

    if args.env:
        # config already ran with the ambient ENV; rebind this module's copy so
        # archive_table() and the banner below both follow the flag. The clients
        # themselves are ENV-independent (same URL and keys either way).
        global ENV  # noqa: PLW0603
        os.environ["ENV"] = args.env
        ENV = args.env

    import local_store  # noqa: PLC0415

    conn = local_store.connect(Path(args.db))

    dates = [r[0] for r in conn.execute("SELECT DISTINCT game_date FROM derived_features ORDER BY game_date")]
    start = args.start or dates[0]
    end = args.end or dates[-1]
    conn.close()

    print(f"comparing {start} .. {end} against Player-Snapshots-{ENV}\n")

    players, teams, player_team = load_derived(Path(args.db), args.season)
    by_player = fetch_archive(start, end)

    print(f"archive player rows : {len(by_player)}")
    print(f"local feature rows  : {len(players)}")
    print(f"local team rows     : {len(teams)}")

    # --- per-player features -------------------------------------------------
    stats = defaultdict(lambda: {"n": 0, "match": 0, "ours_null": 0, "missing": 0})
    examples = defaultdict(list)

    for (date, player_id), arch in by_player.items():
        mine = players.get((date, player_id))

        if mine is None:
            stats["_row"]["missing"] += 1
            continue

        stats["_row"]["n"] += 1

        for field in ("gpg", "five_gpg"):
            stored = arch.get(field)
            ours = mine[field]

            if stored is None:
                continue

            b = stats[field]
            b["n"] += 1

            if ours is None:
                b["ours_null"] += 1
                continue

            if abs(round(ours, args.tol) - round(stored, args.tol)) < 10 ** (-args.tol):
                b["match"] += 1
            elif len(examples[field]) < 8:
                examples[field].append((date, player_id, stored, round(ours, 6)))

    print()
    for field in ("gpg", "five_gpg"):
        b = stats[field]
        if b["n"]:
            pct = 100 * b["match"] / b["n"]
            print(f"{field:<9} {b['match']:>7}/{b['n']:<7} {pct:6.2f}%   (ours null on {b['ours_null']})")
            for ex in examples[field]:
                print(f"          {ex[0]} {ex[1]} archive={ex[2]} ours={ex[3]}")

    print(f"\nrows compared: {stats['_row']['n']}, archive rows with no local match: {stats['_row']['missing']}")

    # --- team features -------------------------------------------------------
    # tgpg and otga are team attributes denormalised onto the archive's player
    # rows. The join goes archive row -> our player_games row for the same
    # (date, player) -> that row's team_abbrev -> derived_team_stats. A player
    # plays for exactly one club on a date, so this is a real team key and the
    # NYI/NYR "New York" collision in team_name never enters the picture. otga
    # is stored on the player's row already resolved to his team's opponent, so
    # it compares directly too - no opponent lookup needed.
    t_stats = {f: {"n": 0, "match": 0, "no_team": 0, "no_ours": 0, "ours_null": 0} for f in ("tgpg", "otga")}
    t_examples = {"tgpg": [], "otga": []}

    for (date, player_id), arch in by_player.items():
        if arch.get("tgpg") is None and arch.get("otga") is None:
            continue

        team = player_team.get((date, player_id))

        if team is None:
            # No game for this (date, player) in the raw store: a capture on a
            # non-game day, or the player did not play that night. No team to
            # resolve, so skip rather than guess.
            t_stats["tgpg"]["no_team"] += 1
            t_stats["otga"]["no_team"] += 1
            continue

        ours = teams.get((date, team))

        if ours is None:
            # The player played but our team stats have no row for that
            # team-date (team_abbrev missing on the raw row, for instance).
            t_stats["tgpg"]["no_ours"] += 1
            t_stats["otga"]["no_ours"] += 1
            continue

        for field in ("tgpg", "otga"):
            stored = arch.get(field)

            if stored is None:
                continue

            b = t_stats[field]
            b["n"] += 1

            value = ours[field]

            if value is None:
                # Season-debut games hold NULL for us by design (a team entering
                # its first game has no rate), while the archive emits 0.
                b["ours_null"] += 1
                continue

            if abs(round(value, args.tol) - round(stored, args.tol)) < 10 ** (-args.tol):
                b["match"] += 1
            elif len(t_examples[field]) < 8:
                t_examples[field].append((date, team, stored, round(value, 6)))

    print()
    for field in ("tgpg", "otga"):
        b = t_stats[field]

        if b["n"]:
            pct = 100 * b["match"] / b["n"]
            print(
                f"{field:<9} {b['match']:>7}/{b['n']:<7} {pct:6.2f}%   "
                f"(unresolvable {b['no_team']}, no local team row {b['no_ours']}, ours null {b['ours_null']})"
            )

            for ex in t_examples[field]:
                print(f"          {ex[0]} {ex[1]} archive={ex[2]} ours={ex[3]}")


if __name__ == "__main__":
    main()
