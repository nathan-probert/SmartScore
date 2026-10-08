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
    gpg        = goals_to_date / gp_to_date   (null when gp_to_date == 0)

Strictly-before is the cutoff that reproduces the archive. Brett Kulak's stored
2024-03-02 row is 2/57, and 3/58 appears on 2024-03-03 once that night's goal
lands - so a row reads as "entering the game on D", which is what a pre-game pick
needs.

Regular season only (gameTypeId 2). Kulak's 25-game, 1-goal 2024 playoff run
would otherwise inflate both numerator and denominator.

WHY DATES COME FROM THE SNAPSHOT, NOT THE CALENDAR
---------------------------------------------------
Rows are written for the dates that already exist in ``Player-Snapshots-{ENV}``
rather than every calendar date in the season. Two reasons: the output stays
directly joinable against the live archive row-for-row, and a full calendar would
emit ~10x the rows for dates nobody ever picks on. Use ``--all-dates`` to emit the
full calendar instead.

WHAT IS AND IS NOT RECONSTRUCTED
--------------------------------
The game-log feed carries per-game goals, assists, points, shots, PIM, PP/SH
goals and points, OT goals, GWG, TOI, shifts, and the home/road flag. All of those
are accumulated here.

It does **not** carry ``opp_goalie_*``, ``lineup_*``, ``pp_unit``, or
``injury_status``. Those come from the lineup and injury endpoints and are out of
scope; the live archive remains their source. The corresponding columns are
absent from the backtrack table rather than written as null, so a null here means
"not reconstructed" and never "not applicable".

