#!/usr/bin/env python
"""Morning sanity check for the daily data pipeline. READ-ONLY.

One-shot verification of the daily day-of data flow, run by a human or a
monitoring cron after the morning slot; it writes nothing anywhere. It answers
three questions in one pass:

1. Is the local crawl current? (latest game date in ``data/raw_nhl.sqlite``
   per core raw/derived table, and per-day row counts for the last few days)
2. Did the projected-lineup collector land today's rows? (``Player-Snapshots-
   {env}``: row count by ``lineup_status``, rows with D-position units, PP1
   count)
3. Is the published training table fresh for the Lambda? (max ``game_date``
   and row count on ``Player-Snapshots-backtrack-{env}``)

Denominator notes live inline: the per-day counts are caveated so a natural
trend of growing rows/day is not read as an error.

Usage::

    .venv/bin/python smartscore/scripts/backtrack/daily_sanity.py --env prod
    .venv/bin/python smartscore/scripts/backtrack/daily_sanity.py --env both
    .venv/bin/python smartscore/scripts/backtrack/daily_sanity.py --env dev --date 2026-10-24

Exit code: 0 with at most stale-warnings printed; 1 only when a check cannot
run at all (missing store, missing credentials). Staleness itself is a printed
warning, not a failure - on an off-day (election-day style no-game slate or an
early morning slot) nothing is stale that will not be stale again tonight.
"""

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[3]
DB_PATH = REPO_DIR / "data" / "raw_nhl.sqlite"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def et_today() -> str:
    """Today's date in America/Toronto (the pipeline's schedule timezone)."""
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    return datetime.datetime.now(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d")


def q_conn(db_path: Path, timeout: float = 60.0) -> sqlite3.Connection:
    """Read-only connection tolerant of a concurrent --build writer/deriver.

    The daily build can hold the write lock for a long derive pass; a reader on
    the regular 5 s default would fail with 'database is locked' exactly on the
    mornings it is scheduled. 60 s covers it; WAL is not forced from here (the
    store's locking mode is the writer's business)."""
    return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=false", uri=True,
                           timeout=timeout)


def sqlite_sanity(season: str, db_path: Path = DB_PATH) -> int:
    """Latest game date + per-day counts from the local store."""
    print(f"== local store: {db_path.name} ==")
    if not db_path.exists():
        print("FAIL: local store missing")
        return 1
    conn = q_conn(db_path)
    q = conn.execute

    latest = q("SELECT max(game_date) FROM player_games WHERE season = ?", (season,)).fetchone()[0]
    n_days = q(
        "SELECT count(DISTINCT game_date) FROM player_games WHERE season = ?", (season,)
    ).fetchone()[0]
    rows = q("SELECT count(*) FROM player_games WHERE season = ?", (season,)).fetchone()[0]
    lineups = q(
        "SELECT count(*) FROM player_lineups l WHERE l.game_id IN"
        " (SELECT DISTINCT game_id FROM player_games WHERE season = ?)",
        (season,),
    ).fetchone()[0]
    shifts_g = q(
        "SELECT count(DISTINCT game_id) FROM shifts WHERE season = ?", (season,)
    ).fetchone()[0]
    tims = q(
        "SELECT count(*), max(game_date) FROM tims_daily WHERE season = ?", (season,)
    ).fetchone()
    derived = q(
        "SELECT count(*) FROM derived_features WHERE season = ?", (season,)
    ).fetchone()[0]
    team = q(
        "SELECT count(*) FROM derived_team_stats WHERE season = ?", (season,)
    ).fetchone()[0]

    print(f"{season} player_games latest game date : {latest} ({n_days} dates, {rows} rows)")
    print(f"{season} player_lineups (reconstructed)  : {lineups} rows")
    print(f"{season} shifts games covered            : {shifts_g}")
    print(f"{season} tims_daily latest                : {tims[1]} ({tims[0]} rows)")
    print(f"{season} derived_features rows            : {derived}")
    print(f"{season} derived_team_stats rows          : {team}")

    print("last three game dates in the store (rows/day):")
    for dt, n in q(
        "SELECT game_date, count(*) FROM player_games WHERE season = ?"
        " GROUP BY game_date ORDER BY game_date DESC LIMIT 3",
        (season,),
    ).fetchall():
        print(f"    {dt}: {n} rows")

    if latest:
        gap = (
            datetime.date.today() - datetime.date.fromisoformat(latest)
        ).days
        if gap >= 2:
            print(f"WARN: latest local game date is {gap} days old - rerun"
                  " run_background.py --cmd build before publishing")
    print()

    conn.close()
    return 0


