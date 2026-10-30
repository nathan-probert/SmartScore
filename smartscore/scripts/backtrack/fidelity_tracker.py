#!/usr/bin/env python3
"""Track daily-build projected-lineup fidelity against backtracking actuals.

WHY THIS EXISTS
---------------
`V16- collect` (snapshot_projected.py) now accumulates projected lineups WITH
units into `Player-Snapshots-prod` / `Player-Snapshots-backtrack-dev`, and the
model notes' adoption gate is explicit: adopt a lineup-aware model only when
the feed reaches >= 90% unit fidelity AND paired McNemar tests beat the
no-lineup ensemble on both seeds (MODEL_NOTES.md, FINAL RESULT + V16- collect;
V11 sensitivity: 81% fidelity ~ +0.1-0.3pp, 100% ~ +1.2pp). Nothing yet
*measures* the first half of that gate. This script is the nightly
measurement: it joins one game date's daily-build projected rows against the
shift-derived ACTUAL lineups in the local store (`player_lineups` in
data/raw_nhl.sqlite, built by local_store.compute_lineups via
lineup_reconstruct) and logs the match rates.

WHAT IT MEASURES, per (date, player_id) pair that exists on BOTH sides:

* unit_match      - projected `lineup_unit` equals actual `lineup_unit`
                    exactly (F1-F4 / D1-D3). Both sides carry the same
                    vocabulary: the collector writes the article's F/D labels
                    verbatim, and actuals are labelled by
                    lineup_reconstruct._rank_labels with the same prefixes.
* pp_match        - projected `pp_unit` equals actual `pp_unit` exactly after
                    PP-slot normalisation (verbatim "POWER PLAY #1" ->
                    PP1/PP2; PP3/PP4 kept, never folded into PP1/PP2).
* presence_match  - the projected row says the player is in a D/F slot that
                    also exists in the actual deployment: projected
                    `lineup_position_group` matches the actual group.

Rate denominators are the compared-pair counts, logged alongside the rates so
later aggregation can reweight correctly (never average rates of different
denominators).

SOURCE SLOTS
------------
The collector runs at 10:30 / 13:00 / 16:00 ET, but the daily-build tables key
on `(date, player_id)` with no snapshot timestamp column - the PK-upsert
semantics mean the stored row is the accumulated view of all three runs, not
three distinct rows. The tracker therefore detects a slot column generically
(`source_slot` / `snapshot_slot` / `slot`, or a `*_at`/`snapshot_time`
timestamp to bucket in ET); today that detection finds nothing and every row
falls into the single slot "single". Per-slot metric rows are written anyway,
so adding a real slot column to the table immediately becomes measurable here
with no code change. Until then, run-level fidelity is the "all" slot.

TEAM JOINING - the gotcha, spelled out
--------------------------------------
The daily build stores `team_name` as the schedule PLACE name ("New York",
"Minnesota"), and "New York" is NYR/NYI-ambiguous, so a place-name join is
never used and never trusted alone. Identity comes from the date's scheduled
split instead: a place name plus the stored `home`/away flag resolves to
exactly one scheduled club on almost every date, checked against the
actuals' `team_abbrev`. A (place, home) pair that still maps to two scheduled
clubs - possible only when two same-place clubs (NYR/NYI) are BOTH home or
BOTH away - is counted AMBIGUOUS and excluded from matching, never guessed.
Players are matched primarily on `(date, player_id)`, which is the real join;
team resolution only disambiguates a player who appears in more than one
actual game that date (a same-day trade), which is otherwise impossible to
place. Time-of-snapshot acceptance: resolution uses only fields the daily
build stored, and the gotcha above (never join on `placeName`) is enforced by
this function being the ONLY team path in the module.

READ-ONLY
---------
Tracker operations are reads only: SELECT against Supabase (never an INSERT,
PATCH or DELETE on any env - reaching prod with the service-role key is a
read-reach detail, not a write grant), and read-only sqlite (`mode=ro`). It
writes exactly one artifact: the append-only log data/ml/fidelity_log.csv
(untracked cache, like the rest of data/ml).

SKIP PATHS (exit 0, printed reason, no log rows)
------------------------------------------------
* The actuals for the date are missing - the current season's shift charts
  have not been crawled yet (the shift crawler backfills concurrently).
* No projected rows with a tracked status (`PROJECTED`, `PROJECTED_PROXY`)
  exist for the date.

Usage::

    uv run python smartscore/scripts/backtrack/fidelity_tracker.py --env prod
    uv run python smartscore/scripts/backtrack/fidelity_tracker.py --env prod --date 2026-10-22
    uv run python smartscore/scripts/backtrack/fidelity_tracker.py --env both
    uv run python smartscore/scripts/backtrack/fidelity_tracker.py --env dev --log-path /tmp/fid.csv
"""

