#!/usr/bin/env python3
"""Backfill team identity (team_name, team_abbr, opponent_*) into Player-Snapshots (#113 follow-up).

The archive wore three different shapes of team identity over its life:

===============  ==========================================================
Era              What a row carries
===============  ==========================================================
through          Mongo ``team_abbr`` (smartscore-api's
2025-01-24       change_team_name_to_abbrev renamed team_name away and
                 backfilled all of it); the #113 port dropped the abbr, so
                 Supabase has neither field filled.
2025-01-25 ..    Nothing. The parse-lambda refactor stopped putting team
2026-10-01       identity in the payload, orphaning the team-level stats
                 (tgpg, otga, otshga, home) sitting next to them.
2026-10-02 ..    ``team_name`` (NHL schedule placeName) via lineup
                 retrieval (#122); no abbr, no opponent.
===============  ==========================================================

This script rebuilds four columns for every row:

* **era 1** - ``team_abbr`` straight from Mongo (authoritative, per row);
  ``team_name``/opponent from that date's NHL schedule - the same payload the
  live pipeline reads placeName from.
* **era 2** - player -> team resolved through, in order:
  1. that date's boxscores (api-web.nhle.com, per game, so trades are exact
     on days the player played);
  2. bracketing appearances - the same team in the nearest boxscores before
     and after the date (healthy scratches land here);
  3. the player's single NHL stint for that season from
     ``/v1/player/{id}/landing``;
  4. the season roster (``/v1/roster/{abbr}/{season}``) when landing has
     nothing. Two-sided disagreements (traded then scratched across the
     trade) stay unresolved and are listed rather than guessed.
* **era 3** - boxscore/bracket/landing for ``team_abbr``; ``team_name`` is
  *kept* as stored, verified equal to the schedule place (mismatch reported).

Everything NHL is cached under ``.nhl_cache/`` (gitignored) keyed by date or
id, so a re-run fetches only what is missing. A full scrape is roughly 4k
requests / ~25 minutes at a polite pace - run it detached and read its log::

    uv run python smartscore/scripts/backfill_team_identity.py --scrape

Then inspect, then write::

    uv run python smartscore/scripts/backfill_team_identity.py     # gates + report, writes nothing
    uv run python smartscore/scripts/backfill_team_identity.py --apply   # gates, canary row, bulk, verify

The write is a column-limited upsert on ``(date, player_id)`` (raw PostgREST
``Prefer: resolution=merge-duplicates`` + ``columns=...`` - postgrest-py's
``upsert()`` cannot restrict the column list): only the four identity columns
plus the key are sent, so no other column of an existing row can change. It is
idempotent - a re-run converges on the same result.

Gates (``--apply`` refuses to write unless all pass):

* era 1: every Mongo abbr must resolve to a schedule place name (100%);
* era 2: >= ERA2_MIN_COVERAGE of rows must resolve; the rest are listed in
  ``.nhl_cache/unresolved.json`` for review;
* era 3: every row must resolve to an abbr, and the schedule place must equal
  the stored ``team_name``.

Required environment: ``ENV``, ``MONGODB_URI``, ``SUPABASE_URL``,
``SUPABASE_SERVICE_ROLE_KEY`` (``config.py`` reads them; ``load_dotenv()``
picks up the local gitignored ``.env``).
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import requests
from pymongo import MongoClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aws_lambda_powertools import Logger  # noqa: E402

from config import (  # noqa: E402
    ENV,
    SUPABASE_ADMIN_CLIENT,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_URL,
)
from player_archive import SNAPSHOT_TABLE, _retry  # noqa: E402

logger = Logger()

SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{date}"
BOXSCORE_URL = "https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore"
LANDING_URL = "https://api-web.nhle.com/v1/player/{player_id}/landing"
ROSTER_URL = "https://api-web.nhle.com/v1/roster/{team_abbr}/{season}"

# Polite pace: NHL is a public CDN with no documented quota; 0.25s between
# requests keeps a ~4k-request scrape under the radar and well inside its ETA.
REQUEST_INTERVAL_SECONDS = 0.25
REQUEST_ATTEMPTS = 5

DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / ".nhl_cache"

# The four columns this script owns. The upsert payload is exactly these plus
# the primary key, so no other column can be touched even in principle.
IDENTITY_COLUMNS = ("team_name", "team_abbr", "opponent_abbr", "opponent_name")
APPLY_COLUMNS = ("date", "player_id", *IDENTITY_COLUMNS)

ERA2_MIN_COVERAGE = 0.999
BATCH_SIZE = 500
SAMPLE_SIZE = 3
PAGE_SIZE = 1000  # PostgREST db-max-rows; reads paginate, writes stay under it.

ERA_MONGO_ABBR = "era1"
ERA_STORED_NAME = "era3"
ERA_GAP = "era2"


class BackfillStats:
    """Counters for the report (and the gates that read them)."""

    def __init__(self):
        self.rows_seen = 0
        self.rows_payload = 0
        self.per_era = Counter()
        self.resolved_by = Counter()
        self.unresolved_by_era = Counter()
        self.unresolved_detail = []
        self.opponent_missing = Counter()
        self.era3_name_mismatch = 0

    def as_dict(self):
        return {
            "rows_seen": self.rows_seen,
            "rows_in_payload": self.rows_payload,
            "era1_rows": self.per_era[ERA_MONGO_ABBR],
            "era2_rows": self.per_era[ERA_GAP],
            "era3_rows": self.per_era[ERA_STORED_NAME],
            "resolved_mongo": self.resolved_by["mongo"],
            "resolved_boxscore": self.resolved_by["boxscore"],
            "resolved_bracket": self.resolved_by["bracket"],
            "resolved_landing": self.resolved_by["landing"],
            "resolved_roster": self.resolved_by["roster"],
            "unresolved": sum(self.unresolved_by_era.values()),
            "opponent_missing": sum(self.opponent_missing.values()),
            "era3_name_mismatch": self.era3_name_mismatch,
        }


def mongo_collection_name(env):
    """The collection the worker wrote to (same split as port_mongo_snapshots)."""
    return "SmartScore" if env == "prod" else "SmartScoreDev"


MONGO_DATABASE = "players"
MONGO_COLLECTION = mongo_collection_name(ENV)


def coerce_player_id(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# NHL API + cache
# ---------------------------------------------------------------------------


def fetch_json(url, cache_path=None):
    """GET a URL as JSON, with disk cache, pacing and bounded retries.

    A cached entry short-circuits before any network call, which is what makes
    the scrape resumable. 404 returns ``None`` (NHL uses it for "no such
    date/game") and is not retried; 429/5xx back off exponentially.
    """
    if cache_path is not None and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    for attempt in range(REQUEST_ATTEMPTS):
        response = requests.get(url, timeout=30)
        time.sleep(REQUEST_INTERVAL_SECONDS)
        if response.status_code == 404:
            return None
        if response.status_code == 200:
            payload = response.json()
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_path.with_name(cache_path.name + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, cache_path)
            return payload
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(2**attempt)
            continue
        response.raise_for_status()

    raise RuntimeError(f"Giving up on {url} after {REQUEST_ATTEMPTS} attempts")


def games_for_date(schedule_payload, date):
    """The games list for one date inside a /v1/schedule response.

    The endpoint answers with a 7-day ``gameWeek`` array; only the entry whose
    ``date`` matches counts (an archive date with no games yields ``[]``).
    """
    games = []
    for day in (schedule_payload or {}).get("gameWeek") or []:
        if day.get("date") == date:
            games.extend(day.get("games") or [])
    return games


def side_fields(game, side, field):
    """One localized string field of one side (``abbrev`` / place / common)."""
    team = game.get(f"{side}Team") or {}
    value = team.get(field) or {}
    if isinstance(value, dict):
        return value.get("default")
    return value


def parse_boxscore(payload):
    """(pid -> team abbr) for every participant listed in a boxscore.

    ``playerByGameStats`` splits by side and group (forwards/defense/goalies);
    the archive contains goalies too, so all three count.
    """
    mapping = {}
    if not payload:
        return mapping
    by_side = payload.get("playerByGameStats") or {}
    for side in ("homeTeam", "awayTeam"):
        abbr = (payload.get(side) or {}).get("abbrev")
        if not abbr:
            continue
        for group in ("forwards", "defense", "goalies"):
            for player in (by_side.get(side) or {}).get(group) or []:
                pid = coerce_player_id(player.get("playerId"))
                if pid is not None:
                    mapping[pid] = abbr
    return mapping


def season_for(date):
    """NHL season id (``20242025``) containing the date; September starts one."""
    year, month = int(date[:4]), int(date[5:7])
    start = year if month >= 9 else year - 1
    return f"{start}{start + 1}"


def parse_landing_stints(payload):
    """{season: {NHL team names}} from a player landing payload.

    A traded player has several ``seasonTotals`` entries for one season
    (``sequence`` orders them); the endpoint gives no dates for the stints,
    which is why the resolver only trusts a *single* team for the season.
    """
    seasons = {}
    for entry in (payload or {}).get("seasonTotals") or []:
        if entry.get("leagueAbbrev") != "NHL":
            continue
        name = (entry.get("teamName") or {}).get("default")
        if name:
            seasons.setdefault(str(entry.get("season")), set()).add(name)
    return seasons


def parse_roster_player_ids(payload):
    """Set of player ids in a /v1/roster response (groups: forw/def/goalie)."""
    pids = set()
    if not payload:
        return pids
    for group in ("forwards", "defensemen", "goalies"):
        for player in payload.get(group) or []:
            pid = coerce_player_id(player.get("id"))
            if pid is not None:
                pids.add(pid)
    return pids


# ---------------------------------------------------------------------------
# Indexes built from the schedule cache
# ---------------------------------------------------------------------------


def build_schedule_index(schedule_by_date):
    """From every cached schedule: per-date game lookup plus global name maps.

    Returns ``(games_by_date, place_by_abbr, abbr_by_teamname)``:

    * ``games_by_date[date][abbr] = (place, opponent_abbr, opponent_place)`` -
      where that team played that date, and against whom;
    * ``place_by_abbr`` - global, because an abbr's place name is stable
      across this window (a relocation means a *new* abbr, ARI vs UTA), and
      an idle team's row still needs a team_name;
    * ``abbr_by_teamname`` - keys ``"Pittsburgh Penguins"``-style names from
      player landing back onto abbrs (``place + " " + commonName``).
    """
    games_by_date = {}
    place_by_abbr = {}
    abbr_by_teamname = {}
    for date, payload in sorted(schedule_by_date.items()):
        per_abbr = {}
        for game in games_for_date(payload, date):
            sides = {}
            for side in ("home", "away"):
                sides[side] = (
                    side_fields(game, side, "abbrev"),
                    side_fields(game, side, "placeName"),
                    side_fields(game, side, "commonName"),
                )
            home_abbr, home_place, home_common = sides["home"]
            away_abbr, away_place, away_common = sides["away"]
            for own, opponent in ((sides["home"], (away_abbr, away_place)), (sides["away"], (home_abbr, home_place))):
                own_abbr, own_place, own_common = own
                if not own_abbr:
                    continue
                per_abbr[own_abbr] = (own_place, opponent[0], opponent[1])
                place_by_abbr.setdefault(own_abbr, own_place)
                if own_place and own_common:
                    abbr_by_teamname.setdefault(f"{own_place} {own_common}", own_abbr)
        games_by_date[date] = per_abbr
    return games_by_date, place_by_abbr, abbr_by_teamname


# ---------------------------------------------------------------------------
# Era 2/3 resolution
# ---------------------------------------------------------------------------


def resolve_player(pid, date, timeline, landings, rosters, abbr_by_teamname):
    """Resolve one era-2/3 player to a team abbr; returns ``(abbr, method)``.

    ``timeline`` is ``{pid: {date: abbr}}`` from boxscores, ``landings`` is
    ``{pid: {season: {team names}}}``, ``rosters`` is
    ``{season: {pid: {abbrs}}}``. Hierarchy (see module docstring): exact
    boxscore -> two-sided bracket -> single landing stint -> unique season
    roster. Anything that would require guessing across a trade returns
    ``(None, reason)``; the caller lists it instead of guessing.
    """
    appearances = timeline.get(pid) or {}
    if date in appearances:
        return appearances[date], "boxscore"

    before = max((d for d in appearances if d < date), default=None)
    after = min((d for d in appearances if d > date), default=None)
    if before and after and appearances[before] == appearances[after]:
        return appearances[before], "bracket"

    season_stints = (landings.get(pid) or {}).get(season_for(date)) or set()
    stint_teams = {abbr_by_teamname.get(name) for name in season_stints} - {None}
    if len(stint_teams) == 1:
        (team,) = stint_teams
        # Trust the stint only when it never contradicts a game-day sighting.
        if all(appearances.get(d) == team for d in appearances):
            return team, "landing"

    candidates = (rosters.get(season_for(date)) or {}).get(pid) or set()
    if len(candidates) == 1:
        (team,) = candidates
        if all(appearances.get(d) == team for d in appearances):
            return team, "roster"

    if before and after:
        return None, "trade-window"
    if before or after:
        return None, "one-sided-bracket"
    if pid not in landings:
        return None, "no-timeline-no-landing"
    return None, "landing-multi-or-mismatch"


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def load_era3_stored_names():
    """((date, player_id) -> stored team_name) for every row that has one.

    The authoritative era-3 set: rows *without* team_name are era 1/era 2 by
    construction. Paginates because PostgREST caps a response at ~1000 rows.
    """
    stored = {}
    start = 0
    while True:

        def run(offset=start):
            return (
                SUPABASE_ADMIN_CLIENT.table(SNAPSHOT_TABLE)
                .select("date,player_id,team_name")
                .not_.is_("team_name", None)
                .range(offset, offset + PAGE_SIZE - 1)
                .execute()
                .data
            )

        page = _retry(lambda: run(start), f"Reading stored team_name rows at offset {start}")
        for row in page:
            pid = coerce_player_id(row.get("player_id"))
            if pid is not None:
                stored[(row["date"], pid)] = row.get("team_name")
        if len(page) < PAGE_SIZE:
            return stored
        start += PAGE_SIZE


def build_payloads(collection, stored_names, schedule_by_date, box_by_date, landings, rosters, only_date, stats):
    """Project every archive row onto APPLY_COLUMNS; returns ``{(date, pid): row}``.

    Unresolved rows are *excluded* (they keep their current nulls) and listed -
    the gates decide whether that is rare enough to apply. The dict keys
    deduplicate the 169 identical copies the port already collapsed.
    """
    games_by_date, place_by_abbr, abbr_by_teamname = build_schedule_index(schedule_by_date)
    timeline = build_timeline(box_by_date)

    rows = {}
    dates = [only_date] if only_date else sorted(collection.distinct("date", {"date": {"$type": "string"}}))
    projection = {"_id": 0, "date": 1, "id": 1, "name": 1, "team_abbr": 1, "team_name": 1}

    for date in dates:
        for doc in collection.find({"date": date}, projection).sort("_id", 1):
            stats.rows_seen += 1
            pid = coerce_player_id(doc.get("id"))
            if doc.get("team_abbr"):
                era = ERA_MONGO_ABBR
            elif (date, pid) in stored_names:
                era = ERA_STORED_NAME
            else:
                era = ERA_GAP
            stats.per_era[era] += 1

            if pid is None:
                stats.unresolved_by_era[era] += 1
                stats.unresolved_detail.append(
                    {"date": date, "player_id": None, "name": doc.get("name"), "reason": "non-numeric-id"}
                )
                continue

            if era == ERA_MONGO_ABBR:
                abbr, method = doc.get("team_abbr"), "mongo"
            else:
                abbr, method = resolve_player(pid, date, timeline, landings, rosters, abbr_by_teamname)

            if not abbr:
                stats.unresolved_by_era[era] += 1
                stats.unresolved_detail.append(
                    {"date": date, "player_id": pid, "name": doc.get("name"), "reason": method}
                )
                continue

            place = place_by_abbr.get(abbr)
            if not place:
                stats.unresolved_by_era[era] += 1
                stats.unresolved_detail.append(
                    {"date": date, "player_id": pid, "name": doc.get("name"), "reason": "abbr-not-in-any-schedule"}
                )
                continue

            stats.resolved_by[method] += 1

            if era == ERA_STORED_NAME:
                team_name = stored_names.get((date, pid))
                if team_name is None:
                    stats.era3_name_mismatch += 1
                    team_name = place
                elif team_name != place:
                    stats.era3_name_mismatch += 1
            else:
                team_name = place

            game_entry = games_by_date.get(date, {}).get(abbr)
            if game_entry:
                _, opp_abbr, opp_place = game_entry
            else:
                stats.opponent_missing[era] += 1
                opp_abbr = opp_place = None

            rows[(date, pid)] = {
                "date": date,
                "player_id": pid,
                "team_name": team_name,
                "team_abbr": abbr,
                "opponent_abbr": opp_abbr,
                "opponent_name": opp_place,
            }

    stats.rows_payload = len(rows)
    return rows


def build_timeline(box_by_date):
    """``{pid: {date: abbr}}`` from every cached boxscore."""
    timeline = {}
    for date, mapping in sorted(box_by_date.items()):
        for pid, abbr in mapping.items():
            timeline.setdefault(pid, {})[date] = abbr
    return timeline


def evaluate_gates(stats):
    """List of human-readable gate failures; empty means --apply may proceed."""
    failures = []

    era1_total = stats.per_era[ERA_MONGO_ABBR]
    era1_bad = stats.unresolved_by_era[ERA_MONGO_ABBR]
    if era1_total and era1_bad:
        failures.append(
            f"era 1: {era1_bad}/{era1_total} Mongo abbrs did not resolve to a schedule place (must be 100%)"
        )

    era2_total = stats.per_era[ERA_GAP]
    era2_bad = stats.unresolved_by_era[ERA_GAP]
    coverage = 1 - (era2_bad / era2_total) if era2_total else 1.0
    if era2_total and coverage < ERA2_MIN_COVERAGE:
        failures.append(
            f"era 2: coverage {coverage:.4%} < {ERA2_MIN_COVERAGE:.2%} ({era2_bad} unresolved of {era2_total})"
        )

    era3_total = stats.per_era[ERA_STORED_NAME]
    era3_bad = stats.unresolved_by_era[ERA_STORED_NAME]
    if era3_total and era3_bad:
        failures.append(f"era 3: {era3_bad}/{era3_total} rows did not resolve to an abbr (must be 100%)")
    if stats.era3_name_mismatch:
        failures.append(f"era 3: schedule place disagrees with stored team_name on {stats.era3_name_mismatch} row(s)")

    return failures


# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------


def scrape(collection, cache_dir):
    """Fetch everything the build reads: schedules, boxscores, landing, rosters.

    Four resumable phases; progress prints land in the log so a detached run
    can be inspected with a single tail. Cache hits make an interrupted run
    pick up where it stopped.
    """
    started = time.time()
    dates = sorted(collection.distinct("date", {"date": {"$type": "string"}}))
    dates_with_abbr = set(collection.distinct("date", {"team_abbr": {"$type": "string"}}))
    box_dates = [d for d in dates if d not in dates_with_abbr]
    print(f"[scrape] {len(dates)} archive dates, {len(box_dates)} need boxscores")

    schedule_by_date = {}
    for index, date in enumerate(dates, 1):
        schedule_by_date[date] = fetch_json(SCHEDULE_URL.format(date=date), cache_dir / "schedule" / f"{date}.json")
        if index % 50 == 0 or index == len(dates):
            print(f"[scrape] schedules {index}/{len(dates)} ({time.time() - started:.0f}s)")

    games_needed = []
    for date in box_dates:
        for game in games_for_date(schedule_by_date.get(date), date):
            if game.get("id"):
                games_needed.append((date, game["id"]))
    print(f"[scrape] {len(games_needed)} boxscores to check")

    box_by_date = {date: {} for date in box_dates}
    fetched = 0
    for index, (date, gid) in enumerate(games_needed, 1):
        path = cache_dir / "boxscore" / f"{gid}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = fetch_json(BOXSCORE_URL.format(game_id=gid), path)
            fetched += 1
        box_by_date[date].update(parse_boxscore(payload))
        if index % 100 == 0 or index == len(games_needed):
            print(f"[scrape] boxscores {index}/{len(games_needed)} (fetched {fetched}, {time.time() - started:.0f}s)")

    # Landing payloads for every era-2/3 player the timeline cannot settle.
    _, _, abbr_by_teamname = build_schedule_index(schedule_by_date)
    timeline = build_timeline(box_by_date)
    needs_landing = set()
    for date in box_dates:
        for doc in collection.find({"date": date}, {"_id": 0, "id": 1}):
            pid = coerce_player_id(doc.get("id"))
            if pid is None:
                continue
            abbr, _ = resolve_player(pid, date, timeline, {}, {}, abbr_by_teamname)
            if not abbr:
                needs_landing.add(pid)
    print(f"[scrape] {len(needs_landing)} players need landing payloads")

    for index, pid in enumerate(sorted(needs_landing), 1):
        path = cache_dir / "landing" / f"{pid}.json"
        if not path.exists():
            fetch_json(LANDING_URL.format(player_id=pid), path)
        if index % 100 == 0 or index == len(needs_landing):
            print(f"[scrape] landing {index}/{len(needs_landing)} ({time.time() - started:.0f}s)")

    # Season rosters (last-resort fallback; cheap enough to always fetch).
    seasons = sorted({season_for(d) for d in box_dates})
    abbrs = sorted(
        {
            abbr
            for payload in schedule_by_date.values()
            for day in payload.get("gameWeek") or []
            for game in day.get("games") or []
            for abbr in (side_fields(game, "home", "abbrev"), side_fields(game, "away", "abbrev"))
            if abbr
        }
    )
    print(f"[scrape] rosters: {len(seasons)} seasons x {len(abbrs)} abbrs")
    for season in seasons:
        for abbr in abbrs:
            path = cache_dir / "roster" / f"{season}_{abbr}.json"
            if not path.exists():
                fetch_json(ROSTER_URL.format(team_abbr=abbr, season=season), path)

    print(f"[scrape] done in {time.time() - started:.0f}s; cache at {cache_dir}")


def load_cache(cache_dir, box_dates):
    """Read the scrape product back into the structures the build needs.

    Returns ``(schedule_by_date, box_by_date, landings, rosters)`` where
    ``landings`` is ``{pid: {season: {team names}}}`` and ``rosters`` is
    ``{season: {pid: {abbrs}}}`` (a set, so a two-team season reads as
    ambiguous rather than silently picking one).
    """
    schedule_by_date = {}
    schedule_dir = cache_dir / "schedule"
    if schedule_dir.exists():
        for path in sorted(schedule_dir.glob("*.json")):
            schedule_by_date[path.stem] = json.loads(path.read_text(encoding="utf-8"))

    # Schedule-driven, so box rows land on archive dates only (the file name
    # carries the game id, not the date).
    box_by_date = {date: {} for date in box_dates}
    for date in box_dates:
        for game in games_for_date(schedule_by_date.get(date), date):
            path = cache_dir / "boxscore" / f"{game.get('id')}.json"
            if path.exists():
                box_by_date[date].update(parse_boxscore(json.loads(path.read_text(encoding="utf-8"))))

    landings = {}
    landing_dir = cache_dir / "landing"
    if landing_dir.exists():
        for path in sorted(landing_dir.glob("*.json")):
            pid = coerce_player_id(path.stem)
            if pid is not None:
                landings[pid] = parse_landing_stints(json.loads(path.read_text(encoding="utf-8")))

    rosters = {}
    roster_dir = cache_dir / "roster"
    if roster_dir.exists():
        for path in sorted(roster_dir.glob("*.json")):
            season, _, abbr = path.stem.partition("_")
            if not abbr:
                continue
            for pid in parse_roster_player_ids(json.loads(path.read_text(encoding="utf-8"))):
                rosters.setdefault(season, {}).setdefault(pid, set()).add(abbr)

    return schedule_by_date, box_by_date, landings, rosters


# ---------------------------------------------------------------------------
# Apply + verify
# ---------------------------------------------------------------------------


def post_batch(rows):
    """One column-limited upsert via raw PostgREST.

    postgrest-py's ``upsert()`` has no column restriction, and an unrestricted
    merge could null columns we never sent. With ``columns=`` the merge sets
    only those columns - and if some PostgREST version ever ignored it, the
    NOT NULL on ``name`` would fail every batch loudly instead of corrupting
    data. Retries transport errors/429/5xx; any other 4xx re-raises at once,
    because a schema rejection will not fix itself.
    """
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SNAPSHOT_TABLE}"
    params = {"on_conflict": "date,player_id", "columns": ",".join(APPLY_COLUMNS)}
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Prefer": "resolution=merge-duplicates,return=minimal",
        "Content-Type": "application/json",
    }
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = requests.post(url, params=params, headers=headers, json=rows, timeout=60)
        except requests.exceptions.RequestException:
            if attempt == REQUEST_ATTEMPTS - 1:
                raise
            time.sleep(2**attempt)
            continue
        if response.status_code in (200, 201, 204):
            return response
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(2**attempt)
            continue
        raise RuntimeError(f"PostgREST rejected the batch ({response.status_code}): {response.text[:500]}")
    raise RuntimeError(f"Batch failed after {REQUEST_ATTEMPTS} attempts")


def count_rows(filters=()):
    """Exact count with optional filters: ``(column, op, value)`` triples."""

    def run():
        query = SUPABASE_ADMIN_CLIENT.table(SNAPSHOT_TABLE).select("date", count="exact", head=True)
        for column, op, value in filters:
            if op == "is_null":
                query = query.is_(column, None)
            elif op == "not_null":
                query = query.not_.is_(column, None)
            elif op == "gte":
                query = query.gte(column, value)
            else:
                raise ValueError(f"unsupported filter op {op!r}")
        return query.execute().count

    return _retry(run, f"Counting rows with filters {filters}")


def apply_payload(rows, batch_size):
    """Canary first: one row, read it back whole, then bulk.

    The canary proves ``columns=`` really restricts the merge before ~139k
    rows depend on it: it reads the row back and checks the key columns we did
    *not* send (``name``, ``scored``) survived, on a single row, before the
    bulk starts.
    """
    ordered = sorted(rows.values(), key=lambda row: (row["date"], row["player_id"]))
    if not ordered:
        print("[apply] nothing to write")
        return 0

    canary = ordered[0]
    post_batch([canary])
    read_back = _retry(
        lambda: (
            SUPABASE_ADMIN_CLIENT.table(SNAPSHOT_TABLE)
            .select(",".join(APPLY_COLUMNS + ("name", "scored")))
            .eq("date", canary["date"])
            .eq("player_id", canary["player_id"])
            .execute()
            .data
        ),
        "Reading back the canary row",
    )
    if not read_back:
        raise RuntimeError("Canary row not found after upsert; aborting before bulk apply")
    row = read_back[0]
    for column in APPLY_COLUMNS:
        if row.get(column) != canary.get(column):
            raise RuntimeError(
                f"Canary column {column} mismatch after upsert: {row.get(column)!r} != {canary.get(column)!r}"
            )
    if not row.get("name"):
        raise RuntimeError(f"Canary row lost its name column: {row!r}")
    print(
        f"[apply] canary ok: {row['date']} {row['player_id']} {row['team_abbr']}"
        f" name={row['name']!r} scored={row['scored']!r}"
    )

    written = 1
    for start in range(1, len(ordered), batch_size):
        batch = ordered[start : start + batch_size]
        post_batch(batch)
        written += len(batch)
        if (written // batch_size) % 10 == 0:
            print(f"[apply] {written}/{len(ordered)} rows upserted")

    print(f"[apply] upserted {written} rows")
    return written


def verify_expectations(before, stats):
    """Post-write invariants; returns failures (empty = verified)."""
    failures = []
    after_total = count_rows()
    if after_total != before["total"]:
        failures.append(f"row count changed: {before['total']} -> {after_total}")

    expected_unresolved = sum(stats.unresolved_by_era.values())
    null_abbr = count_rows([("team_abbr", "is_null", None)])
    if null_abbr != expected_unresolved:
        failures.append(f"null team_abbr = {null_abbr}, expected exactly the {expected_unresolved} unresolved rows")

    null_name = count_rows([("team_name", "is_null", None)])
    if null_name > expected_unresolved:
        failures.append(f"null team_name = {null_name}, more than the {expected_unresolved} unresolved rows")

    graded = count_rows([("scored", "not_null", None)])
    if graded != before["graded"]:
        failures.append(f"graded count changed: {before['graded']} -> {graded}")

    era3_named = count_rows([("date", "gte", "2026-10-02"), ("team_name", "not_null", None)])
    if era3_named < before["era3_named"]:
        failures.append(f"era 3 rows with team_name dropped: {before['era3_named']} -> {era3_named}")

    return failures


# ---------------------------------------------------------------------------
# Report / CLI
# ---------------------------------------------------------------------------


def report(stats, failures, samples, applied):
    verb = "Applied" if applied else "Would apply"
    print(f"\n{'=' * 70}\n{verb} identity columns into {SNAPSHOT_TABLE}\n{'=' * 70}")
    for key, value in stats.as_dict().items():
        print(f"  {key:>24}: {value}")
    if stats.unresolved_detail:
        shown = stats.unresolved_detail[:12]
        print(f"\nUnresolved rows (first {len(shown)} of {len(stats.unresolved_detail)}):")
        for detail in shown:
            print(f"  {detail}")
    if samples:
        print("\nSample rows:")
        for row in samples:
            print(f"  {row}")
    if failures:
        print("\nGATES FAILED:")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("\nAll gates passed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--scrape", action="store_true", help="Fetch NHL schedule/boxscore/landing/roster into the cache and exit."
    )
    parser.add_argument("--apply", action="store_true", help="Upsert after gates pass (default is report-only).")
    parser.add_argument("--date", help="Restrict to one YYYY-MM-DD date (debug).")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"Rows per upsert (default: {BATCH_SIZE}).")
    parser.add_argument(
        "--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"NHL response cache (default: {DEFAULT_CACHE_DIR})."
    )
    args = parser.parse_args(argv)

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        parser.error("MONGODB_URI is not set. Export it (and SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY) first.")

    cache_dir = Path(args.cache_dir)
    client = MongoClient(uri)
    try:
        collection = client[os.environ.get("MONGODB_DATABASE", MONGO_DATABASE)][MONGO_COLLECTION]

        if args.scrape:
            scrape(collection, cache_dir)
            return 0

        dates = [args.date] if args.date else sorted(collection.distinct("date", {"date": {"$type": "string"}}))
        dates_with_abbr = set(collection.distinct("date", {"team_abbr": {"$type": "string"}}))
        box_dates = [d for d in dates if d not in dates_with_abbr]

        schedule_by_date, box_by_date, landings, rosters = load_cache(cache_dir, box_dates)

        stored_names = load_era3_stored_names()
        if args.date:
            stored_names = {key: value for key, value in stored_names.items() if key[0] == args.date}

        stats = BackfillStats()
        rows = build_payloads(
            collection, stored_names, schedule_by_date, box_by_date, landings, rosters, args.date, stats
        )
    finally:
        client.close()

    if stats.unresolved_detail:
        unresolved_path = cache_dir / "unresolved.json"
        unresolved_path.parent.mkdir(parents=True, exist_ok=True)
        unresolved_path.write_text(json.dumps(stats.unresolved_detail, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[build] wrote {len(stats.unresolved_detail)} unresolved rows to {unresolved_path}")

    failures = evaluate_gates(stats)
    report(stats, failures, list(rows.values())[:SAMPLE_SIZE], applied=False)

    if not args.apply:
        return 0
    if failures:
        print("\nRefusing to --apply while gates fail.")
        return 1

    before = {
        "total": count_rows(),
        "graded": count_rows([("scored", "not_null", None)]),
        "era3_named": count_rows([("date", "gte", "2026-10-02"), ("team_name", "not_null", None)]),
    }

    apply_payload(rows, args.batch_size)

    verify_failures = verify_expectations(before, stats)
    if verify_failures:
        print("\nPOST-WRITE VERIFICATION FAILED:")
        for failure in verify_failures:
            print(f"  - {failure}")
        return 1
    print("\nPost-write verification passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
