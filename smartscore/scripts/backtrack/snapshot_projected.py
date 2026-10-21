#!/usr/bin/env python
"""Snapshot today's *official pre-game* projected lineups into Player-Snapshots-{ENV}.

WHY THIS EXISTS
---------------
The live pipeline only annotates players with projected lineups on a date's
``first_run`` (see README "Known gap"), so whatever rows land depend on that
single sunrise scrape, and a failed match is archived as ``UNKNOWN`` with null
units forever - V10's inventory (0 PROJECTED rows, 5,155 UNKNOWN) measured just
that. RotoWire's power-play labels are moreover persisted verbatim ("POWER
PLAY #1"), not as the PP1/PP2 vocabulary the model uses. This script is a
standalone, schedulable collector: it reads the same two public sources the
pipeline reads (NHL.com's daily lineup-projections article and RotoWire's
lineups page), resolves every player to an NHL id via the official roster
feed, matches both sources onto that date's NHL schedule, and writes one row
per projected skater per game date.

WHAT IT WRITES
--------------
One snapshot row per (date, player_id) with exactly these columns:
``date, player_id, name, team_name, home, lineup_unit, lineup_position_group,
pp_unit, lineup_status``. ``lineup_status`` is ``PROJECTED``. Everything else
(gpg, hgpg, opp_goalie_*, injury_*, scored) is deliberately absent from the
payload: this script has no stats source and must never carry stats columns in
a batch, because PostgREST merge-duplicates resets absent-or-null batch fields
on existing rows (the same reason ``save_player_snapshots`` omits ``scored``).

Write modes (both idempotent against the ``(date, player_id)`` primary key):

* ``append`` - insert rows for pairs that do not exist yet, using
  ``ignore_duplicates`` (PostgREST ``resolution=ignore-duplicates``), so an
  already-stored row is never touched. This is the append-only mode.
* ``refresh`` - PATCH **only the four lineup columns** of rows that exist but
  carry no projection (``lineup_status`` null or ``UNKNOWN``). Rows holding a
  real projection (PROJECTED/CONFIRMED/ACTUAL) are never touched, and no other
  column is ever written. This is what accumulates units onto the rows the
  pipeline archived that morning; overwriting a ``UNKNOWN`` stamp with a real
  same-day projection is the point of the collection - the stamp means "the
  source failed then", not "there is nothing better to file".

SAFETY GATES
------------
* The NHL.com article's ``datePublished`` must fall on the requested game date
  (ET) or writes are refused, so yesterday's lineups can never be filed under
  today's date, and a morning run before the article is refreshed collects
  nothing instead of wrong data. ``--force`` waives this.
* ``--env prod`` must be passed explicitly to touch Player-Snapshots-prod.
* ``--limit`` caps rows written for a first careful rollout.
* ``--dry-run`` prints the plan and writes nothing.

The RotoWire page is undated, so it contributes PP units only when the dated
NHL.com article confirms the same game date.

Usage::

    .venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env dev --dry-run
    .venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env dev
    .venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env prod
    # crontab (10:30 / 13:00 / 16:00 ET) - see MODEL_NOTES "V16- collect
    # projected-lineup persistence".
"""

import argparse
import datetime
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Reused from the live product: the exact two lineup scrapers the Lambda chain
# runs, plus the name-normaliser every join here needs (mojibake, accents,
# "A.J." vs "AJ").
from nhl_client import BASE_URL, _get_json  # noqa: E402 - same throttle/cache discipline

from nhl_lineups import normalize_player_name  # noqa: E402

ET = ZoneInfo("America/Toronto")
NHL_LINEUP_ARTICLE_URL = "https://www.nhl.com/news/nhl-lineup-projections-2026-27-season"

# Exact column set sent to Supabase. NEVER add a stats column here: the batch
# is aimed at the same PK rows the pipeline wrote and any column present in the
# batch is included in PostgREST's DO UPDATE SET on conflict.
PAYLOAD_COLUMNS = (
    "date",
    "player_id",
    "name",
    "team_name",
    "home",
    "lineup_unit",
    "lineup_position_group",
    "pp_unit",
    "lineup_status",
)

BATCH_SIZE = 200
PAGE_SIZE = 1000

# Statuses that mean a real projection is already stored; refresh never
# overwrites these.
KEEP_STATUSES = ("PROJECTED", "CONFIRMED", "ACTUAL")

_roster_cache = {}


def et_today():
    return datetime.datetime.now(ET).strftime("%Y-%m-%d")


