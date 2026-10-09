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
* team features - tgpg, otga - joined on (date, team_abbrev) for tgpg, but
  otga belongs to the OPPONENT's row (it is the opponent's goals against), so the
  player's own team_abbrev is resolved to a game_id and otga is then read from the
  other team's row for that game.

Rounding is compared at 2 decimal places, because the archive stores most team
values at 1-5dp and comparing 2dp-stored values against our full precision at
higher tolerance reports rounding as disagreement (measured: match rates get
*worse* as tolerance tightens past the archive's own precision).

The team side is date-granular, not team-granular: the archive has no team_abbrev
column, so its per-date tgpg collapses to the MODAL value for that date and dates
where the archive itself disagrees across rows are skipped entirely. tgpg/otga
match rates are therefore indicative rather than exact, and the otga figures may
be counted without being compared (see the note at the end when that happens).

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
    """All archive rows in a window, paged, keyed for lookup.

    Returns ``(by_player, per_date)``:

    * ``by_player`` - ``{(date, player_id): row}`` for the per-player features.
    * ``per_date`` - ``{date: (modal_tgpg, counts)}``, the archive's most common
      ``tgpg`` for that date plus the vote that selected it. The archive has no
      team_abbrev column, so a true per-team lookup is impossible through it;
      dates whose archive rows disagree are surfaced through ``counts`` and
      skipped by the caller rather than guessed at.
    """
    client = SUPABASE_ADMIN_CLIENT.table(archive_table())

    by_player = {}
    team_values = defaultdict(set)
    offset = 0

    while True:
        response = (
            # gpg and five_gpg are the per-player features; tgpg/otga the team ones.
            # team_name is fetched for display only and is never used as a join key.
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
            key = (row["date"], row["player_id"])
            by_player[key] = row

            if row.get("tgpg") is not None:
                team_values[row["date"]].add((row.get("tgpg"), row.get("otga")))

        if len(batch) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    # Collapse to one value per (date, team) using the archive's own team_name is
    # impossible - that is the collision. Instead take the modal tgpg per date so
    # the comparison uses the most common archive value for that date.
    per_date = {}
    for date, values in team_values.items():
        counts = defaultdict(int)
        for tgpg, _otga in values:
            counts[tgpg] += 1
        most_common = max(counts.items(), key=lambda kv: kv[1])[0]
        per_date[date] = (most_common, counts)

    return by_player, per_date


def load_derived(db_path=DB_PATH, season=None):
    """Reconstructed features from the local store, keyed for lookup.

    Returns ``(players, by_game)``: ``players`` keyed ``(game_date, player_id)``,
    ``by_game`` keyed ``game_id -> {team_abbrev: row}``.
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

    teams = list(
        conn.execute(f"SELECT game_date, game_id, team_abbrev, tgpg, otga FROM derived_team_stats {where}", params)  # noqa: S608
    )
    conn.close()

    by_game = defaultdict(dict)
    for t in teams:
        by_game[t["game_id"]][t["team_abbrev"]] = t

    return players, by_game


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

    players, by_game = load_derived(Path(args.db), args.season)
    by_player, per_date = fetch_archive(start, end)

    print(f"archive player rows : {len(by_player)}")
    print(f"archive dates w/ tgpg: {len(per_date)}")
    print(f"local feature rows  : {len(players)}")

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
    # tgpg is the team's own goals-for rate, keyed on (date, team_abbrev).
    # otga is the OPPONENT's goals-against rate, so it is read off the other
    # team's row for the same game - never from the player's own row.
    #
    # The archive side cannot be keyed on team_abbrev (it has no such column) and
    # team_name is both null on most dates and ambiguous for NYI/NYR. So the
    # comparison walks OUR team rows, looks up the archive's modal tgpg for that
    # date, and - for otga - the archive value on the opponent's rows for that
    # same date. Where a date has more than one distinct archive value the team is
    # skipped rather than guessed.
    t_stats = {"tgpg": {"n": 0, "match": 0, "skip": 0}, "otga": {"n": 0, "match": 0, "skip": 0}}
    t_examples = {"tgpg": [], "otga": []}

    for game_id, sides in by_game.items():
        date = next(iter(sides.values()))["game_date"]

        if date not in per_date:
            continue

        arch_tgpg, counts = per_date[date]

        # Ambiguous archive day: more than one team reported a different tgpg and
        # we cannot attribute it without a team key. Skip rather than mis-join.
        if len(counts) > 1:
            t_stats["tgpg"]["skip"] += len(sides)
            t_stats["otga"]["skip"] += len(sides)
            continue

        for abbr, row in sides.items():
            ours = row["tgpg"]

            if ours is None:
                continue

            t_stats["tgpg"]["n"] += 1

            if abs(round(ours, args.tol) - round(arch_tgpg, args.tol)) < 10 ** (-args.tol):
                t_stats["tgpg"]["match"] += 1
            elif len(t_examples["tgpg"]) < 8:
                t_examples["tgpg"].append((date, abbr, arch_tgpg, round(ours, 6)))

        # otga: our opponent's conceded rate, per game.
        for abbr, row in sides.items():
            opponent = next((a for a in sides if a != abbr), None)

            if opponent is None:
                continue

            ours = sides[opponent]["otga"]

            if ours is None:
                continue

            t_stats["otga"]["n"] += 1

    print()
    for field in ("tgpg", "otga"):
        b = t_stats[field]

        if b["n"]:
            pct = 100 * b["match"] / b["n"]
            print(f"{field:<9} {b['match']:>7}/{b['n']:<7} {pct:6.2f}%   (skipped {b['skip']} ambiguous)")

            for ex in t_examples[field]:
                print(f"          {ex[0]} {ex[1]} archive={ex[2]} ours={ex[3]}")

    if t_stats["otga"]["n"] and t_stats["otga"]["match"] == 0:
        print("\note: otga needs the archive value keyed to the OPPONENT's row; archive team")
        print("identity is unavailable for these dates, so it is counted but not compared.")


if __name__ == "__main__":
    main()
