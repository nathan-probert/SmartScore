"""Gap-filler: top up Player-Snapshots-backtrack-{ENV} for the recent completed
game days the nightly publish has not covered yet.

WHY (stage-2 exactness): the daily picks pass recomputes today's rolling/
career shot features EXACTLY from the published per-game raw columns
(game_goals / game_shots / game_points / game_toi_min on Player-Snapshots-
backtrack-{env}, added by supabase/migrations/20261027_add_per_game_raw_
columns_player_snapshots_backtrack.sql). A published day with no rows would
silently leave those games out of every player's shift(1)/rolling window, so
this helper - the run's FIRST step of the day (run_daily_picks.run invokes it
before any history is read; the ml-picks workflow runs it as its own first
step too, where the per-date markers make the second call a no-op skip):

  1. Reads the table's latest published date L (one query) and the dates the
     marker table (Player-Game-Raw-Markers-{env}, same migration) already
     recorded as fully backfilled.
  2. For EVERY game date strictly after L, up to ``--through-date``
     (default: ET-yesterday; usually exactly one day), NOT yet marked:
       * one schedule request for that date (nhl_client.fetch_schedule), games
         filtered to gameType 2/3 AND an explicitly-terminal game state
         (FINAL/OFF - the fetch_boxscore cache-hostage guard: a box score is
         never requested for a LIVE/FUT game, so no in-flight line-up can be
         cached as final);
       * one FRESH boxscore request per FINAL game - the cache-hostage
         hazard cuts both ways: a boxscore file cached while the game was
         still LIVE (an earlier crawl that raced the clock) would otherwise
         be served forever, so the helper DELETES any existing
         ``boxscore-<id>.json`` cache file before every fetch and lets
         fetch_boxscore re-GET and re-cache the final data (nhl_client is
         not modified; the override lives entirely here);
       * every SKATER row (position != 'G' - goalies live on
         Goalie-Snapshots-backtrack, matching local_store.publish) becomes an
         upsert keyed (date, player_id), in two lanes so no published column
         is ever clobbered:
           - a row the table already owns gets ONLY the raw block
             (game_shots/game_goals/game_points/game_toi_min) - PostgREST
             merge-duplicates updates exactly the payload's keys, so the 16
             precomputed rolling columns and every other published column
             are untouched;
           - a row the table lacks is CREATED as a minimal snapshot shape:
             identity columns (date, player_id, season, name, team_abbrev,
             opponent_abbrev, scored) + the raw block, with the rolling
             cells left NULL for the nightly publish to fill forward.
       * a per-date marker row is written AFTER the upserts land - a date
         whose games were not all final, or whose writes failed, stays
         UNMARKED and is retried by a later pass; a marked date costs the
         next run NOTHING (no schedule request, no writes): the
         first-run-of-day backfill runs once and every same-day re-run is a
         pure skip.
  3. DOES NOT recompute the 16 rolling columns: those belong to the training-
     side history the nightly publish/attach carries.

Retry envelope per the repo's patterns: three tries per batch with
exponential backoff (nhl_client._get_json's shape); one failed batch fails
the DATE (unmarked, retried later), never the whole run.

Sweep bounds: at most MAX_MISSING_DAYS calendar days are processed in one run
(a longer gap needs the local-store top-up path, publish_shot_cols.py, not an
API crawl); each missing day costs 1 schedule request + 1 request per FINAL
game played that day (~12-16 games/night worst case). Progress logs every
5,000 upserted rows.

Usage (the ml-picks workflow's first step, before inference)::

    uv run python smartscore/scripts/backtrack/backfill_missing_days.py --env prod
    uv run python smartscore/scripts/backtrack/backfill_missing_days.py --env prod --dry
    uv run python smartscore/scripts/backtrack/backfill_missing_days.py --env prod --through-date 2026-10-27
"""

import argparse
import datetime
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

_here = Path(__file__).resolve()
_root = _here.parents[3]
sys.path.insert(0, str(_root / "smartscore"))
sys.path.insert(1, str(_here.parent))

import nhl_client  # noqa: E402
from nhl_client import CACHE_DIR, fetch_boxscore, fetch_schedule  # noqa: E402

from config import SUPABASE_ADMIN_CLIENT  # noqa: E402