Usage::

    # One player, one season, print only. Safe first step.
    uv run python smartscore/scripts/backtrack/reconstruct.py --dry-run

    # One player against the archive, with a diff.
    uv run python smartscore/scripts/backtrack/reconstruct.py --player 8476967 --season 20232024

    # Full run.
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

    None rather than 0 so "has not played yet" stays distinguishable from "played
    and scored none".
    """
    if denominator <= 0:
        return None

    return numerator / denominator


def _opponent_for(team_abbrev, game):
    """Work out the opponent abbreviation when the box score is the team source.

    The box score gives us the player's own club from the side their stat block
    came from. The opponent is the other one, and it is not carried per player -
    only per game - so it is recovered by matching ``home``/``away`` against the
    schedule entry for that game. Falling back to the game log keeps a
    mid-season trade (where the log records the new club immediately but the
    schedule lookup is keyed on the game) from blanking the column.
    """
    if team_abbrev and game.get("away") and game.get("home"):
        return game["away"] if team_abbrev == game["home"] else game["home"]

    return game.get("opponentAbbrev")


def reconstruct_player(player_id, season, game_log, name=None, appearances=None):
    """Build one backtrack row per date from a player's season game log.

    The algorithm is a single forward pass rather than a per-date sum: walk the
    games in date order, and each date's row is the totals accumulated *before*
    that game. That is O(games) instead of O(dates x games), and it makes the
    strictly-before cutoff structural rather than something each row has to
    remember to apply.

    Only dates on which the player played get a row. A player sitting out five
    games has no stats change across them, and emitting rows for those dates would
    duplicate one value many times over - which is not what the archive does
    either, since it stores one row per capture date.

    ``appearances`` is the ``{(date, player_id): {team_abbrev, home}}`` map from
    the box scores. When present it overrides the game log's own team fields,
    because the box score is the reliable source for those and the game log drops
    them on some rows. Absent, the game log's values are used.

    Returns (rows, game_count).
    """
    games = sorted(game_log, key=lambda g: g["gameDate"])

    totals = dict.fromkeys(_COUNT_FIELDS, 0)
    totals["toi_seconds"] = 0
    home_gp = home_goals = away_gp = away_goals = 0
    gp = 0

    rows = []
    game_count = len(games)

    for game in games:
        game_date = game["gameDate"]
        goals_in_game = game.get("goals") or 0

        # Prefer the box score for team context; fall back to the game log.
        context = (appearances or {}).get((game_date, player_id))

        if context:
            team_abbrev = context.get("team_abbrev")
            is_home = context.get("home")
        else:
            team_abbrev = game.get("teamAbbrev")
            is_home = game.get("homeRoadFlag") == "H"

        # Emit the pre-game snapshot before folding this game into the totals.
        if gp > 0:
            rows.append(
                {
                    "date": game_date,
                    "player_id": player_id,
                    "name": name,
                    "team_name": to_place(team_abbrev),
                    "home": is_home,
                    "gpg": _rate(totals["goals"], gp),
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

        if is_home:
            home_gp += 1
            home_goals += goals_in_game
        else:
            away_gp += 1
            away_goals += goals_in_game

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


def get_players(limit=None):
    """DEPRECATED - player discovery moved to discover_players().

    Kept only as a cross-check: reading the archive's player list is how a subset
    run can be told apart from a full one. The production path never calls this,
    because the player set must not come from the table being replaced.
    """
    _, supabase = config()
    query = supabase.table(snapshot_table()).select("player_id,name").order("player_id")

    seen = {}
    page_size = 1000
    offset = 0

    while True:
        response = query.range(offset, offset + page_size - 1).execute()
        batch = response.data or []

        if not batch:
            break

        for record in batch:
            player_id = record.get("player_id")
            if player_id is not None and player_id not in seen:
                seen[player_id] = record.get("name")

        if len(batch) < page_size:
            break

        offset += page_size

    targets = sorted(seen.items())

    return targets[:limit] if limit else targets


def archive_names(player_ids):
    """Names for a specific set of players, straight from the archive.

    Exists so a subset run can label its rows without paging the whole archive.
    ``in_`` needs a non-empty list; an empty one is a filter that matches nothing
    and Supabase rejects it.
    """
    if not player_ids:
        return []

    _, supabase = config()
    response = (
        supabase.table(snapshot_table())
        .select("player_id,name")
        .in_("player_id", player_ids)
        .execute()
    )

    seen = {}
    for record in response.data or []:
        seen.setdefault(record["player_id"], record.get("name"))

    return [{"player_id": pid, "name": name} for pid, name in seen.items()]


def get_archive_dates(player_id):
    """The dates the archive holds for one player."""
    _, supabase = config()
    response = (
        supabase.table(snapshot_table())
        .select("date")
        .eq("player_id", player_id)
        .order("date")
        .execute()
    )

    return [r["date"] for r in (response.data or [])]


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
        help="Single NHL player id. Omit to process every player in the archive.",
    )
    parser.add_argument(
        "--season",
        default="20232024",
        help="NHL season id, e.g. 20232024. Default 20232024.",
    )
    parser.add_argument(
        "--players",
        help="Comma-separated player ids to process. Skips the archive scan, which is the "
        "slow part of a subset run - see get_players().",
    )
    parser.add_argument("--limit", type=int, help="Only the first N players.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--all-dates", action="store_true", help="Emit every calendar date in the season.")
    parser.add_argument("--dry-run", action="store_true", help="Reconstruct and report; write nothing.")
    parser.add_argument("--write", action="store_true", help="Upsert into the backtrack table.")
    args = parser.parse_args()

    write = args.write and not args.dry_run

    if args.player:
        name = fetch_player_name(args.player, delay_seconds=args.delay)
        targets = [(args.player, name)]
    elif args.players:
        # Explicit ids. Names are left to the caller-supplied list when given, and
        # otherwise resolved from the landing endpoint, so the rows still carry a
        # name without a 140-request archive scan.
        ids = [int(p) for p in args.players.split(",") if p.strip()]
        known = {r["player_id"]: r["name"] for r in archive_names(ids)}
        targets = [(pid, known.get(pid) or fetch_player_name(pid, delay_seconds=args.delay)) for pid in ids]
        logger.info(f"Reconstructing {len(targets)} explicit player(s) for season {args.season}")
    else:
        logger.info("Loading player list from the archive")
        targets = get_players(limit=args.limit)
        logger.info(f"Reconstructing {len(targets)} player(s) for season {args.season}")

    # Player discovery comes from the season's games, not from the archive. This is
    # the replacement, so the player set must be defined by who actually played.
    if args.players or args.player:
        logger.info("Explicit player list given - skipping box score discovery")
        appearances = {}
        schedule_by_id = {}
        # A subset run has no box scores, so there is no position data and nothing
        # to exclude on. That is fine for spot checks; the full path below is what
        # guarantees goalies never enter the table.
        goalies = set()
    else:
        logger.info(f"Loading {args.season} schedule")
        games = season_games(args.season, delay_seconds=args.delay)
        logger.info(f"{len(games)} regular-season game(s) found")

        schedule_by_id = {g["id"]: g for g in games}
        logger.info("Discovering players from box scores")

        discovered, appearances, goalies = discover_players(games, delay_seconds=args.delay)
        logger.info(f"{len(discovered)} distinct player(s) discovered, {len(goalies)} goalie(s) excluded")

        targets = sorted(pid for pid in discovered if pid not in goalies)

    targets = [(pid, discovered.get(pid)) for pid in targets]

    all_rows = []
    players_with_games = 0

    # Checkpoint every CHECKPOINT_EVERY players rather than accumulating to the
    # end. A full season is ~80 minutes of requests, so a single deferred write
    # loses the entire run to any interruption; incremental upserts mean partial
    # progress survives and the table fills while the crawl is still going.
    # Upserts are idempotent on (date, player_id), so a re-run simply rewrites the
    # same rows.
    CHECKPOINT_EVERY = 25

    for index, (player_id, name) in enumerate(targets, start=1):
        game_log = fetch_game_log(player_id, args.season, delay_seconds=args.delay)

        if not game_log:
            continue

        players_with_games += 1

        # Attach each game log row to its schedule entry so team context can be
        # recovered from the box score's side keys.
        enriched = []
        for game in game_log:
            merged = dict(game)
            schedule_entry = schedule_by_id.get(game.get("gameId"))
            if schedule_entry:
                merged["away"] = schedule_entry.get("away")
                merged["home"] = schedule_entry.get("home")
            enriched.append(merged)

        rows, _ = reconstruct_player(
            player_id,
            args.season,
            enriched,
            name=name,
            appearances=appearances,
        )
        all_rows.extend(rows)

        if index % 25 == 0:
            logger.info(f"{index}/{len(targets)} players, {len(all_rows)} rows so far")

        if write and len(all_rows) >= CHECKPOINT_EVERY * 10:
            checkpointed = write_rows(all_rows)
            logger.info(f"checkpoint: wrote {checkpointed} row(s) for {index}/{len(targets)} players")
            all_rows = []

    logger.info(f"{players_with_games} player(s) had games; {len(all_rows)} row(s) pending")

    if args.player:
        _, supabase = config()
        archive_rows = (
            supabase.table(snapshot_table())
            .select("date,player_id,gpg")
            .eq("player_id", args.player)
            .gte("date", f"{args.season[:4]}-01-01")
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
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
