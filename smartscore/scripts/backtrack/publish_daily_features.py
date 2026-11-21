#!/usr/bin/env python
"""Nightly feature publisher: the bridge between the full ledger (Storage)
and the small ML-Daily-Features-{ENV} table (Supabase) the daily picks run
reads.

WHY THIS EXISTS
---------------
The daily run's only consumers from the 560 MB backtrack family are the 20
history-derived feature columns (the 14 shot/TOI features, prev2/prev3
goals, games_since_goal, days_since_game, club/opp rest days) - everything
else it reads comes from the live snapshot or is computed at predict time.
Those 20 columns cannot be computed on the Actions runner against PostgREST
(career-long features need the whole ledger) - but they CAN be computed
there against the LEDGER FILE, which is only ~130 MB materialized. So the
nightly job runs THIS script:

    1. pull  - download the per-season ledger files, materialize one
               working SQLite database (ml_ledger.pull);
    2. backfill - yesterday's completed games' per-game raw stats go into
               the working ledger (backfill_missing_days.backfill, the
               sqlite backend - the SAME helper the old run used, pointed
               at the local file instead of PostgREST);
    3. compute - for tomorrow's slate (the NHL schedule), the EXACT frame
               pipeline the daily run runs - build_frame + add_derived over
               the full history plus ONE synthetic pre-game row per player -
               sliced to the 20 history-derived columns;
    4. publish - upsert those rows to ML-Daily-Features-prod (one row per
               likely slate player, ~600-800 rows; a season is megabytes);
    5. push   - re-split the working ledger into per-season files and
               upload the ones whose bytes changed (gzip is deterministic:
               historical seasons re-compress identical and are skipped).

The daily run then reads live snapshot + ML-Daily-Features and touches NO
big table. If this script did not run (or failed), the run warns and falls
back per player to that player's last published feature row (the same
warn-and-continue doctrine as a missing backfill day).

The math is NOT forked: step 3 runs model.build_frame / add_derived /
shot_features_from_games verbatim over ledger data - the publisher only
builds the synthetic pre-game rows from the schedule, which is the one
input the run's morning path gets from the live snapshot instead.

Usage (the nightly job)::

    uv run python smartscore/scripts/backtrack/publish_daily_features.py --env prod
    uv run python smartscore/scripts/backtrack/publish_daily_features.py --dry   # read-only: no writes at all
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_here = Path(__file__).resolve()
_root = _here.parents[3]
sys.path.insert(0, str(_root / "smartscore"))
sys.path.insert(1, str(_here.parent))

import backfill_missing_days  # noqa: E402  (same directory: the gap-fill, run against the materialized ledger)
import ml_history_store  # noqa: E402
import ml_ledger  # noqa: E402
import model  # noqa: E402
import nhl_client  # noqa: E402

from config import SUPABASE_ADMIN_CLIENT  # noqa: E402

# Hard-pinned per the shared-ledger doctrine (docs/backtrack_dev_deleted.md):
# ONE feature table, whatever --env the caller passes - prod is the only
# ledger the daily run reads. Kept overridable for tests.
FEATURES_TABLE = os.environ.get("ML_FEATURES_TABLE", "ML-Daily-Features-prod")

# The 20 history-derived columns this publisher owns (the migration's
# feature block). club/opp/name ride along for the run's club resolution
# and human-readable diffs; the full column list is assembled in
# compute_features (SHOT_FEATURES is model's constant - imported, never
# copied).
STREAK_COLUMNS = [
    "prev2_goals",
    "prev3_goals",
    "games_since_goal",
    "days_since_game",
    "club_rest_days",
    "opp_rest_days",
]

# The synthetic pre-game row's identity columns (everything else NaN): the
# same shape _map_live_rows produces from the morning snapshot, sourced from
# the NHL schedule instead.
SYNTHETIC_COLUMNS = [
    "date",
    "player_id",
    "name",
    "team_name",
    "home",
    "team_abbrev",
    "opponent_abbrev",
    "tims",
    "scored",
    "lineup_status",
]


def next_slate_date(now=None):
    """The date the NEXT daily run will predict: tomorrow in ET (the
    nightly job fires the evening before the slate; the morning kicks read
    features for their own date = this one)."""
    now = now or datetime.datetime.now(tz=ZoneInfo("America/Toronto"))
    return (now.date() + datetime.timedelta(days=1)).isoformat()


def slate_clubs(on_date):
    """{club_abbrev: (opponent_abbrev, home)} from the NHL schedule - one
    entry per club with a game on ``on_date`` (reg regular + playoffs, the
    backfill's same filters)."""
    payload = nhl_client.fetch_schedule(on_date)
    clubs = {}
    for week in payload.get("gameWeek") or []:
        if week.get("date") != on_date:
            continue
        for game in week.get("games") or []:
            if game.get("gameType") not in (nhl_client.REGULAR_SEASON, nhl_client.PLAYOFFS):
                continue
            home = (game.get("homeTeam") or {}).get("abbrev")
            away = (game.get("awayTeam") or {}).get("abbrev")
            if not home or not away:
                continue
            clubs[home] = (away, True)
            clubs[away] = (home, False)
    return clubs


def candidate_players(clubs):
    """EVERY ledger player - the site's slate is the whole league pool
    (the first live run: 2,633 slate rows vs 739 club-filtered candidates,
    506 real players left without features).

    A player whose club plays tomorrow gets club/opp/home from the
    schedule; everyone else gets club=None (their row is harmless - the
    run reads only the slate's player_ids - and a scratched/traded player
    whose club IS scheduled keeps their ledger club). The latest
    team_abbrev-bearing row wins: backfill-created rows carry the
    boxscore's club, so a mid-season trade resolves to the current club."""
    table = ml_history_store.TABLE_MAP["Player-Snapshots-backtrack-prod"]
    query = f'SELECT "player_id", "name", "date", "team_abbrev" FROM "{table}" ORDER BY "date"'
    with ml_history_store.connect() as con:
        frame = pd.read_sql_query(query, con)
    frame["player_id"] = pd.to_numeric(frame["player_id"])
    named = frame[frame["team_abbrev"].notna()]
    latest = named.sort_values("date").groupby("player_id").tail(1)
    latest["club"] = latest["team_abbrev"]
    print(
        f"candidate players: {len(latest)} (ledger pool), {int(latest['club'].isin(clubs).sum())} with a game tomorrow",
        flush=True,
    )
    return latest[["player_id", "name", "club"]]


def synthetic_slate_rows(candidates, clubs, on_date):
    """One pre-game row per candidate player: identity from the schedule
    (club/opp/home), every stat column NaN - the shot block attaches the
    computed 14, and add_derived fills the streak/rest block from the
    player's prior rows."""
    opp = {club: pair[0] for club, pair in clubs.items()}
    home = {club: pair[1] for club, pair in clubs.items()}
    rows = []
    for row in candidates.itertuples(index=False):
        rows.append(
            {
                "date": on_date,
                "player_id": int(row.player_id),
                "name": row.name,
                "team_name": None,
                "home": home.get(row.club),
                "team_abbrev": row.club,
                "opponent_abbrev": opp.get(row.club),
                "tims": None,
                "scored": None,
                "lineup_status": None,
            }
        )
    return pd.DataFrame(rows, columns=SYNTHETIC_COLUMNS)


def synthetic_team_rows(clubs, on_date):
    """One Team-Stats row per scheduled club for the slate date: the
    (date, club) identity build_frame's season join and add_derived's
    rest-day schedule need. Rates stay None - they are live-snapshot
    features, never published by this table."""
    season = ml_ledger.season_of(on_date)
    return pd.DataFrame(
        [
            {
                "date": on_date,
                "team_abbrev": club,
                "team_name": None,
                "season": season,
                "game_id": 0,
                "tgpg": None,
                "otga": None,
                "otshga": None,
            }
            for club in sorted(clubs)
        ]
    )


def compute_features(on_date, env, dry=False):
    """Steps 2-3: backfill + the exact frame pipeline over the working
    ledger. Returns the published-shape frame (or, when the slate is
    empty, None)."""
    yesterday = (datetime.date.fromisoformat(on_date) - datetime.timedelta(days=1)).isoformat()
    if dry:
        latest = backfill_missing_days.latest_published_date(env)
        print(f"dry: ledger's latest published date is {latest} (no backfill writes in --dry)", flush=True)
    else:
        summary = backfill_missing_days.backfill(env, through_date=yesterday)
        if summary.get("failed_days"):
            print(
                f"WARNING: the backfill could not complete {summary['failed_days']} game day(s) - the "
                "features below are computed over the history that exists (the run's own doctrine)",
                flush=True,
            )

    clubs = slate_clubs(on_date)
    if not clubs:
        print(f"no games scheduled for {on_date} - nothing to publish", flush=True)
        return None
    candidates = candidate_players(clubs)
    if candidates.empty:
        print(f"no ledger players for {sorted(clubs)} - nothing to publish", flush=True)
        return None
    print(f"slate {on_date}: {len(clubs)} club(s), {len(candidates)} candidate player(s)", flush=True)

    player_ids = [int(pid) for pid in candidates["player_id"]]
    history = ledger_history(on_date)
    teams = ml_history_store.fetch_window(
        LEDGER_TEAM_TABLE, TEAM_FETCH_COLUMNS, "0000-01-01", on_date, ["date", "team_abbrev"]
    )
    goalies = ml_history_store.fetch_window(
        LEDGER_GOALIE_TABLE, GOALIE_FETCH_COLUMNS, "0000-01-01", on_date, ["date", "player_id"]
    )
    teams = pd.concat([teams, synthetic_team_rows(clubs, on_date)], ignore_index=True)

    shot_block = shot_block_from_history(on_date, player_ids, history)
    today = synthetic_slate_rows(candidates, clubs, on_date)
    today = today.merge(shot_block, on=["date", "player_id"], how="left", validate="one_to_one")
    # ppg lives ONLY on the ledger rows (the live snapshot carries gpg /
    # hgpg / five_gpg / hppg but not ppg) - carry each player's latest
    # published single-season PP rate onto the synthetic row: the SAME
    # value the full-ledger path merged from the history rows.
    latest_ppg = history.sort_values("date").groupby("player_id").tail(1)[["player_id", "ppg"]]
    today = today.merge(latest_ppg, on="player_id", how="left", validate="one_to_one")

    frame = model.build_frame(pd.concat([history, today], ignore_index=True), teams, goalies, source="supabase")
    derived = model.add_derived(frame, teams, source="supabase")
    rows = derived[derived["date"] == on_date].reset_index(drop=True)
    # build_frame renamed team_abbrev -> club / opponent_abbrev -> opp.
    columns = [
        "date",
        "player_id",
        "name",
        "club",
        "opp",
        "ppg",
        *model.SHOT_FEATURES,
        *STREAK_COLUMNS,
    ]
    missing = [col for col in columns if col not in rows.columns]
    if missing:
        raise RuntimeError(f"feature frame lacks expected column(s) {missing}")
    return rows[columns]


def publish_features(published, on_date):
    """Upsert the computed rows to ML-Daily-Features-{ENV} (small table -
    one PostgREST upsert, service-role client)."""
    records = json.loads(published.to_json(orient="records"))
    if not records:
        print(f"no feature rows for {on_date} - nothing written", flush=True)
        return 0
    SUPABASE_ADMIN_CLIENT.table(FEATURES_TABLE).upsert(
        records, on_conflict="date,player_id", returning="minimal"
    ).execute()
    print(f"published {len(records)} feature row(s) for {on_date} -> {FEATURES_TABLE}", flush=True)
    return len(records)


def run(event=None):
    """The nightly job's entrypoint. ``event`` carries optional date/dry."""
    event = event or {}
    env = os.environ.get("ENV", "prod")
    on_date = str(event.get("date") or next_slate_date())
    dry = bool(event.get("dry"))
    # THIS process reads/writes the materialized ledger - force the sqlite
    # backend explicitly (the workflow env may not carry it, and the
    # supabase default would route the backfill at the dropped tables).
    os.environ["ML_HISTORY_BACKEND"] = "sqlite"
    print(
        f"publish_daily_features: date={on_date} env={env} table={FEATURES_TABLE} "
        f"ledger={'read-only' if dry else 'pull+backfill+push'}",
        flush=True,
    )

    if not dry:
        working = ml_ledger.pull()
        os.environ["ML_HISTORY_DB"] = str(working)
        print(f"ledger materialized -> {working}", flush=True)
    else:
        # dry stays read-only against REMOTE systems; materializing the
        # ledger locally is how it reads, same as the real pass.
        working = ml_ledger.pull()
        os.environ["ML_HISTORY_DB"] = str(working)
        print(f"dry: ledger materialized (local only) -> {working}", flush=True)

    published = compute_features(on_date, env, dry=dry)
    if not dry and published is not None:
        publish_features(published, on_date)
    if not dry:
        pushed = ml_ledger.push()
        changed = [season for season, (status, _) in pushed.items() if status == "uploaded"]
        print(f"ledger push: {len(changed)} season file(s) changed: {changed}", flush=True)
    return {"statusCode": 200, "date": on_date, "dry": dry, "published": 0 if published is None else len(published)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None, help="slate date YYYY-MM-DD (default: tomorrow in ET)")
    parser.add_argument("--env", default=os.environ.get("ENV", "prod"), choices=["dev", "prod"])
    parser.add_argument("--dry", action="store_true", help="read-only: no ledger writes, no feature publish")
    args = parser.parse_args(argv)
    event = {}
    if args.date:
        event["date"] = args.date
    if args.dry:
        event["dry"] = True
    os.environ["ENV"] = args.env
    result = run(event)
    return 0 if result["statusCode"] == 200 else 1


if __name__ == "__main__":
    sys.exit(main())


# ----------------------------------------------------- ledger reads (local)


# The ledger tables (ml_history_store's TABLE_MAP keys) and the columns the
# publisher's own reads consume - moved here when the run's fallback path
# retired (the publisher is the only full-ledger reader left).
LEDGER_PLAYER_TABLE = "Player-Snapshots-backtrack-prod"
LEDGER_TEAM_TABLE = "Team-Stats-backtrack-prod"
LEDGER_GOALIE_TABLE = "Goalie-Snapshots-backtrack-prod"
HISTORY_FETCH_COLUMNS = [
    "date",
    "player_id",
    "name",
    "team_name",
    "team_abbrev",
    "opponent_abbrev",
    "home",
    "tims",
    "scored",
    "gpg",
    "hgpg",
    "five_gpg",
    "hppg",
    "ppg",
    "injury_status",
    "injury_desc",
    "lineup_unit",
    "lineup_position_group",
    "lineup_status",
    "pp_unit",
]
RAW_COLUMNS = ["game_goals", "game_shots", "game_points", "game_toi_min"]
TEAM_FETCH_COLUMNS = ["date", "team_abbrev", "team_name", "tgpg", "otga", "otshga", "season", "game_id"]
GOALIE_FETCH_COLUMNS = [
    "date",
    "player_id",
    "name",
    "team_abbrev",
    "team_name",
    "gaa",
    "save_pct",
    "record",
    "shutouts",
    "games_played",
]


def ledger_history(on_date):
    """The FULL prior history from the materialized working ledger
    (strictly before the pick date - the prior-games context), read the
    model.fetch_table way: one ordered SELECT, no month-window paging (a
    local index has no PostgREST statement timeout to dodge).

    Full history ON PURPOSE: career_games / career_goals_pg /
    career_shots_pg are expanding career sums and ewm_* is career-long, so
    a windowed fetch would mis-state every veteran's block - and with the
    stage-2 raw columns landed, these published rows ARE the per-game
    series the exact recompute runs on. tgpg/otga/otshga are NOT here (the
    player table carries null placeholders; build_frame re-merges them from
    Team-Stats)."""
    columns = [*HISTORY_FETCH_COLUMNS, *RAW_COLUMNS]
    frame = ml_history_store.fetch_window(LEDGER_PLAYER_TABLE, columns, "0000-01-01", on_date, ["date", "player_id"])
    missing = [col for col in columns if col not in frame.columns]
    if missing:
        raise RuntimeError(f"ledger fetch lacks expected column(s) {missing}")
    return frame


def shot_block_from_history(on_date, player_ids, history):
    """The 14 shot/TOI features for ``on_date``, computed EXACTLY from the
    materialized Storage ledger - the shared pandas core
    (model.shot_features_from_games - imported, never forked) over the
    published per-game record (the stage-2 raw columns game_goals /
    game_shots / game_toi_min), plus ONE synthetic pre-game row per player
    (its own zeros can never enter its features - every definition is
    shifted one game).

    Rows with NO count facts (game_shots or game_goals NULL) are DROPPED
    from the series and the math runs over the available raw rows only
    (partial-prefix recompute, LOUD-warned); a NULL game_toi_min with KNOWN
    counts is KEPT (the core's own NaN semantics)."""
    games = history[["date", "player_id", "game_goals", "game_shots", "game_toi_min"]].copy()
    incomplete_mask = history["game_goals"].isna() | history["game_shots"].isna()
    incomplete = int(incomplete_mask.sum())
    if incomplete:
        null_days = sorted(set(history.loc[incomplete_mask, "date"].astype(str)))
        print(
            f"WARNING: shot-block dropped {incomplete} published row(s) that carry no per-game raw stat "
            f"(pre-top-up lateness; game day(s) {null_days[:5]}"
            f"{'...' if len(null_days) > 5 else ''}) - the rolling/ewm/career features are computed over "
            "the available raw rows only (partial-prefix recompute)",
            flush=True,
        )
        games = games[~incomplete_mask.values]
    games = games.rename(columns={"game_goals": "goals", "game_shots": "shots", "game_toi_min": "toi"})
    games["season"] = games["date"].map(ml_ledger.season_of)
    games["player_id"] = pd.to_numeric(games["player_id"])
    have = set(games.loc[games["date"] == on_date, "player_id"])
    synthetic_ids = [pid for pid in dict.fromkeys(player_ids) if pid not in have]
    synthetic = pd.DataFrame(
        {
            "player_id": synthetic_ids,
            "date": on_date,
            "season": ml_ledger.season_of(on_date),
            "goals": 0,
            "shots": 0,
            "toi": 0,
        }
    )
    block = model.shot_features_from_games(pd.concat([games, synthetic], ignore_index=True))
    block = block[block["date"] == on_date].reset_index(drop=True)
    missing = [col for col in model.SHOT_FEATURES if col not in block.columns]
    if missing:
        raise RuntimeError(f"shot-block core produced no column(s) {missing} for {on_date}")
    return block
