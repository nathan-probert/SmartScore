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

* per-player features - gpg, five_gpg, hgpg, hppg - joined on (date, player_id),
  both keys natural and unique. hgpg/hppg compare only where the local window
  exists (both prior seasons crawled), so 2021-22/2022-23 rows count as "ours
  null" there rather than as disagreement.
* team features - tgpg, otga - joined on (date, player_id) too, then resolved to
  a team through our own player_games: the archive denormalises each team's
  values onto every one of its player rows, and a player plays for exactly one
  club on a given date. That gives a real team key without ever reading the
  archive's team_name, which is a PLACE name and collides for NYI/NYR ("New
  York"). otga needs no opponent lookup either - the archive stores it on the
  same player's row already resolved to his team's opponent.

A third check (``--goalies``) needs no archive at all: it compares full-season
goalie totals against the NHL's own historical club-stats
(``/v1/club-stats/{team}/{season}/2``), which is season-end ground truth for
the game-log accumulation that goalie_features reconstructs. It is separate
because it hits the network rather than Supabase, and because the archive's
own goalie stat columns only exist from 2026-27 on.

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
            client.select("date,player_id,gpg,five_gpg,hgpg,hppg,tgpg,otga,team_name")
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
        for r in conn.execute(
            f"SELECT game_date, player_id, gpg, five_gpg, hgpg, hppg FROM derived_features {where}",  # noqa: S608
            params,
        )
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