def _start_date_et(start_utc):
    return datetime.datetime.fromisoformat(start_utc.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")


def schedule_games(date_str, delay_seconds=0.5):
    """Games starting on one game date (ET): game_id, gameState and both teams.

    ``schedule/{date}`` answers with the seven-day window containing the date
    (nhl_client's SCHEDULE_WINDOW_DAYS note), so every game is classified by
    its own ET start date and only the requested date is kept.
    """
    payload = _get_json(f"{BASE_URL}/schedule/{date_str}", delay_seconds)
    games = []
    for day in payload.get("gameWeek") or []:
        for game in day.get("games") or []:
            start_utc = game.get("startTimeUTC")
            if not start_utc or _start_date_et(start_utc) != date_str:
                continue
            games.append(
                {
                    "game_id": game.get("id"),
                    "game_state": game.get("gameState"),
                    "start_utc": start_utc,
                    "away": _schedule_team(game.get("awayTeam")),
                    "home": _schedule_team(game.get("homeTeam")),
                }
            )
    return games


def _schedule_team(team):
    if not team:
        return team
    return {
        "abbrev": team.get("abbrev"),
        "place": team.get("placeName", {}).get("default"),
        "common": team.get("commonName", {}).get("default"),
    }


def _match_team(name, side_a, side_b):
    """Which of two schedule teams does a source team name belong to?"""
    if not name:
        return None
    key = normalize_player_name(name)
    for side in (side_a, side_b):
        if not side or not side.get("abbrev"):
            continue
        candidates = {
            normalize_player_name(side.get("place") or ""),
            normalize_player_name(side.get("common") or ""),
        }
        if key and key in candidates:
            return side
    return None


def _match_schedule_game(away_name, home_name, games):
    """Resolve one scrape payload's matchup onto exactly one scheduled game.

    Both source names must resolve, and to different schedule sides, or the
    matchup is not confidently this game. Name-keyed so the sources'
    "Maple Leafs" style nicknames still land on the schedule's place name
    ("Toronto") - the mismatch that leaves the live merge empty (see V16 in
    MODEL_NOTES).
    """
    resolved = []
    for game in games:
        away_target = _match_team(away_name, game["away"], game["home"])
        home_target = _match_team(home_name, game["away"], game["home"])
        if away_target and home_target and away_target != home_target:
            resolved.append((game, away_target, home_target))

    if len(resolved) > 1:
        raise LookupError(f"ambiguous matchup {away_name}@{home_name} against {len(games)} scheduled game(s)")
    if not resolved:
        return None

    game, away_target, home_target = resolved[0]
    return {"game": game, "away": away_target, "home": home_target}


def _roster(abbrev, delay_seconds=0.5):
    """``{normalised_name: (player_id, display_name)}`` for a team's current roster."""
    if abbrev in _roster_cache:
        return _roster_cache[abbrev]
    try:
        payload = _get_json(f"{BASE_URL}/roster/{abbrev}/current", delay_seconds)
    except requests.HTTPError:
        payload = {}
    roster = {}
    for group in ("forwards", "defensemen", "goalies"):
        for player in payload.get(group) or []:
            player_id = player.get("id")
            first = (player.get("firstName") or {}).get("default") or ""
            last = (player.get("lastName") or {}).get("default") or ""
            display = f"{first} {last}".strip()
            if player_id and display:
                roster[normalize_player_name(display)] = (int(player_id), display)
    _roster_cache[abbrev] = roster
    return roster


def _clean_name(name):
    """Strip the latin-1 mojibake/nbsp debris the article body carries.

    ``get_nhl_com_lineups``'s own repair only fixes 'Â'+C1 pairs; a trailing '
    Â' from an mangled non-breaking space survives it and would turn
    'Tony DeAngeloÂ' etc. into a roster miss.
    """
    return (name or "").replace("\u00c2", " ").replace("\xa0", " ").strip()


def _pp_label(label):
    """RotoWire's "POWER PLAY #1" -> "PP1"; None for anything unrecognised."""
    text = (label or "").upper()
    if "#1" in text:
        return "PP1"
    if "#2" in text:
        return "PP2"
    return None


def _article_team_entries(team):
    """Forward/defence units for one team parsed from the NHL.com article body.

    Returns ``[(player_name, lineup_unit F1..F4/D1..D3, position_group, None)]``.
    Unlike the live product's merge (which stores forward units only) defence
    pairs are kept too: the backtrack actuals carry D1-D3, so the projections
    must as well or unit fidelity cannot be measured for defence.
    """
    entries = []
    for unit in team.get("units") or []:
        label = unit.get("label") or ""
        group = "F" if label.startswith("F") else ("D" if label.startswith("D") else None)
        if not group:
            continue
        for player in unit.get("players") or []:
            entries.append((player, label, group, None))
    return entries


def _rotowire_team_entries(team):
    """RotoWire power-play units for one team as ``[(name, None, None, PP1/PP2)]``."""
    entries = []
    for unit in team.get("pp_units") or []:
        label = _pp_label(unit.get("label"))
        if not label:
            continue
        for player in unit.get("players") or []:
            entries.append((player.get("name"), None, None, label))
    return entries


def article_published_date():
    """ET date the article's JSON-LD carries, or None when unavailable."""
    from nhl_lineups import _fetch_text  # noqa: PLC0415 - keeps the import local like the callers there

    document = _fetch_text(NHL_LINEUP_ARTICLE_URL)
    if not document:
        return None

    import html as _html  # noqa: PLC0415 - mirrored from get_nhl_com_lineups

    source = _html.unescape(document)
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', source, re.S):
        try:
            decoded = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and "NewsArticle" in str(decoded.get("@type", "")):
            published = decoded.get("datePublished")
            if published:
                return _start_date_et(published)
    return None


def build_rows(date_str, delay_seconds=0.5, limit=None):
    """Collect every projected skater row for one game date.

    Second element is a list of skipped-item diagnostics; rows share no
    (date, player_id) pair (the first source's field wins on conflict, so the
    article's forward line stays the row and RotoWire only fills pp_unit). To
    let both sources contribute to one player, the pp pass is applied onto the
    rows collected before it rather than adding a second row for the same key.
    """
    games = schedule_games(date_str, delay_seconds=delay_seconds)
    if not games:
        print(f"{date_str}: no NHL games scheduled")
        return [], ["no-scheduled-games"]

    from nhl_lineups import get_nhl_com_lineups, get_rotowire_lineups  # noqa: PLC0415

    nhl_games = get_nhl_com_lineups()
    rotowire_games = get_rotowire_lineups()
    if not nhl_games and not rotowire_games:
        return [], ["both lineup sources returned no games"]

    rows = {}
    sequence = []  # stable ordering for reports, mirrors rows dict
    skipped = []

    for source_games, team_fn in ((nhl_games, _article_team_entries), (rotowire_games, _rotowire_team_entries)):
        for game in source_games:
            try:
                matchup = _match_schedule_game(game.get("away"), game.get("home"), games)
            except LookupError as e:
                skipped.append(str(e))
                continue
            if matchup is None:
                skipped.append(f"{game.get('away')}@{game.get('home')}: no schedule match")
                continue

            game_date = _start_date_et(matchup["game"]["start_utc"])
            for team in game.get("teams") or []:
                target = _match_team(team.get("name"), matchup["away"], matchup["home"])
                if not target or not target.get("abbrev"):
                    skipped.append(f"{team.get('name')!r}: unresolved team on the schedule")
                    continue
                roster = _roster(target["abbrev"], delay_seconds=delay_seconds)

                for name, unit, group, pp in team_fn(team):
                    resolved = roster.get(normalize_player_name(_clean_name(name)))
                    if not resolved:
                        skipped.append(f"{target['abbrev']} {name!r}: not on current roster")
                        continue
                    player_id, display = resolved
                    key = (game_date, player_id)
                    row = rows.get(key)
                    home_flag = matchup["game"]["home"]["abbrev"] == target["abbrev"]
                    if row is None:
                        row = {
                            "date": game_date,
                            "player_id": player_id,
                            "name": display,
                            "team_name": target["place"],
                            "home": home_flag,
                            "lineup_unit": unit,
                            "lineup_position_group": group,
                            "pp_unit": pp,
                            "lineup_status": "PROJECTED",
                        }
                        rows[key] = row
                        sequence.append(row)
                        continue
                    # Merge the other source's contribution: article units win
                    # where present, RotoWire fills pp-only players.
                    if unit and not row["lineup_unit"]:
                        row["lineup_unit"], row["lineup_position_group"] = unit, group
                    if pp and not row["pp_unit"]:
                        row["pp_unit"] = pp
                    if row["lineup_unit"] or row["pp_unit"]:
                        row["lineup_status"] = "PROJECTED"

    if limit is not None:
        sequence = sequence[:limit]
    return sequence, skipped


def _existing_rows(supabase, table, date_str):
    """``{player_id: row}`` already stored for the date, paged."""
    client = supabase.table(table)
    existing = {}
    offset = 0
    while True:
        response = (
            client.select("player_id,lineup_status,lineup_unit,pp_unit,gpg,hgpg,scored,name,team_name")
            .eq("date", date_str)
            .order("player_id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = response.data or []
        for row in batch:
            existing[int(row["player_id"])] = row
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return existing


def _group_key(row):
    return (row["lineup_unit"], row["lineup_position_group"], row["pp_unit"], row["lineup_status"])


def _patch_lineup_fields(supabase, table, date_str, groups):
    """PATCH only the four lineup columns, grouped by identical field values."""
    patched = 0
    for (lineup_unit, group, pp_unit, status), player_ids in groups.items():
        payload = {
            "lineup_unit": lineup_unit,
            "lineup_position_group": group,
            "pp_unit": pp_unit,
            "lineup_status": status,
        }
        (
            supabase.table(table)
            .update(payload, returning="minimal")
            .eq("date", date_str)
            .in_("player_id", player_ids)
            .execute()
        )
        patched += len(player_ids)
    return patched


def run(supabase, env, date_str, modes, limit=None, force=False, dry_run=False):
    article_date = article_published_date()
    if article_date != date_str and not force:
        return 1, (
            f"article covers {article_date}, requested {date_str}; "
            "refusing to write (use --force if verified by hand)"
        )

    rows, skipped = build_rows(date_str, limit=limit)
    forwards = sum(1 for row in rows if row["lineup_position_group"] == "F")
    defence = sum(1 for row in rows if row["lineup_position_group"] == "D")
    pp = sum(1 for row in rows if row["pp_unit"])
    print(f"{date_str}: collected {len(rows)} rows (F {forwards}, D {defence}, pp non-null {pp}); "
          f"{len(skipped)} item(s) skipped")
    for line in skipped[:15]:
        print(f"  skipped: {line}")
    if len(skipped) > 15:
        print(f"  ... {len(skipped) - 15} more")

    if dry_run:
        for row in rows[:5]:
            print("  sample:", json.dumps(row))
        return 0, "dry-run only"

    if not rows:
        return 0, "nothing to write"

    table = f"Player-Snapshots-{env}"
    existing = _existing_rows(supabase, table, date_str)
    print(f"  existing rows for {date_str}: {len(existing)}")

    appended = refreshed = 0
    if "append" in modes:
        new_rows = [row for row in rows if int(row["player_id"]) not in existing]
        for start in range(0, len(new_rows), BATCH_SIZE):
            batch = new_rows[start : start + BATCH_SIZE]
            (
                supabase.table(table)
                .upsert(batch, on_conflict="date,player_id", ignore_duplicates=True, returning="minimal")
                .execute()
            )
            appended += len(batch)
        print(f"  append: {appended} new row(s) inserted")

    if "refresh" in modes:
        groups = defaultdict(list)
        for row in rows:
            stored = existing.get(int(row["player_id"]))
            # Only rows that exist and carry no real projection - a source
            # failure stamp in the archive, or a bare new row from *this*
            # script's own append - are replaced by the projection. Rows with
            # PROJECTED/CONFIRMED/ACTUAL are left exactly as stored.
            if stored is None or (stored.get("lineup_status") or "") in KEEP_STATUSES:
                continue
            groups[_group_key(row)].append(int(row["player_id"]))
        if groups:
            refreshed = _patch_lineup_fields(supabase, table, date_str, groups)
        print(f"  refresh: {refreshed} stored row(s) patched with projection fields")

    return 0, f"appended {appended}, refreshed {refreshed}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env", default="dev", choices=("dev", "prod"), help="Supabase table suffix (default dev).")
    parser.add_argument("--date", default=None, help="Game date YYYY-MM-DD (default: today, America/Toronto).")
    parser.add_argument("--mode", nargs="+", choices=("append", "refresh"), default=["append", "refresh"])
    parser.add_argument("--limit", type=int, default=None, help="Cap rows written (safety for a first rollout).")
    parser.add_argument("--force", action="store_true", help="Write even when the article date does not match.")
    parser.add_argument("--dry-run", action="store_true", help="Collect and report only; no writes.")
    args = parser.parse_args()

    date_str = args.date or et_today()
    if args.date and args.date != et_today() and not args.force:
        print("note: the projected sources are a daily slate; non-today dates need --force and usually fail")

    os.environ["ENV"] = args.env
    from config import SUPABASE_ADMIN_CLIENT  # noqa: PLC0415 - initialised after ENV is final

    code, message = run(
        SUPABASE_ADMIN_CLIENT,
        args.env,
        date_str,
        set(args.mode),
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(f"{date_str}: {message}")
    return code


if __name__ == "__main__":
    sys.exit(main())