# The only game states this backfill will fetch a box score for. FINAL and OFF
# both mean completed (nhl_client.season_games lines 221-222); anything else -
# including an in-flight LIVE whose box score the fetch_boxscore cache would
# then hold hostage for the rest of the day - is skipped and re-processed on a
# later pass once it finishes.
TERMINAL_GAME_STATES = {"FINAL", "OFF"}
REGULAR_SEASON, PLAYOFFS = nhl_client.REGULAR_SEASON, nhl_client.PLAYOFFS

MAX_MISSING_DAYS = 14
UPSERT_BATCH = 500
UPSERT_RETRIES = 3
PROGRESS_EVERY = 5_000

# The per-game RAW block this helper writes (migration 20261027) - exactly
# what run_daily_picks.shot_block_from_history feeds model
# .shot_features_from_games (goals/shots/toi; points rides along as the
# per-game provenance the migration ships).
RAW_BLOCK_COLUMNS = ("game_shots", "game_goals", "game_points", "game_toi_min")

MARKER_TABLE_PREFIX = "Player-Game-Raw-Markers-"

_RAW_SCHEMA_PROBE: dict[str, bool] = {}


def raw_columns_landed(table, probe_column="game_shots"):
    """True once migration 20261027's per-game raw columns exist on ``table``.

    Pre-migration runs must NOT send the raw keys (PostgREST 400s any request
    naming an unknown column) and the marker table may not exist either, so
    this probe is the graceful-degrade gate: until the DDL lands (deploy.yml's
    migrate job applies it on merge to main; label a 'deploy' PR for dev) the
    helper skips with ONE loud warning and zero writes - the authorized
    first-run backfill becomes possible with the same one command post-merge.
    Cached per table for the process lifetime."""
    placed = _RAW_SCHEMA_PROBE.get(table)
    if placed is None:
        try:
            SUPABASE_ADMIN_CLIENT.table(table).select(probe_column).limit(1).execute()
            placed = True
        except Exception:  # noqa: BLE001 - any failure means "not landed yet"
            placed = False
        _RAW_SCHEMA_PROBE[table] = placed
    return placed


def player_table(env):
    return f"Player-Snapshots-backtrack-{env}"


def marker_table(env):
    return f"{MARKER_TABLE_PREFIX}{env}"


def season_label(on_date):
    """The NHL season id ('20262027') a slate date belongs to. Same rule as
    run_daily_picks.season_label (not imported: that module pulls boto3 and
    the full inference stack, and this helper must stay light) - the season
    that started the prior October runs through June, with the COVID-era
    exceptions (Jul-Sep 2020 games key 20192020, July 2021 keys 20202021 -
    the store's own ids)."""
    day = datetime.date.fromisoformat(on_date)
    if datetime.date(2020, 7, 1) <= day <= datetime.date(2020, 9, 30):
        return "20192020"
    if datetime.date(2021, 7, 1) <= day <= datetime.date(2021, 7, 31):
        return "20202021"
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}{start + 1}"


def yesterday_et(now=None):
    """Yesterday's date in ET - the default ``--through-date``."""
    now = now or datetime.datetime.now(tz=ZoneInfo("America/Toronto"))
    return (now.date() - datetime.timedelta(days=1)).isoformat()


def latest_published_date(env):
    """The newest date with any published snapshot row (None when empty)."""
    rows = (
        SUPABASE_ADMIN_CLIENT.table(player_table(env)).select("date").order("date", desc=True).limit(1).execute().data
        or []
    )
    return rows[0]["date"] if rows else None


def marked_dates(env, after_date):
    """Dates the marker table records as fully backfilled (>= after_date).

    The read is the cheap witness later runs consult INSTEAD of re-crawling;
    an unknown/absent marker table (pre-migration envs - the table ships in
    the same 20261027 migration as the columns) surfaces as no markers, which
    just means the dates are attempted - the upserts are idempotent by PK
    either way."""
    table = marker_table(env)
    rows = []
    offset = 0
    while True:
        try:
            page = (
                SUPABASE_ADMIN_CLIENT.table(table)
                .select("date")
                .gte("date", after_date)
                .range(offset, offset + 999)
                .execute()
            )
        except Exception:  # noqa: BLE001 - PGRST205 (table absent) etc. => no markers yet
            if offset == 0:
                print(f"markers: {table} not readable yet (pre-migration?) - treating as no markers", flush=True)
                return set()
            raise
        data = page.data or []
        if not data:
            break
        rows.extend(data)
        offset += len(data)
    return {row["date"] for row in rows}