import argparse
import csv
import datetime
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from team_map import to_place  # noqa: E402

ET = ZoneInfo("America/Toronto")

REPO_ROOT = Path(__file__).resolve().parents[3]
RAW_DB = REPO_ROOT / "data" / "raw_nhl.sqlite"
LOG_PATH = REPO_ROOT / "data" / "ml" / "fidelity_log.csv"

DAILY_BUILD_TABLES = {
    "dev": "Player-Snapshots-backtrack-dev",
    "prod": "Player-Snapshots-prod",
}
# The only statuses whose lineup fields are a real projection signal. The
# live pipeline's UNKNOWN rows (null units) and game truth (CONFIRMED/ACTUAL)
# are excluded: UNKNOWN measures nothing and CONFIRMED/ACTUAL already
# answer the game.
TRACKED_STATUSES = ("PROJECTED", "PROJECTED_PROXY")

LOG_COLUMNS = ("ts", "env", "season", "scope", "date", "source_slot", "metric", "value", "matches", "n")

# Column names the slot detector looks for, in preference order: a label
# column wins over a timestamp column.
_SLOT_LABEL_COLUMNS = ("source_slot", "snapshot_slot", "slot")
_SLOT_TIME_COLUMNS = ("snapshot_time", "snapshot_at", "updated_at", "created_at")

_METRICS = ("unit_match", "pp_match", "presence_match")


def et_today():
    return datetime.datetime.now(ET).strftime("%Y-%m-%d")


