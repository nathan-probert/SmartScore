#!/usr/bin/env python
"""Rebuild + catch up the raw per-game store - now an OWNER-SIDE OFFLINE
UTILITY only (make backtrack-refresh).

WHY (kept, narrowed)
---
Since the stage-2 per-game raw columns landed (20261027 migration), the
picks pass reads Supabase alone: the daily gap-fill is
backfill_missing_days.py (bounded, FINAL-only, no local store) and
run_daily_picks recomputes the rolling/career block exactly from the
published record. This chain is retired from .github/workflows/ml-picks.yml
and survives only for local store maintenance on the owner box:

  1. ``dump_store.load`` rebuilds the store from the committed season dumps
     (data/dumps/<season>.sql.gz, tracked in git - the raw layer's audit
     trail). No credentials, no Supabase, no crawl.
  2. ``local_store.build`` fills the CURRENT season on cache-miss through
     nhl_client (public NHL endpoints + its retry envelope; the only writes
     are local: the raw store and the data/cache HTTP cache). Idempotent:
     step 1 truncates and re-loads; step 2 upserts (COALESCE-merge on
     (player_id, game_id)).

Usage::

    uv run python smartscore/scripts/backtrack/refresh_store.py
    uv run python smartscore/scripts/backtrack/refresh_store.py --through 2026-10-27
    uv run python smartscore/scripts/backtrack/refresh_store.py --skip-load   # crawl only

There is no Supabase here at all: publishing (make backtrack-publish) stays
an owner-side chain that this script neither needs nor touches.
"""

import argparse
import datetime
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dump_store  # noqa: E402
import local_store  # noqa: E402


def season_label(on_date):
    """The NHL season id ('20262027') a slate date belongs to: the season
    that started the prior October runs through June, so Jan-Jun dates label
    to year-1. Mirrors run_daily_picks.season_label (kept here so this
    wrapper imports no Supabase config)."""
    day = datetime.date.fromisoformat(on_date)
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}{start + 1}"


def refresh(
    through=None,
    db_path=local_store.DB_PATH,
    dump_dir=dump_store.DUMP_DIR,
    skip_load=False,
    delay=None,
    fresh_schedule=True,
):
    """The chain: dump load, then the current season's build. Returns the
    (season, player_games rows, newest game date) triple printed for the log."""
    through = through or datetime.date.today().isoformat()
    season = season_label(through)

    if skip_load and not Path(db_path).exists():
        print(f"{Path(db_path).name} missing even though --skip-load was given; loading dumps anyway", flush=True)
        skip_load = False

    total = None
    if not skip_load:
        total = dump_store.load(db_path, dump_dir)
        print(f"dump load: {total} player-games through the last committed season", flush=True)

    # DAILY_DATA_RUNBOOK.md section 2's gotcha: nhl_client.season_games()
    # serves a warm schedule file verbatim - it never re-walks one - so a
    # stale schedule silently freezes the game list at the moment it was
    # written (observed: a bare re-run re-merged known games and added ZERO
    # rows, reporting a normal success). The per-game caches ARE safe to
    # keep (keyed per game/player, missing fields re-fetch), so only the
    # schedule file is dropped: a fresh walk is a few dozen light requests
    # and rediscovers every newly FINAL game.
    season_schedule_cache = local_store.CACHE_DIR / f"schedule-{season}.json"
    if fresh_schedule and season_schedule_cache.exists():
        season_schedule_cache.unlink()
        print(f"dropped the season schedule cache (fresh walk): {season_schedule_cache.name}", flush=True)

    delay_seconds = local_store.DEFAULT_DELAY_SECONDS if delay is None else float(delay)
    written, _log_written = local_store.build(season, db_path=db_path, delay_seconds=delay_seconds)

    conn = local_store.connect(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM player_games WHERE season = ?", (season,)).fetchone()[0]
        newest = conn.execute("SELECT MAX(game_date) FROM player_games").fetchone()[0]
    finally:
        conn.close()

    print(
        f"build {season}: {written} row(s) upserted; season now holds {rows} player-games; "
        f"newest game in the store: {newest}",
        flush=True,
    )
    return season, rows, newest


def main():
    parser = argparse.ArgumentParser(description="Rebuild the raw store from dumps + crawl the current season.")
    parser.add_argument("--through", help="Slate date YYYY-MM-DD (default: today) - picks the season to build.")
    parser.add_argument("--db", default=str(local_store.DB_PATH))
    parser.add_argument("--dumps", default=str(dump_store.DUMP_DIR))
    parser.add_argument("--skip-load", action="store_true", help="Keep the existing store; crawl the season only.")
    parser.add_argument(
        "--delay", type=float, help="Seconds between NHL API requests (default: nhl_client's throttle)."
    )
    parser.add_argument(
        "--warm-schedule",
        action="store_true",
        help="Keep the season schedule cache (DANGER: the walk then serves the cached game list verbatim - "
        "DAILY_DATA_RUNBOOK.md section 2; default is to re-walk, see --fresh-schedule).",
    )
    args = parser.parse_args()

    fresh_schedule = not args.warm_schedule
    refresh(args.through, args.db, args.dumps, args.skip_load, args.delay, fresh_schedule)
    return 0


if __name__ == "__main__":
    sys.exit(main())