def existing_player_keys(table, day):
    """The player_id set the snapshot table already owns for one date."""
    ids = []
    offset = 0
    while True:
        page = (
            SUPABASE_ADMIN_CLIENT.table(table).select("player_id").eq("date", day).range(offset, offset + 999).execute()
        )
        data = page.data or []
        if not data:
            break
        ids.extend(int(row["player_id"]) for row in data)
        offset += len(data)
    return set(ids)


def _toi_minutes(toi):
    """``MM:SS`` -> decimal minutes (the parsing model._toi_minutes runs);
    a None/blank toi stays None - an honest unknown the inference core
    drops with a warning rather than inventing a zero."""
    if isinstance(toi, str) and ":" in toi:
        minutes, seconds = toi.split(":")[:2]
        return int(minutes) + int(seconds) / 60
    return None if toi in (None, "") else float(toi)


def _record_rows(game, records):
    """Per-player raw upsert rows for one game's boxscore records.

    Skaters only (position != 'G'), matching local_store.publish's shape
    (goalies live on Goalie-Snapshots-backtrack). Returns
    (update_rows, create_rows): the UPDATE lane carries ONLY the raw block
    (an existing published row is top-upped, never overwritten - a payload
    key absent from an upsert would be an explicit NULL on PostgREST, which
    would wipe a published column); the CREATE lane adds the identity
    columns a minimal snapshot shape needs (the publish later completes the
    row: backfill adds raw numbers, publish owns the rates/aggregates).
    """
    home = (game.get("homeTeam") or {}).get("abbrev") or game.get("home")
    away = (game.get("awayTeam") or {}).get("abbrev") or game.get("away")
    day = game.get("gameDate") or game.get("date")
    update_rows, create_rows = [], []
    for record in records:
        position = record.get("position")
        if position == "G":
            continue
        row_date = record.get("game_date") or day
        if not row_date:
            raise RuntimeError(f"boxscore request returned a player row with no game_date (game {game.get('id')})")
        team = record.get("team_abbrev")
        opponent = away if team == home else (home if team == away else None)
        raw = {
            "date": row_date,
            "player_id": int(record["player_id"]),
            "game_goals": int(record.get("goals") or 0),
            "game_shots": int(record.get("shots") or 0),
            "game_points": int(record.get("points") or 0),
            "game_toi_min": _toi_minutes(record.get("toi")),
        }
        goals = raw["game_goals"]
        if team:
            create_rows.append(
                {
                    **raw,
                    "season": season_label(str(row_date)),
                    "name": record.get("name") or f"player-{record['player_id']}",
                    "team_abbrev": team,
                    "opponent_abbrev": opponent,
                    "scored": 1 if goals > 0 else 0,
                }
            )
        else:
            # No club evidence: still create the row (name is NOT NULL on the
            # table), just without the club columns - the publish completes them.
            create_rows.append(
                {
                    **raw,
                    "season": season_label(str(row_date)),
                    "name": record.get("name") or f"player-{record['player_id']}",
                }
            )
        update_rows.append(raw)
    return update_rows, create_rows


def _upsert_rows(table, rows, batch_size=UPSERT_BATCH):
    """Merge-duplicates upsert, 500-batch, with the repo's retry envelope.

    Three tries per batch with exponential backoff (nhl_client._get_json's
    shape); one failed batch raises (the caller fails the DATE - unmarked,
    retried by a later pass - never the whole run). Every row set must share
    ONE key signature: a mixed payload would turn a missing key into an
    explicit NULL on PostgREST, wiping a published column."""
    if not rows:
        return 0
    shapes = {tuple(sorted(row)) for row in rows}
    if len(shapes) != 1:
        raise RuntimeError(
            f"upsert rows mix key signatures {[sorted(s) for s in shapes]} - "
            "split update/create lanes and upsert each lane separately"
        )
    written = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        error = None
        for attempt in range(UPSERT_RETRIES):
            try:
                client_call = SUPABASE_ADMIN_CLIENT.table(table).upsert(
                    batch, on_conflict="date,player_id", returning="minimal"
                )
                client_call.execute()
                error = None
                break
            except Exception as exc:  # noqa: BLE001 - retried below, then surfaced
                error = exc
                if attempt == UPSERT_RETRIES - 1:
                    break
                time.sleep(2.0 * (attempt + 1))
        if error is not None:
            raise RuntimeError(f"backfill upsert failed ({table}, batch {start // batch_size}): {error!r}")
        written += len(batch)
    return written


