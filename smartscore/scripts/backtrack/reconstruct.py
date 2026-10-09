#!/usr/bin/env python3
"""Reconstruct season-to-date player stats from NHL game logs into Supabase.

WHY
---
``Player-Snapshots-{ENV}`` stores one row per player per date, captured live while
the pipeline runs. It is sparse (only pick-relevant dates, not every game played)
and lossy on early-season rows (rates stored at 2 decimal places where later rows
carry 6). That makes it unable to answer "what were this player's stats entering
game X" for an arbitrary X.

This script answers that by walking the NHL per-game box scores forward and
accumulating, then writing one row per player per date into
``Player-Snapshots-backtrack-{ENV}``. It is additive: the live archive is read for
comparison and never written.

THE DERIVATION
--------------
For each season and each date D, a player's totals sum their games with
``game_date`` strictly *before* D::

    gp_to_date = count(games before D)
    gpg        = goals_to_date / gp_to_date   (0 when gp_to_date == 0)

Strictly-before is the cutoff that reproduces the archive. Brett Kulak's stored
2024-03-02 row is 2/57, and 3/58 appears on 2024-03-03 once that night's goal
lands - so a row reads as "entering the game on D", which is what a pre-game pick
needs. On a player's first game gp_to_date is 0, and the archive stores 0 there
(not null), so this does too: 0/0 normalises to 0, keeping the table a drop-in
replacement.

Regular season only (gameTypeId 2). Kulak's 25-game, 1-goal 2024 playoff run
would otherwise inflate both numerator and denominator.

WHY ROWS COME FROM THE GAME LOG, NOT THE CALENDAR
-------------------------------------------------
A row is emitted for each date the player played - the game log's dates, not
every calendar date. The archive only ever stores game dates too (every one of
its 17,105 2023-24 rows sits on a game date), so the tables stay joinable
row-for-row without emitting ~10x the rows for dates nobody picks on. The one
coverage gap is playoff dates: the archive holds 1,398 rows there (its pipeline
kept running into the 2024 playoffs) and this regular-season-scope rebuild never
reproduces them. Non-game dates are not a gap - the archive has none.

WHAT IS AND IS NOT RECONSTRUCTED
--------------------------------
The game-log feed carries per-game goals, assists, points, shots, PIM, PP/SH
goals and points, OT goals, GWG, TOI, shifts, and the home/road flag. All of those
are accumulated here.

It does **not** carry ``opp_goalie_*``, ``lineup_*``, ``pp_unit``, or
``injury_status``. Those come from the lineup and injury endpoints and are out of
scope; the live archive remains their source. The corresponding columns exist in
the backtrack table but are never written here, so they stay null: null means
"not reconstructed", never "not applicable".

Usage::

    # One player, one season, print only. Fast and needs no database access.
    uv run python smartscore/scripts/backtrack/reconstruct.py --player 8476967 --season 20232024 --dry-run

    # Same player, plus a diff against the archive.
    uv run python smartscore/scripts/backtrack/reconstruct.py --player 8476967 --season 20232024

    # Full run. Discovers every player from box scores (tens of minutes); writes nothing.
    uv run python smartscore/scripts/backtrack/reconstruct.py --dry-run

    # Full run writing to the backtrack table.
    uv run python smartscore/scripts/backtrack/reconstruct.py --write

Idempotent: upserts on ``(date, player_id)``, so a re-run converges.

Environment: ``ENV`` (``dev``/``prod``, default ``dev``) picks both tables.
``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY`` are read by ``config.py``.
"""

import argparse
import os
import sys
from functools import cache

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from aws_lambda_powertools import Logger  # noqa: E402
from nhl_client import (  # noqa: E402
    DEFAULT_DELAY_SECONDS,
    fetch_boxscore,
    fetch_game_log,
    fetch_player_name,
    season_games,
)
from team_map import to_place  # noqa: E402

logger = Logger()


# config.py builds a Supabase client at import time, which raises when the
# credentials are absent. The derivation below is pure arithmetic and needs no
# database, so config is imported lazily - that keeps the reconstruction testable
# on a machine with no .env, and keeps a dry run from demanding write credentials.
#
# functools.cache does the memoising so there is no module-level mutable global.
@cache
def config():
    from config import ENV, SUPABASE_ADMIN_CLIENT  # noqa: PLC0415

    return ENV, SUPABASE_ADMIN_CLIENT


def snapshot_table():
    return f"Player-Snapshots-{config()[0]}"


def backtrack_table():
    return f"Player-Snapshots-backtrack-{config()[0]}"