def normalize_unit(value):
    """Projected/actual lineup_unit onto one exact-match vocabulary.

    Returns the stripped upper-case label, or None for blank/NaN/None.
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip().upper()
    return text or None


def normalize_pp(value):
    """PP-slot label onto the PP1/PP2 stereotype, keeping PP3/PP4 as themselves.

    "POWER PLAY #1" is the live pipeline's verbatim form (still stored on
    historical rows); the collector already normalizes fresh writes. Both
    vocabularies exist side-by-side in the tables, so every reader here owns
    the translation - the stored value is never rewritten.
    """
    text = normalize_unit(value)
    if not text:
        return None
    if text.startswith("POWER PLAY"):
        number = text.rsplit("#", 1)[-1].strip()
        return f"PP{number}" if number.isdigit() else None
    return text


def season_for_date(date_str):
    """Season id for a game date - the league breaks in July, not January.

    Same rule as local_store._season_from_date, so both sides of the join
    label one date identically, and the TEXT-vs-int64 dtype gotcha is
    handled by everything in this module carrying the season as str().
    """
    year = int(date_str[:4])
    month = int(date_str[5:7])
    season = f"{year}{year + 1:04d}" if month >= 7 else f"{year - 1:04d}{year:04d}"
    return str(season)


def load_daily_build(env, date_str, supabase=None):
    """One date's PROJECTED/PROJECTED_PROXY rows from the daily-build table.

    Read-only. `supabase` is injectable for tests; production code builds the
    lazy client `reconstruct.config()` uses (same .env credentials, service
    role for read reach into prod).
    """
    table = DAILY_BUILD_TABLES[env]
    if supabase is None:
        from reconstruct import config  # noqa: PLC0415 - lazy, like reconstruct

        supabase = config()[1]

    result = (
        supabase.table(table)
        .select("date,player_id,team_name,home,lineup_unit,lineup_position_group,pp_unit,lineup_status")
        .eq("date", date_str)
        .execute()
    )
    rows = [row for row in (result.data or []) if row.get("lineup_status") in TRACKED_STATUSES]
    rows.sort(key=lambda row: int(row["player_id"]))
    return table, rows


def actuals_for_date(date_str, db_path=RAW_DB):
    """ACTUAL deployments for a game date from the local store.

    player_lineups games are discovered through player_games' game_date (join
    on game_id AND player_id, so a traded player's two stints stay apart), and
    team_abbrev/home ride along for the scheduled-side team resolution. The
    return is ``None`` only when the store itself is absent - the module's
    no-store marker. Rows whose lineup_status is not ACTUAL are deployment
    truth missing a unit (healthy scratch, or a skater under every floor) and
    join for presence but contribute no unit/PP comparison.
    """
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            {
                "game_id": int(r["game_id"]),
                "player_id": int(r["player_id"]),
                "team_abbrev": r["team_abbrev"],
                "home": int(bool(r["home"])),
                "game_date": r["game_date"],
                "lineup_unit": normalize_unit(r["lineup_unit"]),
                "pp_unit": normalize_pp(r["pp_unit"]),
                "lineup_position_group": r["lineup_position_group"],
                "lineup_status": r["lineup_status"],
            }
            for r in conn.execute(
                "SELECT lg.game_id, lg.player_id, lg.lineup_unit, lg.pp_unit,"
                " lg.lineup_position_group, lg.lineup_status,"
                " pg.team_abbrev, pg.home, pg.game_date"
                " FROM player_lineups lg"
                " JOIN player_games pg ON pg.game_id = lg.game_id AND pg.player_id = lg.player_id"
                " WHERE pg.game_date = ?",
                (date_str,),
            )
        ]
    finally:
        conn.close()
    return rows


def detect_slot_column(projected_rows):
    """Which daily-build column (if any) identifies a collection slot."""
    if not projected_rows:
        return None
    columns = projected_rows[0].keys()
    for name in _SLOT_LABEL_COLUMNS:
        if name in columns:
            return name
    for name in _SLOT_TIME_COLUMNS:
        if name in columns:
            return name
    return None


def slot_for_row(row, detected_column):
    """The source-slot label a daily-build row falls into.

    A label column comes through verbatim; a timestamp column is read in ET
    and rendered "HH:MM". No detectable column -> "single", the meaning of
    the PK-collapsed daily build (see module docstring).
    """
    if not detected_column:
        return "single"
    value = row.get(detected_column)
    if not value:
        return "single"
    if detected_column in _SLOT_LABEL_COLUMNS:
        return str(value).strip() or "single"
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "single"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.astimezone(ET).strftime("%H:%M")


def resolve_team_abbrev(projected_row, actuals_by_game):
    """Actual team abbreviation for a projected row, from scheduled identity.

    The join is (place name, home flag) -> the one actual game of the date
    that puts the club on the matching side - a scheduled-side identity test,
    never a raw place-name string join against `placeName`.

    Returns ``(abbrev, ambiguous)``: ambiguous means the (place, side) pair
    matched two clubs - only possible when two same-place clubs (NYR/NYI) are
    BOTH home or BOTH away - and the caller must exclude the row rather than
    guess. ``None, False`` means the place matched no club at all, which leaves
    the pair joinable on player id alone.
    """
    place = (projected_row.get("team_name") or "").strip()
    if not place:
        return None, False
    home = projected_row.get("home")
    wanted = None if home is None else int(bool(home))

    matches = {
        game["team_abbrev"]
        for game in actuals_by_game.values()
        if game["team_abbrev"]
        and to_place(game["team_abbrev"]) == place
        and (wanted is None or int(bool(game["home"])) == wanted)
    }
    if len(matches) == 1:
        return next(iter(matches)), False
    if len(matches) > 1:
        return None, True
    return None, False


def pair_with_actuals(projected_rows, actual_rows, date_str):
    """Join daily-build rows to ACTUAL deployments, one pair per compared player.

    The primary join is (date, player_id). Team resolution - the scheduled
    split in resolve_team_abbrev - disambiguates a player with multiple actual
    game rows that date (same-day trade); ambiguity between such sides is
    counted, never guessed.

    Returns ``(pairs, breadcrumbs)``: each pair carries both sides' normalized
    values and the game ids they came from; breadcrumbs carries exclusion
    counts prefixed ``n_``.
    """
    actuals_by_game = {}
    for row in actual_rows:
        game = actuals_by_game.setdefault(row["game_id"], {"team_abbrev": None, "home": 0})
        if game["team_abbrev"] is None and row["game_date"] == date_str:
            game["team_abbrev"] = row["team_abbrev"]
            game["home"] = row["home"]

    actuals_by_pid = defaultdict(list)
    for row in actual_rows:
        actuals_by_pid[row["player_id"]].append(row)

    pairs, breadcrumbs = [], defaultdict(int)

    for projected in projected_rows:
        try:
            player_id = int(projected["player_id"])
        except (KeyError, TypeError, ValueError):
            breadcrumbs["n_unresolved_ids"] += 1
            continue

        candidates = actuals_by_pid.get(player_id, [])
        if not candidates:
            breadcrumbs["n_unknown_players"] += 1
            continue

        abbrev, ambiguous = resolve_team_abbrev(projected, actuals_by_game)
        if ambiguous:
            breadcrumbs["n_team_ambiguous"] += 1
            continue

        chosen = candidates
        if abbrev is not None and len(candidates) > 1:
            scoped = [row for row in candidates if row["team_abbrev"] == abbrev]
            if scoped:
                chosen = scoped

        # Prefer a deployment-labelled actual row when the player has several;
        # the rule is deterministic on (has-unit, game_id) so runs are
        # reproducible.
        actual = min(chosen, key=lambda row: (row["lineup_status"] != "ACTUAL", row["game_id"]))
        pairs.append(
            {
                "date": date_str,
                "player_id": player_id,
                "projected_unit": normalize_unit(projected.get("lineup_unit")),
                "projected_pp": normalize_pp(projected.get("pp_unit")),
                "projected_group": (projected.get("lineup_position_group") or "").strip().upper() or None,
                "projected_status": projected.get("lineup_status"),
                "actual_unit": actual["lineup_unit"] if actual["lineup_status"] == "ACTUAL" else None,
                "actual_pp": actual["pp_unit"] if actual["lineup_status"] == "ACTUAL" else None,
                "actual_group": (actual["lineup_position_group"] or "").strip().upper() or None,
                "actual_game_id": actual["game_id"],
            }
        )

    return pairs, dict(breadcrumbs)


def fidelity_metrics(pairs):
    """Fidelity rates over compared pairs; empty input yields None rates.

    A unit/PP match counts only when BOTH sides carry a value: a projected row
    with no PP assignment interviewing an actual PP1 skater is a missing
    projection, not evidence either way, and stays out of the denominator.
    The denominators are echoed in the returned entries so aggregation can
    reweight by exposure rather than average rates.
    """
    counts, denominators = {}, {}
    for metric in _METRICS:
        counts[metric], denominators[metric] = 0, 0

    for pair in pairs:
        if pair["projected_unit"] is not None and pair["actual_unit"] is not None:
            denominators["unit_match"] += 1
            counts["unit_match"] += pair["projected_unit"] == pair["actual_unit"]
        if pair["projected_pp"] is not None and pair["actual_pp"] is not None:
            denominators["pp_match"] += 1
            counts["pp_match"] += pair["projected_pp"] == pair["actual_pp"]
        if pair["projected_group"] in ("F", "D") and pair["actual_group"] in ("F", "D"):
            denominators["presence_match"] += 1
            counts["presence_match"] += pair["projected_group"] == pair["actual_group"]

    return {
        metric: {
            "value": (counts[metric] / denominators[metric]) if denominators[metric] else None,
            "matches": counts[metric],
            "n": denominators[metric],
        }
        for metric in _METRICS
    }


def metric_log_rows(metrix_package):
    """One log row per metric, value + matches + exposure."""
    return [
        {"metric": metric, "value": entry["value"], "matches": entry["matches"], "n": entry["n"]}
        for metric, entry in sorted(metrix_package.items())
    ]


def append_log(env, season, scope, date_str, source_slot, rows, log_path=LOG_PATH):
    """Append metric rows to the fidelity log; the header is written once.

    The log is an append-only cache (untracked, like the rest of data/ml):
    every run adds its rows, and downsream readers take the newest ts. No
    Supabase or store write is ever issued by this module.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        with open(log_path, "w", newline="") as handle:
            csv.writer(handle).writerow(LOG_COLUMNS)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log_path, "a", newline="") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(
                [
                    stamp,
                    env,
                    season,
                    scope,
                    date_str,
                    source_slot,
                    row["metric"],
                    "" if row["value"] is None else f"{row['value']:.6f}",
                    row["matches"],
                    row["n"],
                ]
            )


