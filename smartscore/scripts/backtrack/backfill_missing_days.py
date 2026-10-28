"""Gap-filler: top up Player-Snapshots-backtrack-{ENV} for the recent completed
game days the nightly publish has not covered yet.

WHY (stage-2 exactness): the daily picks pass recomputes today's rolling/
career shot features EXACTLY from the published per-game raw columns
(game_goals / game_shots / game_toi_min on Player-Snapshots-backtrack-{env},
added by supabase/migrations/20261027_add_per_game_raw_columns_player_
snapshots_backtrack.sql). A published day with no rows would silently leave
those games out of every player's shift(1)/rolling window, and a LIVE game's
box score fetched early would freeze hostages in it - so this helper:

  1. Reads the table's latest published date (one query).
  2. For EVERY completed game date strictly after it, up to ``--through-date``
     (default: ET-yesterday; usually exactly one day):
       * one schedule request for that date (nhl_client.fetch_schedule), games
         filtered to gameType 2/3 AND an explicitly-terminal game state
         (FINAL/OFF - the fetch_boxscore cache-hostage guard: a box score is
         never requested for a LIVE/FUT game, so no in-flight line-up can be
         cached as final);
       * one boxscore request per FINAL game (nhl_client.fetch_boxscore);
         typical cost is 2-3 requests per game when counting the schedule
         window the day shares, well bounded;
       * every SKATER row (position != 'G' - goalies live on
         Goalie-Snapshots-backtrack, matching local_store.publish) becomes an
         upsert of ONLY the raw columns (+ the PK + name):
         on_conflict="date,player_id", PostgREST merge-duplicates updates just
         the provided keys, so a row the publish already wrote keeps every
         other column untouched (idempotent re-runs safe).
  3. DOES NOT recompute the 16 rolling columns: those belong to the training-
     side history the nightly publish/attach carries.

Sweep bounds: at most MAX_MISSING_DAYS calendar days are processed in one run
(a longer gap needs the local-store top-up path, publish_shot_cols.py, not an
API crawl); each missing day costs 1 schedule request + 1 request per FINAL
game played that day (~12-16 games/night worst case).

Usage (the ml-picks workflow's first step, before inference)::

    uv run python smartscore/scripts/backtrack/backfill_missing_days.py --env prod
    uv run python smartscore/scripts/backtrack/backfill_missing_days.py --env prod --dry
    uv run python smartscore/scripts/backtrack/backfill_missing_days.py --env prod --through-date 2026-10-27
"""

import argparse
import datetime
import sys
from pathlib import Path

_here = Path(__file__).resolve()
_root = _here.parents[3]
sys.path.insert(0, str(_root / "smartscore"))
sys.path.insert(1, str(_here.parent))

import nhl_client  # noqa: E402
from nhl_client import fetch_boxscore, fetch_schedule  # noqa: E402

from config import SUPABASE_ADMIN_CLIENT  # noqa: E402

# The only game states this backfill will fetch a box score for. FINAL and OFF
# both mean completed (nhl_client.season_games lines 221-222); anything else -
# including an in-flight LIVE whose box score the fetch_boxscore cache would
# then hold hostage for the rest of the day - is skipped and re-processed on a
# later pass once it finishes.
TERMINAL_GAME_STATES = {"FINAL", "OFF"}
REGULAR_SEASON, PLAYOFFS = nhl_client.REGULAR_SEASON, nhl_client.PLAYOFFS

MAX_MISSING_DAYS = 14


def player_table(env):
    return f"Player-Snapshots-backtrack-{env}"


def latest_published_date(env):
    """The newest date with any published snapshot row (None when empty)."""
    rows = (
        SUPABASE_ADMIN_CLIENT.table(player_table(env)).select("date").order("date", desc=True).limit(1).execute().data
        or []
    )
    return rows[0]["date"] if rows else None


def _record_rows(game_id, records):
    """Per-player raw upsert rows for one game's boxscore records.

    Skaters only (position != 'G'), matching local_store.publish's shape
    (goalies live on Goalie-Snapshots-backtrack). toi "MM:SS" -> decimal
    minutes (the parsing the raw store + the inference core both run);
    goals/shots stay exact ints; a missing/None toi ships as None - an honest
    unknown, and the inference core drops raw-incomplete rows with a warning
    rather than inventing a zero.
    """
    rows = []
    for record in records:
        position = record.get("position")
        if position == "G":
            continue
        if not record.get("game_date"):
            raise RuntimeError(f"boxscore request returned a player row with no game_date (game {game_id})")
        toi = record.get("toi")
        if isinstance(toi, str) and ":" in toi:
            minutes = int(toi.split(":")[0])
            toi_min = minutes + int(toi.split(":")[1]) / 60
        else:
            toi_min = None if toi in (None, "") else float(toi)
        rows.append(
            {
                "date": record.get("game_date"),
                "player_id": int(record["player_id"]),
                "name": record.get("name") or f"player-{record['player_id']}",
                "game_goals": int(record.get("goals") or 0),
                "game_shots": int(record.get("shots") or 0),
                "game_toi_min": toi_min,
            }
        )
    if rows and any(row.get("date") is None for row in rows):  # pragma: no cover - guarded above
        raise RuntimeError("boxscore request returned a player row with no game_date")
    return rows