# Same justification as player_archive.py: a row is ~30 columns, so 500 keeps the
# body small and stays under PostgREST's 1000-row default.
BATCH_SIZE = 500

# Every per-game integer summed into a *_to_date column.
_COUNT_FIELDS = (
    "goals",
    "assists",
    "points",
    "shots",
    "pim",
    "powerPlayGoals",
    "powerPlayPoints",
    "shorthandedGoals",
    "shorthandedPoints",
    "otGoals",
    "gameWinningGoals",
)


def _parse_toi_seconds(toi):
    """Convert an NHL "MM:SS" TOI string to seconds.

    A game with no TOI field yields 0 rather than None, so the running total stays
    an integer. Seconds are the stored unit because summing "20:49" strings would
    be a string concatenate, not arithmetic.
    """
    if not toi or ":" not in str(toi):
        return 0

    minutes, _, seconds = str(toi).partition(":")

    try:
        return int(minutes) * 60 + int(seconds)
    except ValueError:
        return 0


def _rate(numerator, denominator):
    """goals/games, or None when no games have been played.

    The None stays distinguishable here so callers choose the policy:
    reconstruct_player normalises it to 0 to match the archive's pre-debut rows.
    """
    if denominator <= 0:
        return None

    return numerator / denominator


def reconstruct_player(player_id, season, game_log, name=None, appearances=None):
    """Build one backtrack row per date from a player's season game log.

    The algorithm is a single forward pass rather than a per-date sum: walk the
    games in date order, and each date's row is the totals accumulated *before*
    that game. That is O(games) instead of O(dates x games), and it makes the
    strictly-before cutoff structural rather than something each row has to
    remember to apply.

    Only dates on which the player played get a row - including the first game,
    whose row carries gpg 0 (the archive's pre-debut convention, verified against
    rows where the player scored that very night). A player sitting out five
    games has no stats change across them, and emitting rows for those dates would
    duplicate one value many times over - which is not what the archive does
    either, since it only ever stores game dates.

    ``appearances`` is the ``{(date, player_id): {team_abbrev, home}}`` map from
    the box scores. When present it overrides the game log's own team fields,
    because the box score is the reliable source for those and the game log drops
    them on some rows. Absent, the game log's values are used.

    Returns (rows, game_count).
    """
    games = sorted(game_log, key=lambda g: g["gameDate"])

    totals = dict.fromkeys(_COUNT_FIELDS, 0)
    totals["toi_seconds"] = 0
    gp = 0

    rows = []
    game_count = len(games)

    for game in games:
        game_date = game["gameDate"]

        # Prefer the box score for team context; fall back to the game log.
        context = (appearances or {}).get((game_date, player_id))

        if context:
            team_abbrev = context.get("team_abbrev")
            is_home = context.get("home")
        else:
            team_abbrev = game.get("teamAbbrev")
            is_home = game.get("homeRoadFlag") == "H"

        # Emit the pre-game snapshot before folding this game into the totals.
        # The first game emits too: gp == 0 there, gpg's 0/0 normalises to 0 to
        # match the archive's pre-debut rows, and publish() never sees a null.
        gpg = _rate(totals["goals"], gp)
        rows.append(
            {
                "date": game_date,
                "player_id": player_id,
                # publish() guards the name this way; matching it keeps the
                # NOT NULL column fed even when a box score lacks a name.
                "name": name or f"player-{player_id}",
                "team_name": to_place(team_abbrev),
                "home": is_home,
                "gpg": 0.0 if gpg is None else gpg,
                # Only gpg is written. The raw counters it is computed from
                # (gp_to_date, goals_to_date, ...) live in
                # data/raw_nhl.sqlite as one row per player per game - see
                # local_store.py. The table's column set mirrors
                # Player-Snapshots-{ENV} so it can replace it, and that set has
                # no room for the counters. Anything else is added there and
                # projected in, not accumulated again here.
            }
        )

        gp += 1
        for field in _COUNT_FIELDS:
            totals[field] += game.get(field) or 0
        totals["toi_seconds"] += _parse_toi_seconds(game.get("toi"))

    return rows, game_count