def aggregate_past_log(env, season, log_path=LOG_PATH):
    """Season-scope rows recomputed from this env's date-scope rows.

    Rates are reweighted by exposure (sum matches / sum n) rather than averaged:
    a 0/2 slot must not cancel a 9/10 slot. Rows are appended with scope
    'season' and empty date, so a re-run is purely additive at the file level.
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return

    with open(log_path, newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("env") == env and row.get("scope") == "date"]

    totals = defaultdict(lambda: {"matches": 0, "n": 0})
    for row in rows:
        try:
            matches, n = int(row["matches"]), int(row["n"])
        except (TypeError, ValueError):
            continue
        metric = row.get("metric") or ""
        if n and metric in _METRICS:
            cell = totals[metric]
            cell["matches"] += matches
            cell["n"] += n

    for metric in sorted(totals):
        cell = totals[metric]
        append_log(
            env,
            season,
            "season",
            "",
            "all",
            [{"metric": metric, "value": cell["matches"] / cell["n"], "matches": cell["matches"], "n": cell["n"]}],
            log_path=log_path,
        )


def summarize(date_str, table, slots, pairs, combined, season, env):
    """Human-readable one-page summary of one run's results."""
    slot_labels = ", ".join(f"{slot} ({len(rows)} row(s))" for slot, rows in sorted(slots.items()))
    lines = [
        f"SmartScore feed-fidelity :: {env} :: {date_str} (season {season})",
        f"daily build: {table} - {sum(len(rows) for rows in slots.values())} tracked row(s);"
        f" slots: {slot_labels or 'none'}",
        f"compared pairs: {len(pairs)}",
    ]
    for metric in _METRICS:
        entry = combined[metric]
        if entry["value"] is None:
            lines.append(f"  {metric:<16} n/a (no comparable pairs)")
        else:
            lines.append(f"  {metric:<16} {entry['matches']}/{entry['n']} = {entry['value'] * 100:.1f}%")
    lines.append(
        "adoption gate (MODEL_NOTES FINAL RESULT / V16- collect): >= 90% unit fidelity"
        " AND McNemar wins vs the no-lineup ensemble on BOTH seeds."
    )
    return "\n".join(lines)