def _upsert_rows(table, rows, batch_size=500):
    """Merge-duplicates upsert of the raw columns only.

    No other column is in the payload, so an existing published row is
    top-upped, never overwritten (PostgREST resolves merge-duplicates by
    updating exactly the keys present in the payload).
    """
    written = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        SUPABASE_ADMIN_CLIENT.table(table).upsert(batch, on_conflict="date,player_id", returning="minimal").execute()
        written += len(batch)
    return written


def days_between(lo, through):
    """One day strictly after ``lo`` through ``through`` inclusive, ascending,
    bounded (a sweep guard: only ``MAX_MISSING_DAYS`` days per run)."""
    missing = []
    cursor = datetime.date.fromisoformat(lo) + datetime.timedelta(days=1) if lo else datetime.date.max
    final = datetime.date.fromisoformat(through)
    while cursor <= final:
        missing.append(cursor.isoformat())
        cursor += datetime.timedelta(days=1)
    return missing[:MAX_MISSING_DAYS]


def backfill(env, through_date=None, dry=False, delay_seconds=None):
    """The chain (read latest -> schedule per day -> boxscore per FINAL game ->
    upsert). Returns (games_processed, rows_upserted)."""
    table = player_table(env)
    through = through_date or (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    latest = latest_published_date(env)
    if latest is None:
        raise RuntimeError(f"{table} is empty - run the publish/backfill chain first; dry-raw is not the bootstrap")
    if latest >= through:
        print(f"{table}'s latest published date is {latest} (through {through}): nothing to backfill")
        return 0, 0
    missing = days_between(latest, through)
    if not missing:
        print(f"{table}: no completed-game window between {latest} and {through}")
        return 0, 0
    if len(missing) == MAX_MISSING_DAYS:
        print(
            f"WARNING: a {MAX_MISSING_DAYS}-day gap between {latest} and {through} stays OPEN at its tail - "
            "walls of >14 missing days need the offline local-store top-up (publish_shot_cols.py), "
            "not this bounded helper",
            flush=True,
        )
    delay = nhl_client.DEFAULT_DELAY_SECONDS if delay_seconds is None else float(delay_seconds)
    games_done, rows_written = 0, 0
    for day in missing:
        payload = fetch_schedule(day, delay_seconds=delay)
        day_games = [
            game
            for week in payload.get("gameWeek") or []
            for game in week.get("games") or []
            if week.get("date") == day
            and game.get("gameType") in (REGULAR_SEASON, PLAYOFFS)
            and str(game.get("gameState", "")).upper() in TERMINAL_GAME_STATES
        ]
        skipped = sum(
            1
            for week in payload.get("gameWeek") or []
            for game in week.get("games") or []
            if week.get("date") == day and game.get("gameType") in (REGULAR_SEASON, PLAYOFFS)
        ) - len(day_games)
        if skipped:
            print(f"{day}: skipping {skipped} game(s) in a non-terminal state (FINAL-only guard)", flush=True)
        if not day_games:
            continue
        day_written = 0
        for game in day_games:
            records = fetch_boxscore(game["id"], delay_seconds=delay)
            rows = _record_rows(game["id"], records)
            if not rows:
                continue
            games_done += 1
            if dry:
                print(f"{day}: game {game['id']} dry-read of {len(rows)} skater row(s) (no write)", flush=True)
                day_written += len(rows)
                continue
            day_written += _upsert_rows(table, rows)
        rows_written += day_written
        print(f"{day}: {len(day_games)} FINAL game(s), {day_written} raw column(s) {'read' if dry else 'upserted'}")
    print(f"backfill {'(dry)' if dry else ''}: {games_done} game(s), {rows_written} player-game row(s)")
    return games_done, rows_written


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="prod", choices=["dev", "prod"], help="backtrack table suffix (default prod)")
    parser.add_argument(
        "--through-date", default=None, help="last date to backfill through, YYYY-MM-DD (default: ET-yesterday)"
    )
    parser.add_argument("--dry", action="store_true", help="read-only: no upserts, log what WOULD be written")
    parser.add_argument("--delay", type=float, help="seconds between NHL API requests (default: nhl_client's)")
    args = parser.parse_args()
    backfill(args.env, through_date=args.through_date, dry=args.dry, delay_seconds=args.delay)
    return 0


if __name__ == "__main__":
    sys.exit(main())