def discover_players(games, delay_seconds=DEFAULT_DELAY_SECONDS):
    """Every player who appeared in ``games``, discovered from the games themselves.

    The roster comes from each game's box score rather than from
    ``Player-Snapshots-{ENV}``. That is deliberate: this table is meant to replace
    the archive, not supplement it, so the set of players must not be defined by
    who the old table happened to record. A player picked for the first time
    tomorrow shows up here because he played a game.

    Returns ``(players, appearances, goalies)``:

    * ``players`` - ``{player_id: name}`` for every distinct player seen.
    * ``appearances`` - ``{(date, player_id): {team_abbrev, home, ...}}``, the
      per-game context the box score is authoritative for.
    * ``goalies`` - the subset of ``players`` the box scores classify as position
      "G". Excluded from the snapshot: goalies have no meaningful gpg, and the
      archive never held them (their numbers live in ``opp_goalie_*`` instead).

    The box score is the source of truth for team and home/road, not the player's
    own game log. The game log omits ``teamAbbrev`` in some rows - ``ARI`` was
    missing this way and silently nulled every row involving Arizona - whereas the
    box score keys its stat blocks by side and always carries both abbrevs.
    """
    players = {}
    goalies = set()
    appearances = {}

    for index, game in enumerate(games, start=1):
        for record in fetch_boxscore(game["id"], delay_seconds=delay_seconds):
            player_id = record["player_id"]

            if player_id not in players:
                players[player_id] = record.get("name")

            # Position comes straight from the box score's stat block (forwards /
            # defense / goalies), so this is the roster's own classification rather
            # than an inference. Guessing from the stats would be wrong: a skater
            # who never recorded a shot looks identical to a goalie on goals=0,
            # shots=0.
            if record.get("position") == "G":
                goalies.add(player_id)

            appearances[(game["date"], player_id)] = {
                "team_abbrev": record.get("team_abbrev"),
                "home": record.get("home"),
            }

        if index % 100 == 0:
            logger.info(f"  scanned {index}/{len(games)} games, {len(players)} player(s) so far")

    return players, appearances, goalies


def archive_names(player_ids):
    """Names for a specific set of players, straight from the archive.

    Exists so a subset run can label its rows without paging the whole archive.
    ``in_`` needs a non-empty list; an empty one is a filter that matches nothing
    and Supabase rejects it.
    """
    if not player_ids:
        return []

    _, supabase = config()
    response = supabase.table(snapshot_table()).select("player_id,name").in_("player_id", player_ids).execute()

    seen = {}
    for record in response.data or []:
        seen.setdefault(record["player_id"], record.get("name"))

    return [{"player_id": pid, "name": name} for pid, name in seen.items()]


def write_rows(rows):
    """Upsert rows into the backtrack table, batching by BATCH_SIZE."""
    _, supabase = config()
    written = 0

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        supabase.table(backtrack_table()).upsert(
            batch,
            on_conflict="date,player_id",
            returning="minimal",
        ).execute()
        written += len(batch)

    return written


