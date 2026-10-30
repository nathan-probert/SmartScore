#!/usr/bin/env python
"""Snapshot pre-game projected lineups into Player-Snapshots-{ENV}.

WHY THIS EXISTS
---------------
The live pipeline only annotates players with projected lineups on a date's
``first_run`` (see README "Known gap"), so whatever rows land depend on that
single sunrise scrape, and a failed match is archived as ``UNKNOWN`` with null
units forever. This script is a standalone, schedulable collector: it reads
the two public sources the pipeline reads (NHL.com's daily lineup-projections
article and RotoWire's power-play page), resolves every player to an NHL id
via the official roster feed, matches the sources onto that date's NHL
schedule, and persists the strongest available pre-game unit signal per
skater.

UNIT PROVENANCE (official vs proxy) - never fake official fidelity
-----------------------------------------------------------------
Two tiers of unit signal, flagged per row through ``lineup_status``:

* ``PROJECTED``      - every unit value on the row comes from a *published
  pre-game source*: the NHL.com article (F1-F4 lines, D1-D3 pairs) and
  RotoWire's PP units. RotoWire's page is undated; on days the dated
  article confirms the same slate (article ``datePublished`` == game date)
  both sources contribute. On days the article lags (it happens: at 11:30
  ET on 2026-10-22 it was still carrying the previous slate) the article's
  units are refused, and only the RotoWire slate is collected - every
  matchup is date-anchored by being matched onto that day's NHL schedule,
  and trailing previous-slate games fail the match and are skipped, which
  is exactly the filter the live pipeline itself relies on.
* ``PROJECTED_PROXY`` - at least one unit value on the row is a *proxy*: the
  player's strongest strictly-prior deployment, taken from the freshest
  earlier archive row carrying a unit, falling back to the local
  shift-chart reconstructions (``player_lineups`` in data/raw_nhl.sqlite,
  complete through the end of the last crawled season - ACTUAL F1-F4/D1-D3
  and PP1/PP2). Position-guarded (a prior D1 unit is never attached to a
  roster forward). Proxy fidelity never launders into the official label:
  a stored row already labelled ``PROJECTED`` accepts *official* fills
  only, never proxy values.

WHAT IT WRITES
--------------
One snapshot row per (date, player_id) with exactly these columns:
``date, player_id, name, team_name, home, lineup_unit, lineup_position_group,
pp_unit, lineup_status``. Everything else (gpg, hgpg, opp_goalie_*,
injury_*, scored, ...) is deliberately absent from every payload - asserted
before every write - because any column present in a batch is included in
PostgREST's DO UPDATE SET on conflict and would reset stats on stored rows.

Write paths (both idempotent against the ``(date, player_id)`` primary key):

* ``append`` - insert rows for pairs that do not exist yet via
  ``ignore_duplicates`` (PostgREST ``resolution=ignore-duplicates``): an
  already-stored row is never touched, so this path is insert-only by
  database semantics, not just by convention.
* ``refresh`` - fill *blank* lineup fields on stored rows only, replacing a
  source-failure ``UNKNOWN`` stamp with this run's better data and
  normalising the verbatim "POWER PLAY #N" label to PP1/PP2 when a row is
  patched anyway. Guards: non-null stored values are never overwritten;
  ``CONFIRMED``/``ACTUAL`` rows - the game's ground truth - are excluded in
  the group build AND by a PostgREST ``not.in`` filter on the UPDATE itself
  (a row that flips to ACTUAL between read and PATCH cannot be clobbered);
  and a pre-game timestamp guard refuses to patch any player whose
  scheduled game start has passed, or whose team cannot be placed on the
  date's schedule, unless ``--force`` waives after hand verification.

SAFETY GATES
------------
* Official-tier writes require the article's ``datePublished`` to fall on
  the requested game date (ET); ``--force`` waives after hand verification.
* Proxy history is always read strictly BEFORE the target date.
* Every append payload is asserted to carry no stats/grade column, and the
  refresh payload is hard-limited to the four lineup columns.
* ``--env prod`` must be passed explicitly to touch Player-Snapshots-prod.
* ``--limit`` caps inserted rows for a first careful rollout.
* ``--dry-run`` prints the plan and writes nothing.

Usage::

    .venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env dev --dry-run
    .venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env dev
    .venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env prod
    # crontab (10:30 / 13:00 / 16:00 ET) - see
    # smartscore/scripts/backtrack/PROJECTED_LINEUP_RUNBOOK.md
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Reused from the live product: the throttle/cache-disciplined HTTP helper,
# plus the name-normaliser every join here needs (mojibake, accents,
# "A.J." vs "AJ").
from nhl_client import BASE_URL, _get_json  # noqa: E402

from nhl_lineups import normalize_player_name  # noqa: E402

ET = ZoneInfo("America/Toronto")
NHL_LINEUP_ARTICLE_URL = "https://www.nhl.com/news/nhl-lineup-projections-2026-27-season"

# Exact column set sent to Supabase on append. NEVER add a stats column here:
# the batch is aimed at the same PK rows the pipeline wrote and any column
# present in the batch is included in PostgREST's DO UPDATE SET on conflict.
# FORBIDDEN_COLUMNS is asserted absent from every payload before any write.
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
FORBIDDEN_COLUMNS = frozenset(
    {
        "scored",
        "gpg",
        "hgpg",
        "five_gpg",
        "hppg",
        "tgpg",
        "otga",
        "otshga",
        "injury_status",
        "injury_desc",
        "tims",
        "lineup_unit_source",
        "opp_goalie_name",
        "opp_goalie_team",
        "opp_goalie_status",
        "opp_goalie_confirmed",
        "opp_goalie_nhl_id",
        "opp_goalie_gaa",
        "opp_goalie_save_pct",
        "opp_goalie_record",
        "opp_goalie_shutouts",
        "opp_goalie_games_played",
    }
)
# The exact column set the refresh path may write, no exceptions.
REFRESH_COLUMNS = frozenset({"lineup_unit", "lineup_position_group", "pp_unit", "lineup_status"})

BATCH_SIZE = 200
PAGE_SIZE = 1000

# Statuses holding game ground truth - the refresh path excludes these both
# when building PATCH groups and again inside the UPDATE statement itself.
HARD_KEEP_STATUSES = ("CONFIRMED", "ACTUAL")
OFFICIAL_STATUS = "PROJECTED"
PROXY_STATUS = "PROJECTED_PROXY"

RAW_DB = Path(__file__).resolve().parents[3] / "data" / "raw_nhl.sqlite"

_roster_cache = {}


def et_today():
    return datetime.datetime.now(ET).strftime("%Y-%m-%d")


def _parse_utc(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _start_date_et(start_utc):
    return _parse_utc(start_utc).astimezone(ET).strftime("%Y-%m-%d")


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


def _team_keys(side):
    """All normalised name keys a schedule side can be looked up by.

    Only schedule-carried identity fields (abbreviation, place name, common
    name) are used - the join is always against a specific scheduled team,
    never a free-text place string pulled from a source payload.
    """
    keys = set()
    for field in ("abbrev", "place", "common"):
        value = side.get(field)
        if value:
            keys.add(normalize_player_name(value))
    return keys


def _match_team(name, side_a, side_b):
    """Which of two schedule teams does a source team name belong to?"""
    if not name:
        return None
    key = normalize_player_name(name)
    for side in (side_a, side_b):
        if not side or not side.get("abbrev"):
            continue
        if key and key in _team_keys(side):
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
    """``{normalised_name: (player_id, display_name, position_group)}`` for a
    team's current roster; the group (F/D/G) comes from the official roster
    feed's own grouping and guards proxy units against wrong-side assignment."""
    if abbrev in _roster_cache:
        return _roster_cache[abbrev]
    try:
        payload = _get_json(f"{BASE_URL}/roster/{abbrev}/current", delay_seconds)
    except requests.HTTPError:
        payload = {}
    roster = {}
    for group, label in (("forwards", "F"), ("defensemen", "D"), ("goalies", "G")):
        for player in payload.get(group) or []:
            player_id = player.get("id")
            first = (player.get("firstName") or {}).get("default") or ""
            last = (player.get("lastName") or {}).get("default") or ""
            display = f"{first} {last}".strip()
            if player_id and display:
                roster[normalize_player_name(display)] = (int(player_id), display, label)
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