def _fetch_boxscore_fresh(game_id, delay_seconds=None, dry=False, started_utc=None):
    """fetch_boxscore with the cached-LIVE hazard explicitly overridden.

    nhl_client.fetch_boxscore serves ``data/cache/boxscore-<id>.json`` whenever
    it exists - and a file written while the game was still LIVE (an earlier
    crawl that raced the final horn) would be served forever as if final. The
    helper therefore DELETES any existing cache file before every fetch so the
    request below is a guaranteed fresh GET of the FINAL box score (which
    re-caches the final data, healing the file for every other consumer).
    nhl_client itself is NOT modified (task constraint); the override lives
    entirely here. A dry run performs no unlink (read-only); instead it FLAGS
    a cache file that predates the game's likely completion (start + 3h) as a
    possibly-partial LIVE read - the real pass overrides it with a fresh GET."""
    path = Path(CACHE_DIR) / f"boxscore-{game_id}.json"
    if dry:
        if path.exists():
            try:
                age_ok = not started_utc or path.stat().st_mtime >= _start_plus_hours(started_utc, 3).timestamp()
            except OSError:
                age_ok = True
            if not age_ok:
                print(
                    f"WARNING (dry): game {game_id}'s cached boxscore file predates the game's completion - "
                    "it may hold partial LIVE data; the real pass deletes it and re-GETs fresh",
                    flush=True,
                )
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            print(f"WARNING: could not remove stale boxscore cache {path.name}; fetching anyway", flush=True)
    kwargs = {} if delay_seconds is None else {"delay_seconds": delay_seconds}
    return fetch_boxscore(game_id, **kwargs)