def diff_against_archive(rows, archive_rows):
    """Compare reconstructed rows against the archive on the shared keys.

    Compares at a tolerance matched to the archive's storage precision rather than
    exactly: early-season archive rows hold 2 decimal places, so an exact
    comparison would report rounding as disagreement.
    """
    reconstructed = {(r["date"], r["player_id"]): r for r in rows}
    compared = 0
    matched = 0
    differences = []

    for record in archive_rows:
        key = (record["date"], record.get("player_id"))
        row = reconstructed.get(key)

        if row is None:
            continue

        compared += 1

        # Only `gpg` is compared. `hgpg` is deliberately absent from this table -
        # it is a 3-year window in the archive and cannot be rebuilt from one
        # season of game logs, so comparing it would report a false difference on
        # every row.
        for field in ("gpg",):
            stored = record.get(field)
            if stored is None:
                continue

            mine = row.get(field)
            if mine is None:
                differences.append((key, field, stored, mine))
                continue

            # Round the stored value to the precision it actually carries, then
            # compare. A 2dp stored value is not expected to match a 6dp number.
            decimals = len(str(stored).split(".")[1]) if "." in str(stored) else 0
            if abs(round(mine, decimals) - round(stored, decimals)) < 1e-9:
                matched += 1
            else:
                differences.append((key, field, stored, round(mine, 6)))

    return compared, matched, differences


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--player",
        type=int,
        help="Single NHL player id. Omit to process every player who played that season.",
    )
    parser.add_argument(
        "--season",
        default="20232024",
        help="NHL season id, e.g. 20232024. Default 20232024.",
    )
    parser.add_argument(
        "--players",
        help="Comma-separated player ids to process. Skips box score discovery, the slow part of a full run.",
    )
    parser.add_argument("--limit", type=int, help="Only the first N discovered players.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--dry-run", action="store_true", help="Reconstruct and report; write nothing.")
    parser.add_argument("--write", action="store_true", help="Upsert into the backtrack table.")
    args = parser.parse_args()

    write = args.write and not args.dry_run

    # Player discovery comes from the season's games, not from the archive. This
    # is the replacement, so the player set must be defined by who actually
    # played - not by who the old table happened to record.
    if args.player:
        name = fetch_player_name(args.player, delay_seconds=args.delay)
        targets = [(args.player, name)]
        # No box scores crawled for a spot check, so team context falls back to
        # the game log (see the --players branch below for the full rationale).
        appearances = {}
    elif args.players:
        # Explicit ids. Names are resolved from the archive when it has them and
        # otherwise from the landing endpoint, so the rows still carry a name
        # without crawling every box score.
        ids = [int(p) for p in args.players.split(",") if p.strip()]
        known = {r["player_id"]: r["name"] for r in archive_names(ids)}
        targets = [(pid, known.get(pid) or fetch_player_name(pid, delay_seconds=args.delay)) for pid in ids]
        logger.info("Explicit player list given - skipping box score discovery")
        # A subset run has no box scores, so there is no position data and nothing
        # to exclude on. That is fine for spot checks; the full path below is what
        # guarantees goalies never enter the table.
        appearances = {}
        goalies = set()
    else:
        logger.info(f"Loading {args.season} schedule")
        games = season_games(args.season, delay_seconds=args.delay)
        logger.info(f"{len(games)} regular-season game(s) found")

        logger.info("Discovering players from box scores")
        discovered, appearances, goalies = discover_players(games, delay_seconds=args.delay)
        logger.info(f"{len(discovered)} distinct player(s) discovered, {len(goalies)} goalie(s) excluded")

        targets = sorted((pid, discovered.get(pid)) for pid in discovered if pid not in goalies)

        if args.limit:
            targets = targets[: args.limit]

    logger.info(f"Reconstructing {len(targets)} player(s) for season {args.season}")

    all_rows = []
    players_with_games = 0

    # Checkpoint periodically rather than accumulating to the end. Discovery
    # plus the per-player game log crawl is tens of minutes of requests for a
    # full season, so a single deferred write loses the entire run to any
    # interruption; incremental upserts mean partial progress survives and the
    # table fills while the crawl is still going. Checkpoints fire every
    # CHECKPOINT_ROWS rows rather than every N players: a player contributes
    # anywhere from 1 to 82 of them. Upserts are idempotent on (date, player_id),
    # so a re-run simply rewrites the same rows.
    CHECKPOINT_ROWS = 250

    for index, (player_id, name) in enumerate(targets, start=1):
        game_log = fetch_game_log(player_id, args.season, delay_seconds=args.delay)

        if not game_log:
            continue

        players_with_games += 1

        rows, _ = reconstruct_player(
            player_id,
            args.season,
            game_log,
            name=name,
            appearances=appearances,
        )
        all_rows.extend(rows)

        if index % 25 == 0:
            logger.info(f"{index}/{len(targets)} players, {len(all_rows)} rows so far")

        if write and len(all_rows) >= CHECKPOINT_ROWS:
            checkpointed = write_rows(all_rows)
            logger.info(f"checkpoint: wrote {checkpointed} row(s) for {index}/{len(targets)} players")
            all_rows = []

    logger.info(f"{players_with_games} player(s) had games; {len(all_rows)} row(s) pending")

    if args.player:
        _, supabase = config()
        # Season window: October of its first year through June of its second.
        # Calendar January would pull in the tail of the previous season.
        archive_rows = (
            supabase.table(snapshot_table())
            .select("date,player_id,gpg")
            .eq("player_id", args.player)
            .gte("date", f"{args.season[:4]}-10-01")
            .lte("date", f"{int(args.season[:4]) + 1}-06-30")
            .execute()
        ).data or []

        compared, matched, differences = diff_against_archive(all_rows, archive_rows)
        logger.info(f"archive comparison: {matched}/{compared} rows agree")

        for key, field, stored, mine in differences[:15]:
            logger.warning(f"  differs {key} {field}: archive={stored} backtrack={mine}")

    if write:
        # Flush whatever the last checkpoint did not cover.
        if all_rows:
            written = write_rows(all_rows)
            logger.info(f"Final flush: wrote {written} row(s) to {backtrack_table()}")
        else:
            logger.info("Nothing pending; all rows already checkpointed")
    else:
        logger.info(f"Dry run - {len(all_rows)} row(s) not written. Pass --write to upsert.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