def _canonical_stored_pp(label):
    """Normalise a stored verbatim PP label ("POWER PLAY #1") to PP1/PP2.

    The live merge persisted RotoWire's label verbatim while the model's
    vocabulary is PP1/PP2. Same fact, canonical form; applied only when the
    row is being patched anyway, so untouched dates are never churned.
    """
    return _pp_label(label) or label


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
    from nhl_lineups import _fetch_text  # noqa:PLC0415 - local like its callers

    document = _fetch_text(NHL_LINEUP_ARTICLE_URL)
    if not document:
        return None

    import html as _html  # noqa:PLC0415 - mirrored from get_nhl_com_lineups

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


def build_rows(date_str, delay_seconds=0.5, include_article=True):
    """Collect every official published-source skater row for one game date.

    Rows share no (date, player_id) pair (the first source's field wins on
    conflict, so the article's forward line stays the row and RotoWire only
    fills pp_unit). ``include_article=False`` (a stale-article day) collects
    the RotoWire slate only. ``meta`` carries, per (date, player_id) key,
    the team abbreviation / scheduled start / roster position needed by the
    pre-game guard and proxy checks; the meta dict is never sent to Supabase.
    """
    games = schedule_games(date_str, delay_seconds=delay_seconds)
    if not games:
        print(f"{date_str}: no NHL games scheduled")
        return {}, {}, ["no-scheduled-games"]

    start_by_key = {}
    for game in games:
        for side in (game["away"], game["home"]):
            for key in _team_keys(side):
                start_by_key[key] = game["start_utc"]

    from nhl_lineups import get_nhl_com_lineups, get_rotowire_lineups  # noqa:PLC0415

    sources = []
    if include_article:
        sources.append((get_nhl_com_lineups(), _article_team_entries, "article"))
    sources.append((get_rotowire_lineups(), _rotowire_team_entries, "rotowire"))
    if not any(games for games, _, _ in sources):
        return {}, {}, ["no lineup source returned any games"]

    rows = {}
    meta = {}
    skipped = []
    for source_games, team_fn, _tier in sources:
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
                    player_id, display, pos_group = resolved
                    key = (game_date, player_id)
                    meta[key] = {
                        "team_abbrev": target["abbrev"],
                        "start_utc": matchup["game"]["start_utc"],
                        "roster_group": pos_group,
                    }
                    row = rows.get(key)
                    home_flag = matchup["game"]["home"]["abbrev"] == target["abbrev"]
                    if row is None:
                        rows[key] = {
                            "date": game_date,
                            "player_id": player_id,
                            "name": display,
                            "team_name": target["place"],
                            "home": home_flag,
                            "lineup_unit": unit,
                            "lineup_position_group": group,
                            "pp_unit": pp,
                            "lineup_status": OFFICIAL_STATUS,
                        }
                        continue
                    # Merge the other source's contribution: article units win
                    # where present, RotoWire fills pp-only players.
                    if unit and not row["lineup_unit"]:
                        row["lineup_unit"], row["lineup_position_group"] = unit, group
                    if pp and not row["pp_unit"]:
                        row["pp_unit"] = pp

    return rows, meta, skipped