def supabase_sanity(env: str, date_str: str, do_train: bool = True) -> None:
    """Read-only PostgREST checks against both Supabase tables for one env.

    Uses the service-role key purely for read reach (the same reach the admin
    client grants elsewhere); every request below is a GET.
    """
    import os  # noqa: PLC0415

    from dotenv import load_dotenv  # noqa: PLC0415
    import requests  # noqa: PLC0415

    load_dotenv()
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    def rest(path, params=None, count_exact=False):
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        if count_exact:
            headers["Prefer"] = "count=exact"
        resp = requests.get(f"{url}/rest/v1/{path}", headers=headers, params=params,
                            timeout=30)
        if resp.status_code >= 400:
            print(f"FAIL: GET {path} -> {resp.status_code}")
            return []
        rows = resp.json()
        if count_exact:
            total = resp.headers.get("content-range", "").split("/")[-1]
            return rows, int(total) if total.isdigit() else len(rows)
        return rows

    table_projected = f"Player-Snapshots-{env}"
    print(f"== Supabase --env {env} ==")

    # projected snapshots for the requested date
    rows = rest(table_projected, {
        "select": "date,lineup_status,lineup_position_group,lineup_unit,pp_unit",
        "date": f"eq.{date_str}", "limit": "20000",
    })
    by_status: dict = {}
    d_units = 0
    pp1 = 0
    for r in rows:
        status = r.get("lineup_status") or "(null)"
        by_status[status] = by_status.get(status, 0) + 1
        # D units: a D-pair slot (D1-D3). lineup_unit is the slot label, the
        # position group its projection; projection rows may carry only one.
        group = (r.get("lineup_position_group") or "").upper()
        if group == "D" or (r.get("lineup_unit") or "").upper() in {"D1", "D2", "D3"}:
            d_units += 1
        if (r.get("pp_unit") or "").upper() in {"PP1", "POWER PLAY #1"}:
            pp1 += 1
    print(f"{table_projected} @{date_str} rows: {len(rows)} by_status={by_status}")
    print(f"    rows with D units (D1-D3 or group=D): {d_units}")
    print(f"    rows with PP1: {pp1}")
    if not rows:
        print(f"WARN: zero projected rows on {date_str} - did"
              " snapshot_projected.py run at 10:30 ET?")
    elif "PROJECTED" not in by_status and "PROJECTED_PROXY" not in by_status:
        print("WARN: nothing official/proxy collected yet for the date")

    if not do_train:
        return

    # published training-table freshness (max date via order+limit). The
    # backtrack tables' column is `date` (game/snapshot date), not game_date.
    table_train = f"Player-Snapshots-backtrack-{env}"
    latest_rows = rest(table_train, {
        "select": "date", "order": "date.desc",
        "limit": "1",
    })
    if not latest_rows:
        print(f"{table_train}: no rows (empty table: publish has never run"
              " for this env)")
        return
    latest = latest_rows[0]["date"]
    _, total = rest(table_train, {
        "select": "date", "order": "date.desc", "limit": "1",
        "date": f"eq.{latest}",
    }, count_exact=True)
    print(f"{table_train} latest date: {latest} ({total} rows on that date)")
    if latest and latest != date_str:
        print("note: published table lags the local store - publish is a"
              " separate step; run make backtrack-publish after the build")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env", choices=("dev", "prod", "both"), default="both")
    parser.add_argument("--date", default=None,
                        help="projected-snapshot date to check (default: today ET)."
                        " Backtrack training tables are checked at their max date,"
                        " not this one - they can legitimately lag.")
    parser.add_argument("--season", default="20262027")
    args = parser.parse_args()

    date_str = args.date or et_today()
    rc = 0

    print("note: this script only SELECTs; it never writes Supabase")
    rc1 = sqlite_sanity(args.season)
    rc = rc1 if rc1 else rc

    for env in (["dev", "prod"] if args.env == "both" else [args.env]):
        supabase_sanity(env, date_str)

    return rc


if __name__ == "__main__":
    sys.exit(main())