def track_date(env, date_str, supabase=None, db_path=RAW_DB, log_path=LOG_PATH):
    """One (env, date) run. Returns ``(status, message)`` for the CLI caller.

    Status is ``tracked`` when metrics were computed and logged, ``skip`` when
    the date cannot be compared (missing actuals / missing projections /
    missing store) - skips print a clear reason and log nothing.
    """
    season = season_for_date(date_str)

    table, projected_rows = load_daily_build(env, date_str, supabase=supabase)
    if not projected_rows:
        return "skip", f"{env}: no PROJECTED/PROJECTED_PROXY rows in {table} for {date_str} - nothing to compare"

    actual_rows = actuals_for_date(date_str, db_path=db_path)
    if actual_rows is None:
        return "skip", f"{env}: local store not found at {db_path} - actuals unavailable"
    if not any(row["game_date"] == date_str for row in actual_rows):
        return (
            "skip",
            f"{env}: no ACTUAL lineups in the local store for {date_str}"
            " (this season's shift charts are not crawled yet - the shift crawler backfills concurrently)."
            " Rerun once the backfill lands.",
        )

    slot_column = detect_slot_column(projected_rows)
    slots = defaultdict(list)
    for row in projected_rows:
        slots[slot_for_row(row, slot_column)].append(row)

    all_pairs, breadcrumbs = [], defaultdict(int)
    for source_slot in sorted(slots):
        slot_pairs, slot_breadcrumbs = pair_with_actuals(
            projected_rows=slots[source_slot], actual_rows=actual_rows, date_str=date_str
        )
        all_pairs.extend(slot_pairs)
        for key, value in slot_breadcrumbs.items():
            breadcrumbs[key] += value

        # Per-slot fidelity is the key number: it says which collection slot
        # to trust. Today the daily build PK-collapses the three collection
        # runs, so this is a single "single" row per run - see module docstring.
        for entry in metric_log_rows(fidelity_metrics(slot_pairs)):
            append_log(env, season, "date", date_str, source_slot, [entry], log_path=log_path)

    combined = fidelity_metrics(all_pairs)
    for entry in metric_log_rows(combined):
        append_log(env, season, "date", date_str, "all", [entry], log_path=log_path)

    aggregate_past_log(env, season, log_path=log_path)
    if table is None:  # pragma: no cover - defensive, never taken
        return "skip", "unreachable"
    return "tracked", summarize(date_str, table, slots, all_pairs, combined, season, env)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Nightly lineup-feed fidelity tracker: daily build PROJECTED rows vs shift-derived ACTUAL lineups.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env", default="prod", choices=("dev", "prod", "both"), help="daily-build table environment (default prod)."
    )
    parser.add_argument(
        "--date", default=et_today(), help="game date YYYY-MM-DD (default: today ET, the collection date)."
    )
    parser.add_argument("--log-path", default=str(LOG_PATH), help="override the fidelity log location (tests).")
    parser.add_argument("--db-path", default=str(RAW_DB), help="override the local store location (tests).")
    args = parser.parse_args(argv)

    date_str = args.date.strip()
    try:
        datetime.date.fromisoformat(date_str)
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got {date_str!r}")

    envs = ("dev", "prod") if args.env == "both" else (args.env,)
    log_path = Path(args.log_path)

    statuses = []
    for env in envs:
        status, message = track_date(env, date_str, db_path=args.db_path, log_path=log_path)
        statuses.append(status)
        print(message)

    if all(status == "skip" for status in statuses):
        return 0

    print(
        f"\nfidelity log: {log_path} (one row per (env, date, source-slot, metric);"
        " season-scope aggregate rows appended per run)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