def fetch_club_stats(team, season, game_type=2):
    """One team's season-end club-stats block: ``{"goalies": [...]}``.

    ``GET /v1/club-stats/{team}/{season}/{game_type}`` is the historical form
    of the live pipeline's ``/now`` call - same fields, but scoped to a closed
    season, so it is season-end ground truth for validating the game-log
    accumulation. Goalie entries carry playerId, gamesPlayed, wins, losses,
    overtimeLosses, goalsAgainstAverage (6dp), savePercentage (6dp), shotsAgainst,
    saves, goalsAgainst, shutouts, timeOnIce (seconds).
    """
    import requests  # noqa: PLC0415

    url = f"https://api-web.nhle.com/v1/club-stats/{team}/{season}/{game_type}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def validate_goalies(db_path=DB_PATH, season=None):
    """Compare full-season goalie totals against club-stats season-end values.

    The raw player_games rows and the goalie_features window share one source
    and one filter (``position = 'G' AND goals_against IS NOT NULL``), so
    matching club-stats here validates the accumulation arithmetic end to end:
    the per-60 GAA formula, the saves/shots ratio, the decision counts, the
    team-scoped partition, and the toi parse. As-of-date mid-season values are
    the same sums over a shorter frame - the strictly-before window - so a
    correct season end implies a correct window.

    Team-scoped on both sides, matching club-stats' own team URL: a traded
    goalie appears once per club with that club's totals.
    """
    import sqlite3  # noqa: PLC0415
    from decimal import ROUND_HALF_UP, Decimal  # noqa: PLC0415

    from team_map import TEAM_ABBREV_TO_PLACE  # noqa: PLC0415

    def sv6(saves, shots):
        """savePercentage at 6dp, rounded half-up the way club-stats rounds.

        Python's round() is half-to-even, and a tie like 1404/1536 =
        0.9140625 then disagrees with the API by one unit in the last place
        (0.914063 there, 0.914062 here). 0 shots reads 0.0 - the value
        club-stats returns beside shotsAgainst 0.
        """
        if not shots:
            return 0.0
        return float((Decimal(saves) / Decimal(shots)).quantize(Decimal("0.000001"), ROUND_HALF_UP))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    params = (season,) if season else ()
    # Two fixed literals rather than one f-string with a WHERE interpolated:
    # S608 flags the f-string shape even when the interpolated piece is a
    # literal defined two lines above, and the duplication is one line.
    # Season is always a grouping key (even when filtered to one) because
    # club-stats is per-season: without it, a multi-season run would compare
    # each season's API row against every season summed.
    if season:
        goalie_sql = """
            SELECT season, player_id, team_abbrev,
                   COUNT(*) AS gp,
                   SUM(COALESCE(goals_against, 0)) AS ga,
                   SUM(COALESCE(shots_against, 0)) AS sa,
                   SUM(COALESCE(saves, 0)) AS sv_saves,
                   SUM(COALESCE(shutouts, 0)) AS so,
                   SUM(CASE WHEN decision = 'W' THEN 1 ELSE 0 END) AS w,
                   SUM(CASE WHEN decision = 'L' THEN 1 ELSE 0 END) AS l,
                   SUM(CASE WHEN decision = 'O' THEN 1 ELSE 0 END) AS o,
                   SUM(
                       CAST(substr(toi, 1, instr(toi, ':') - 1) AS INTEGER) * 60
                       + CAST(substr(toi, instr(toi, ':') + 1) AS INTEGER)
                   ) AS toi_sec
            FROM player_games
            WHERE season = ? AND position = 'G' AND goals_against IS NOT NULL
              AND substr(game_id, 5, 2) = '02'
            GROUP BY season, player_id, team_abbrev
        """
    else:
        goalie_sql = """
            SELECT season, player_id, team_abbrev,
                   COUNT(*) AS gp,
                   SUM(COALESCE(goals_against, 0)) AS ga,
                   SUM(COALESCE(shots_against, 0)) AS sa,
                   SUM(COALESCE(saves, 0)) AS sv_saves,
                   SUM(COALESCE(shutouts, 0)) AS so,
                   SUM(CASE WHEN decision = 'W' THEN 1 ELSE 0 END) AS w,
                   SUM(CASE WHEN decision = 'L' THEN 1 ELSE 0 END) AS l,
                   SUM(CASE WHEN decision = 'O' THEN 1 ELSE 0 END) AS o,
                   SUM(
                       CAST(substr(toi, 1, instr(toi, ':') - 1) AS INTEGER) * 60
                       + CAST(substr(toi, instr(toi, ':') + 1) AS INTEGER)
                   ) AS toi_sec
            FROM player_games
            WHERE position = 'G' AND goals_against IS NOT NULL
              AND substr(game_id, 5, 2) = '02'
            GROUP BY season, player_id, team_abbrev
        """

    # Season totals per (season, goalie, club) straight off the raw rows - the
    # same rows the window reads, so any drift shows up as a club-stats mismatch.
    # Regular season only (game id digit 6 = '02'): player_games also holds
    # playoff rows under the same season id, and club-stats game_type=2 does
    # not - a mixed total would over-count every playoff goalie.
    local = {}
    for r in conn.execute(goalie_sql, params):
        local[(r["season"], r["player_id"], r["team_abbrev"])] = r

    seasons = (
        [season] if season else [r[0] for r in conn.execute("SELECT DISTINCT season FROM player_games ORDER BY season")]
    )
    conn.close()

    total = matched = 0
    examples = []

    for season_id in seasons:
        for team in sorted(TEAM_ABBREV_TO_PLACE):
            try:
                payload = fetch_club_stats(team, season_id)
            except Exception as exc:  # noqa: BLE001 - a missing club is a data hole, not a crash
                # 404 is structural, not a fetch failure: UTA did not exist
                # before 2024-2025 and ARI became UTA after 2023-2024, so
                # every other season of that club legitimately has no page.
                if "404" not in str(exc):
                    print(f"  {season_id} {team}: club-stats fetch failed: {exc}")
                continue

            for goalie in payload.get("goalies") or []:
                pid = goalie.get("playerId")
                ours = local.get((season_id, pid, team))
                total += 1

                if ours is None:
                    if len(examples) < 8:
                        examples.append((season_id, team, pid, "no local appearance", None))
                    continue

                # Compare at 6dp - club-stats carries GAA/SV% to 6dp, and the
                # toi-second arithmetic is exact, so a real mismatch is not a
                # rounding artifact.
                # Count fields treat API null as 0 - a goalie with no wins has
                # wins: null in club-stats, 0 in our sum; same for every other
                # count. GAA/SV% keep None so a missing rate still fails.
                checks = [
                    ("gp", goalie.get("gamesPlayed") or 0, ours["gp"]),
                    ("w", goalie.get("wins") or 0, ours["w"]),
                    ("l", goalie.get("losses") or 0, ours["l"]),
                    ("o", goalie.get("overtimeLosses") or 0, ours["o"]),
                    ("ga", goalie.get("goalsAgainst") or 0, ours["ga"]),
                    ("sa", goalie.get("shotsAgainst") or 0, ours["sa"]),
                    ("so", goalie.get("shutouts") or 0, ours["so"]),
                    (
                        "gaa",
                        goalie.get("goalsAgainstAverage"),
                        round(3600.0 * ours["ga"] / ours["toi_sec"], 6) if ours["toi_sec"] else None,
                    ),
                    (
                        "sv",
                        goalie.get("savePercentage") or 0,
                        # saves / shots, not (sa - ga) / sa: club-stats'
                        # savePercentage is its saves field over shots, and
                        # official saves can exceed sa - ga (see
                        # player_games.saves). Rounded half-up like theirs;
                        # `or 0` maps an omitted field the same way the count
                        # fields map it - a real hole still fails the ratio.
                        sv6(ours["sv_saves"], ours["sa"]),
                    ),
                ]

                # Counts, toi, and SV% compare exactly (integer sums / integer
                # ratios). GAA gets its own looser tolerance: club-stats
                # publishes toi rounded to whole seconds but computes GAA from
                # unrounded ice time, so their 6dp GAA can differ from any
                # recomputation over their own published numbers by a few
                # millionths - observed up to 3e-6, bounded by ~2e-4 relative
                # for the sub-second per-game rounding. An accumulation bug
                # (wrong window, wrong team scope, wrong formula) is percent-
                # level, far outside this.
                bad = []
                for field, api, loc in checks:
                    if field == "gaa":
                        if api is None or loc is None:
                            if api != loc:
                                bad.append((field, api, loc))
                        elif abs(api - loc) > max(5e-7, 2e-4 * max(abs(api), abs(loc))):
                            bad.append((field, api, loc))
                    elif (api is None and loc is not None) or (api is not None and loc is None):
                        bad.append((field, api, loc))
                    elif isinstance(api, (int, float)) and isinstance(loc, (int, float)) and abs(api - loc) > 5e-7:
                        bad.append((field, api, loc))
                    elif isinstance(api, str) and isinstance(loc, str) and api != loc:
                        bad.append((field, api, loc))

                if bad:
                    if len(examples) < 8:
                        examples.append((season_id, team, pid, bad[0][0], bad))
                else:
                    matched += 1

    print(f"\ngoalie season-end vs club-stats: {matched}/{total} goalie-team totals match")
    for ex in examples:
        print(f"  {ex[0]} {ex[1]} player={ex[2]} {ex[3]} {ex[4] if ex[4] else ''}")
    return matched, total


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env", default=None, help="Override ENV.")
    parser.add_argument("--season", default=None, help="Limit the local side to one season.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--tol", type=int, default=2, help="Decimal places to compare at.")
    parser.add_argument(
        "--goalies",
        action="store_true",
        help="Compare season-end goalie totals against the NHL club-stats API (no archive needed).",
    )
    args = parser.parse_args()

    if args.goalies:
        validate_goalies(Path(args.db), args.season)
        return 0

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

        for field in ("gpg", "five_gpg", "hgpg", "hppg"):
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
    for field in ("gpg", "five_gpg", "hgpg", "hppg"):
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