def _start_plus_hours(start_utc, hours):
    """A UTC ISO start stamp + ``hours``, as an aware datetime (the same
    parsing run_daily_picks._start_plus_hours runs)."""
    stamp = datetime.datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp + datetime.timedelta(hours=hours)


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
    """The chain (read latest -> markers -> schedule per day -> FRESH boxscore
    per FINAL game -> two-lane upserts -> marker row).

    Returns a summary dict: {status, games, rows, created, backfilled,
    skipped, failed_days, pending}. ``dry`` performs ZERO writes (no upserts,
    no marker rows) - it reads the schedule/boxscores and logs what WOULD be
    written; the cache-unlink is skipped too, and a cached boxscore file that
    predates the game's completion is read but flagged (the fresh GET happens
    on the real pass).
    """
    table = player_table(env)
    through = through_date or yesterday_et()
    if not raw_columns_landed(table):
        print(
            f"WARNING: the per-game raw columns are not landed on {table} yet (migration 20261027 - "
            "deploy.yml's migrate job applies it on merge to main) - skipping the gap-fill; the picks "
            "pass likewise fails loud naming the migration until the DDL + the one-shot top-up land. "
            "Zero writes performed.",
            flush=True,
        )
        return {
            "status": "skipped-columns",
            "games": 0,
            "rows": 0,
            "created": 0,
            "backfilled": [],
            "skipped": [],
            "failed_days": [],
            "pending": [],
        }
    latest = latest_published_date(env)
    if latest is None:
        raise RuntimeError(f"{table} is empty - run the publish/backfill chain first; this helper is not the bootstrap")
    summary = {
        "status": "ok",
        "games": 0,
        "rows": 0,
        "created": 0,
        "backfilled": [],
        "skipped": [],
        "failed_days": [],
        "pending": [],
    }
    if latest >= through:
        summary["status"] = "up-to-date"
        print(f"{table}'s latest published date is {latest} (through {through}): nothing to backfill")
        return summary
    missing = days_between(latest, through)
    if not missing:
        print(f"{table}: no completed-game window between {latest} and {through}")
        summary["status"] = "up-to-date"
        return summary
    if len(missing) == MAX_MISSING_DAYS:
        print(
            f"WARNING: a {MAX_MISSING_DAYS}-day gap between {latest} and {through} stays OPEN at its tail - "
            "walls of >14 missing days need the offline local-store top-up (publish_shot_cols.py), "
            "not this bounded helper",
            flush=True,
        )
    done = marked_dates(env, missing[0])
    delay = nhl_client.DEFAULT_DELAY_SECONDS if delay_seconds is None else float(delay_seconds)
    written_total = 0
    for day in missing:
        if day in done:
            summary["skipped"].append(day)
            print(f"{day}: already marked backfilled - skipping (idempotent re-run: 0 writes)")
            continue
        try:
            payload = fetch_schedule(day, delay_seconds=delay)
            week_games = [
                game
                for week in payload.get("gameWeek") or []
                for game in week.get("games") or []
                if week.get("date") == day and game.get("gameType") in (REGULAR_SEASON, PLAYOFFS)
            ]
            day_games = [game for game in week_games if str(game.get("gameState", "")).upper() in TERMINAL_GAME_STATES]
            pending = len(week_games) - len(day_games)
            if pending:
                summary["pending"].append(day)
                print(
                    f"{day}: skipping {pending} game(s) in a non-terminal state (FINAL-only guard; "
                    "the date stays unmarked for a later pass)",
                    flush=True,
                )
            if not day_games:
                continue
            day_written, day_created = 0, 0
            for game in day_games:
                records = _fetch_boxscore_fresh(
                    game["id"], delay_seconds=delay, dry=dry, started_utc=game.get("startTimeUTC")
                )
                update_rows, create_rows = _record_rows(game, records)
                if not update_rows:
                    continue
                summary["games"] += 1
                if dry:
                    print(
                        f"{day}: game {game['id']} dry-read of {len(update_rows)} skater row(s) - no write "
                        "(the real pass splits top-up vs create by the table's existing keys)",
                        flush=True,
                    )
                    day_written += len(update_rows)
                    continue
                existing = existing_player_keys(table, day)
                create_lane = [row for row in create_rows if row["player_id"] not in existing]
                update_lane = [row for row in update_rows if row["player_id"] in existing]
                game_written = _upsert_rows(table, update_lane)
                game_written += _upsert_rows(table, create_lane)
                day_written += game_written
                day_created += len(create_lane)
                if written_total // PROGRESS_EVERY < (written_total + game_written) // PROGRESS_EVERY:
                    print(f"progress: {written_total + game_written} rows upserted so far", flush=True)
                written_total += game_written
            summary["rows"] += day_written
            summary["created"] += day_created
            if not dry:
                SUPABASE_ADMIN_CLIENT.table(marker_table(env)).upsert(
                    [{"date": day, "games": len(day_games), "player_games": day_written}],
                    on_conflict="date",
                    returning="minimal",
                ).execute()
            summary["backfilled"].append(day)
            print(
                f"{day}: {len(day_games)} FINAL game(s), {day_written} player-game row(s) "
                f"({day_created} created absent, {day_written - day_created} topped up)"
                f"{'; marker written' if not dry else '; DRY: no marker written'}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - one failed date must not fail the run
            summary["failed_days"].append(day)
            print(
                f"WARNING: the backfill of {day} failed ({type(exc).__name__}: {str(exc)[:240]}) - "
                "the date stays UNMARKED and is retried by the next pass; continuing",
                flush=True,
            )
    if summary["failed_days"]:
        summary["status"] = "partial"
    print(
        f"backfill {'(dry)' if dry else ''}: {summary['games']} game(s), {summary['rows']} player-game row(s) "
        f"({summary['created']} created), status {summary['status']}"
    )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="prod", choices=["dev", "prod"], help="backtrack table suffix (default prod)")
    parser.add_argument(
        "--through-date",
        default=None,
        help="last date to backfill through, YYYY-MM-DD (default: ET-yesterday)",
    )
    parser.add_argument(
        "--dry", action="store_true", help="read-only: no upserts, no marker rows, log what WOULD be written"
    )
    parser.add_argument("--delay", type=float, help="seconds between NHL API requests (default: nhl_client's)")
    args = parser.parse_args(argv)
    summary = backfill(args.env, through_date=args.through_date, dry=args.dry, delay_seconds=args.delay)
    return 0 if summary["status"] in ("ok", "up-to-date", "skipped-columns") else 1


if __name__ == "__main__":
    sys.exit(main())