def _prior_from_archive(supabase, table, date_str, player_ids):
    """Freshest strictly-prior archive row per player carrying a unit.

    The proxy source of first resort: earlier projection rows for the same
    players, read with ``date < target`` only.
    """
    prior = {}
    ids = sorted({int(p) for p in player_ids})
    if not ids:
        return prior
    client = supabase.table(table)
    offset = 0
    while True:
        response = (
            client.select("date,player_id,lineup_unit,lineup_position_group,pp_unit")
            .in_("player_id", ids)
            .lt("date", date_str)
            .or_("lineup_unit.not.is.null,pp_unit.not.is.null")
            .order("date", desc=True)
            .order("player_id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = response.data or []
        for row in batch:
            pid = int(row["player_id"])
            entry = prior.setdefault(
                pid,
                {
                    "lineup_unit": None,
                    "lineup_position_group": None,
                    "pp_unit": None,
                    "lineup_date": None,
                    "pp_date": None,
                    "source": "archive",
                },
            )
            date = row.get("date") or ""
            if row.get("lineup_unit") and (entry["lineup_date"] is None or date > entry["lineup_date"]):
                entry["lineup_unit"] = row["lineup_unit"]
                entry["lineup_position_group"] = row.get("lineup_position_group")
                entry["lineup_date"] = date
            if row.get("pp_unit") and (entry["pp_date"] is None or date > entry["pp_date"]):
                entry["pp_unit"] = _canonical_stored_pp(row["pp_unit"])
                entry["pp_date"] = date
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return prior


def _prior_from_sqlite(date_str, player_ids):
    """Freshest strictly-prior reconstructed unit per player from the local store.

    ``player_lineups`` (see lineup_reconstruct) holds ACTUAL F1-F4/D1-D3 and
    PP1/PP2 reconstructions per (player_id, game_id); ``player_games``
    supplies each game's ET date. Availability ends where the raw crawl
    stopped (2026-06-14 at the time of writing), so this is the deeper but
    staler fallback behind the archive scan.
    """
    prior = {}
    if not RAW_DB.exists() or not player_ids:
        return prior
    ids = sorted({int(p) for p in player_ids})
    con = sqlite3.connect(f"file:{RAW_DB}?mode=ro", uri=True)
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = con.execute(
                (  # noqa: S608
                    "SELECT pg.player_id, pg.game_date, pl.lineup_unit,"
                    " pl.lineup_position_group, pl.pp_unit"
                    " FROM player_games pg"
                    " JOIN player_lineups pl"
                    "  ON pl.game_id = pg.game_id AND pl.player_id = pg.player_id"
                    " WHERE pg.game_date < ? AND pl.lineup_unit IS NOT NULL"
                    " AND pg.player_id IN ({})"
                ).format(placeholders),
                [date_str, *chunk],
            ).fetchall()
            for player_id, game_date, lineup, group, pp in rows:
                entry = prior.setdefault(
                    int(player_id),
                    {
                        "lineup_unit": None,
                        "lineup_position_group": None,
                        "pp_unit": None,
                        "lineup_date": None,
                        "pp_date": None,
                        "source": "sqlite-shiftcharts",
                    },
                )
                if game_date and (entry["lineup_date"] is None or game_date > entry["lineup_date"]):
                    entry["lineup_unit"], entry["lineup_position_group"], entry["lineup_date"] = (
                        lineup,
                        group,
                        game_date,
                    )
                if pp and (entry["pp_date"] is None or game_date > entry["pp_date"]):
                    entry["pp_unit"] = pp
                    entry["pp_date"] = game_date
    finally:
        con.close()
    return prior


def _assert_append_safe(batch):
    """Refuse any append row carrying a column outside the allowlist.

    Defense in depth for the prod write path: the payload must contain only
    identity + lineup columns - no stats, no grade - because a batch column
    would reset the same field on any conflict-consumed stored row.
    """
    allowed = set(PAYLOAD_COLUMNS)
    for row in batch:
        extra = set(row) - allowed
        if extra:
            raise AssertionError(f"append payload carries non-allowlisted columns: {sorted(extra)}")
        forbidden = set(row) & FORBIDDEN_COLUMNS
        if forbidden:
            raise AssertionError(f"append payload carries FORBIDDEN stats/grade columns: {sorted(forbidden)}")


def _existing_rows(supabase, table, date_str):
    """``{player_id: row}`` already stored for the date, paged."""
    client = supabase.table(table)
    existing = {}
    offset = 0
    while True:
        response = (
            client.select("player_id,name,team_name,lineup_status,lineup_unit,lineup_position_group,pp_unit")
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


def _resolve_pair(stored, official, prior, roster_group):
    """Final lineup fields + provenance status for one (date, player) pair.

    fill-only, provenance-disciplined:

    * ``HARD_KEEP_STATUSES`` rows (ACTUAL/CONFIRMED ground truth) -> None:
      never modified.
    * A non-null stored value is never overwritten. A stored verbatim
      "POWER PLAY #N" pp label counts as changed only by canonicalisation
      to PP1/PP2 - the same fact in the model's vocabulary.
    * A stored ``PROJECTED`` row (an official projection) accepts official
      fills only - proxy values must not launder into the official label.
    * Any other stored row (an UNKNOWN stamp, a bare row) and brand-new
      pairs: official fills win per field, remaining blanks fall back to
      the strictly-prior proxy (position-guarded), and any proxy value on
      the final row forces the ``PROJECTED_PROXY`` label.

    Returns ``(lineup_fields_dict, status)`` or ``None`` when nothing changes.
    """
    s_status = (stored or {}).get("lineup_status") or ""
    if s_status in HARD_KEEP_STATUSES:
        return None

    official = official or {}
    prior = prior or {}
    s_unit = (stored or {}).get("lineup_unit") or None
    s_group = (stored or {}).get("lineup_position_group") or None
    s_pp_raw = (stored or {}).get("pp_unit") or None
    s_pp = _canonical_stored_pp(s_pp_raw) if s_pp_raw else None
    official_row = s_status == OFFICIAL_STATUS

    unit, group, pp = s_unit, s_group, s_pp
    used_official = False
    used_proxy = False

    if not unit and official.get("lineup_unit"):
        unit, group, used_official = official["lineup_unit"], official.get("lineup_position_group"), True
    if not pp and official.get("pp_unit"):
        pp, used_official = official["pp_unit"], True

    if not official_row:
        if not unit and prior.get("lineup_unit"):
            prior_group = prior.get("lineup_position_group")
            # Position guard: a prior unit labelled D never dresses a roster
            # forward (or vice versa); an unknown roster side keeps the fill.
            if not roster_group or not prior_group or prior_group[0] == roster_group[0]:
                unit, group, used_proxy = prior["lineup_unit"], prior_group, True
        if not pp and prior.get("pp_unit"):
            pp, used_proxy = prior["pp_unit"], True

    if not official_row and not used_official and not used_proxy:
        # nothing new from either tier - only the verbatim -> canonical pp
        # rewrite can still make the patch worthwhile
        if pp == s_pp_raw:
            return None

    if not (unit or pp):
        return None

    if official_row:
        status = OFFICIAL_STATUS
    elif used_proxy:
        status = PROXY_STATUS
    else:
        status = OFFICIAL_STATUS

    changed = (
        (unit or None) != (s_unit or None)
        or ((group or None) if unit else None) != (s_group or None)
        or (pp or None) != (s_pp_raw or None)
        or status != s_status
    )
    if not changed:
        return None
    fields = {"lineup_unit": unit, "lineup_position_group": group, "pp_unit": pp}
    return fields, status


def _execute_patch_groups(supabase, table, date_str, groups):
    """PATCH exactly the four lineup columns, grouped by identical values.

    The UPDATE carries a ``lineup_status not.in (CONFIRMED, ACTUAL)`` filter
    so a row promoted to the game's ground truth between the read and this
    statement is skipped by the database, not just by our bookkeeping.
    """
    patched = 0
    for (unit, group, pp, status), player_ids in groups.items():
        payload = {
            "lineup_unit": unit,
            "lineup_position_group": group,
            "pp_unit": pp,
            "lineup_status": status,
        }
        if set(payload) != REFRESH_COLUMNS:
            raise AssertionError(f"refresh payload columns drifted: {sorted(payload)}")
        (
            supabase.table(table)
            .update(payload, returning="minimal")
            .eq("date", date_str)
            .in_("player_id", player_ids)
            .not_.in_("lineup_status", list(HARD_KEEP_STATUSES))
            .execute()
        )
        patched += len(player_ids)
    return patched


def run(supabase, env, date_str, modes, limit=None, force=False, dry_run=False, use_proxy=True):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    article_date = article_published_date()
    official_ok = article_date == date_str
    if not official_ok:
        print(
            f"official tier: article covers {article_date}, requested {date_str};"
            " published-source units skipped (the undated RotoWire page is only"
            " believed when the dated article confirms the same slate)"
        )
        if force:
            official_ok = True
            print("  --force given: official tier un-blocked by hand verification")

    rows, meta, skipped = {}, {}, []
    if official_ok:
        rows, meta, skipped = build_rows(date_str)
    else:
        # Article lags the slate: the dated article's units are refused, but
        # the RotoWire slate is collected (rows date-anchored by the schedule
        # match), then the strictly-prior proxy tier fills lineup blanks.
        rows, meta, skipped = build_rows(date_str, include_article=False)
        print("  RotoWire-only tier: article stale, its F/D units refused")
    forwards = sum(1 for row in rows.values() if row["lineup_position_group"] == "F")
    defence = sum(1 for row in rows.values() if row["lineup_position_group"] == "D")
    pp_count = sum(1 for row in rows.values() if row["pp_unit"])
    print(
        f"{date_str}: official sources -> {len(rows)} rows (F {forwards}, D {defence},"
        f" pp non-null {pp_count}); {len(skipped)} item(s) skipped"
    )
    for line in skipped[:15]:
        print(f"  skipped: {line}")
    if len(skipped) > 15:
        print(f"  ... {len(skipped) - 15} more")

    if dry_run:
        for row in list(rows.values())[:5]:
            print("  official sample:", json.dumps(row))
        return 0, "dry-run only (no table reads, no writes)"

    table = f"Player-Snapshots-{env}"
    existing = _existing_rows(supabase, table, date_str)
    print(f"  stored rows for {date_str}: {len(existing)}")

    official_by_pid = {int(row["player_id"]): row for row in rows.values()}

    universe_ids = set(official_by_pid) | set(existing)
    print(f"  unit-freshening universe: {len(universe_ids)} player(s)")

    prior = {}
    if use_proxy:
        prior = _prior_from_archive(supabase, table, date_str, universe_ids)
        for pid, entry in _prior_from_sqlite(date_str, universe_ids).items():
            target = prior.setdefault(
                pid,
                {
                    "lineup_unit": None,
                    "lineup_position_group": None,
                    "pp_unit": None,
                    "lineup_date": None,
                    "pp_date": None,
                    "source": "sqlite-shiftcharts",
                },
            )
            if entry["lineup_unit"] and not target["lineup_unit"]:
                target["lineup_unit"] = entry["lineup_unit"]
                target["lineup_position_group"] = entry["lineup_position_group"]
                target["lineup_date"] = entry["lineup_date"]
                target["source"] = entry["source"]
            if entry["pp_unit"] and not target["pp_unit"]:
                target["pp_unit"] = entry["pp_unit"]
                target["pp_date"] = entry["pp_date"]
        coverage = sum(1 for entry in prior.values() if entry["lineup_unit"] or entry["pp_unit"])
        print(f"  proxy coverage: {coverage} player(s) with a strictly-prior unit")

    # schedule lookups for the pre-game guard on stored-only players
    games = schedule_games(date_str)
    start_by_key = {}
    abbrev_by_place = {}
    for game in games:
        for side in (game["away"], game["home"]):
            for key in _team_keys(side):
                start_by_key[key] = game["start_utc"]
            if side.get("place"):
                abbrev_by_place[normalize_player_name(side["place"])] = side["abbrev"]

    def game_start_for(pid):
        """Scheduled start for a player's team, or None when unplaceable."""
        row = official_by_pid.get(pid)
        if row is not None:
            entry = meta.get((row["date"], int(row["player_id"])))
            if entry:
                return entry.get("start_utc")
        stored = existing.get(pid)
        abbrev = None
        if stored:
            abbrev = abbrev_by_place.get(normalize_player_name(stored.get("team_name") or ""))
        if abbrev:
            return start_by_key.get(normalize_player_name(abbrev))
        return None

    def roster_group_for(pid):
        entry = official_by_pid.get(pid)
        if entry is not None:
            return (meta.get((entry["date"], int(entry["player_id"]))) or {}).get("roster_group")
        stored = existing.get(pid)
        abbrev = None
        if stored:
            abbrev = abbrev_by_place.get(normalize_player_name(stored.get("team_name") or ""))
        if abbrev:
            for value in _roster(abbrev).values():
                if value[0] == pid:
                    return value[2]
        return None

    # ---- resolve every pair in the universe -------------------------------
    new_payloads = []
    patch_groups = defaultdict(list)
    guard_skipped = unknown_start = 0
    for pid in sorted(universe_ids):
        resolved = _resolve_pair(
            existing.get(pid),
            official_by_pid.get(pid),
            prior.get(pid) if use_proxy else None,
            roster_group_for(pid),
        )
        if resolved is None:
            continue
        fields, status = resolved
        stored = existing.get(pid)

        if stored is None:
            official_row = official_by_pid.get(pid)
            if official_row is None:
                continue  # proxy-only pair with no identity: never fabricated
            payload = {
                "date": official_row["date"],
                "player_id": pid,
                "name": official_row["name"],
                "team_name": official_row["team_name"],
                "home": official_row["home"],
                **fields,
                "lineup_status": status,
            }
            new_payloads.append(payload)
        else:
            start_utc = game_start_for(pid)
            if start_utc is None:
                unknown_start += 1
                if not force:
                    continue  # cannot verify the team plays this date: skip
            elif _parse_utc(start_utc) <= now_utc and not force:
                guard_skipped += 1
                continue  # game already underway: never touch deployment data
            patch_groups[(fields["lineup_unit"], fields["lineup_position_group"], fields["pp_unit"], status)].append(
                pid
            )

    appended = refreshed = 0
    if "append" in modes:
        batch = new_payloads[:limit] if limit is not None else new_payloads
        if batch:
            _assert_append_safe(batch)
            for start in range(0, len(batch), BATCH_SIZE):
                (
                    supabase.table(table)
                    .upsert(
                        batch[start : start + BATCH_SIZE],
                        on_conflict="date,player_id",
                        ignore_duplicates=True,
                        returning="minimal",
                    )
                    .execute()
                )
            appended = len(batch)
        print(f"  append: {appended} new row(s) inserted (ignore_duplicates; existing rows untouched)")

    if "refresh" in modes:
        if patch_groups:
            refreshed = _execute_patch_groups(supabase, table, date_str, patch_groups)
        print(
            f"  refresh: {refreshed} stored row(s) filled"
            f" (guard-skipped: {guard_skipped} game-started, {unknown_start} team-unplaceable)"
        )
    elif guard_skipped or unknown_start:
        print(f"  (append-only mode: {guard_skipped + unknown_start} pair(s) would have been guard-skipped)")

    return 0, f"appended {appended}, refreshed {refreshed}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env", default="dev", choices=("dev", "prod"), help="Supabase table suffix (default dev).")
    parser.add_argument("--date", default=None, help="Game date YYYY-MM-DD (default: today, America/Toronto).")
    parser.add_argument("--mode", nargs="+", choices=("append", "refresh"), default=["append", "refresh"])
    parser.add_argument("--limit", type=int, default=None, help="Cap rows appended (safety for a first rollout).")
    parser.add_argument(
        "--force", action="store_true", help="Write even when the article date/query guards do not line up."
    )
    parser.add_argument("--dry-run", action="store_true", help="Collect and report only; no writes, no table reads.")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Skip the strictly-prior proxy tier; persist official published units only.",
    )
    args = parser.parse_args()

    date_str = args.date or et_today()
    if args.date and args.date != et_today() and not args.force:
        print("note: the projected sources are a daily slate; non-today dates need --force and usually fail")

    os.environ["ENV"] = args.env
    from config import SUPABASE_ADMIN_CLIENT  # noqa:PLC0415 - initialised after ENV is final

    code, message = run(
        SUPABASE_ADMIN_CLIENT,
        args.env,
        date_str,
        set(args.mode),
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        use_proxy=not args.no_proxy,
    )
    print(f"{date_str}: {message}")
    return code


if __name__ == "__main__":
    sys.exit(main())
