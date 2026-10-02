"""
NHL lineup retrieval for starting line combinations.

Two complementary sources, both parsed deterministically (no LLM extraction):

- ``get_nhl_com_lineups`` reads the JSON-LD ``NewsArticle`` block embedded in the
  NHL.com daily lineup-projections article and parses the markdown ``articleBody``.
  This is the only free source that publishes forward lines (F1-F4) and defence
  pairs (D1-D3) alongside both goalies, scratches and injuries.
- ``get_rotowire_lineups`` scrapes the lineup section of the RotoWire lineups
  page. RotoWire does not publish forward lines publicly, but it *does* publish
  power play units, which the NHL.com article omits.

Both return the same shape: a list of games, each with per-team units. Nothing is
persisted or merged here; callers keep whichever fields they need.

Expected structure per team, used for the parse-quality log: 4 forward trios
(12 skaters), 3 defence pairs (6 skaters) and 2 goalies. Deviations are logged
rather than raised so a partial parse is visible in logs instead of silent.
"""

import html
import json
import re
import time
from typing import Dict, List, Optional

import requests
from aws_lambda_powertools import Logger
from bs4 import BeautifulSoup, Tag
from unidecode import unidecode

logger = Logger()

NHL_LINEUP_ARTICLE_URL = "https://www.nhl.com/news/nhl-lineup-projections-2026-27-season"
ROTOWIRE_LINEUPS_URL = "https://www.rotowire.com/hockey/nhl-lineups.php"

# RotoWire blocks scrapes with a desktop browser UA; see service.ROTOWIRE_HEADERS.
SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

EXPECTED_FORWARD_LINES = 4
EXPECTED_DEFENCE_PAIRS = 3
EXPECTED_GOALIES = 2

# A lineup unit is identified by how many skaters it contains, so the sizes are
# load-bearing rather than incidental.
UNIT_SIZE_FORWARD = 3
UNIT_SIZE_DEFENCE = 2
UNIT_SIZE_GOALIE = 1

# Guards against a malformed parse silently absorbing an entire article as one unit.
MAX_WORDS_PER_PLAYER = 4

EXPECTED_TEAMS_PER_GAME = 2

# The articleBody is one flat markdown blob: game headers are appended to the end
# of the preceding paragraph rather than starting their own line, so segments must
# be cut by match position instead of splitting on newlines.
_GAME_HEADER = re.compile(
    r"##\s*\*\*([A-Z0-9][A-Z0-9 .'&\-]*?)\s*\(([\d\-]+)\)\s*at\s*"
    r"([A-Z0-9][A-Z0-9 .'&\-]*?)\s*\(([\d\-]+)\)\*\*"
)
_TEAM_LINEUP_HEADER = re.compile(r"\*\*([A-Za-z0-9 ]+?) projected lineup\*\*")
_SCRATCHED = re.compile(r"^\**\s*Scratched:?\**\s*", re.IGNORECASE)
_INJURED = re.compile(r"^\**\s*Injured:?\**\s*", re.IGNORECASE)
_STATUS_REPORT = re.compile(r"^\**\s*Status report\**\s*$", re.IGNORECASE)


def _strip_markdown(text: str) -> str:
    """Reduce a markdown fragment to a bare label or player name."""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*{1,3}", "", text)
    return _repair_mojibake(text).strip()


def _split_people(text: str) -> List[str]:
    """Split a trailing annotation line into individual names."""
    return [name.strip() for name in text.split(",") if name.strip()]


def _name_from_slug(slug: str) -> str:
    """Rebuild a display name from a RotoWire player slug (``tage-thompson-5172``).

    Slugs are lossy: a hyphen inside a real name (``ukko-pekka-luukkonen``) is
    indistinguishable from a word separator. Use :func:`normalize_player_name` when
    matching these against your own player list.
    """
    parts = slug.split("-")
    # Trailing numeric segment is the RotoWire player id, not part of the name.
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(part.capitalize() for part in parts)


# A latin-1 mojibake marker is a UTF-8 lead byte rendered in U+00C0-U+00FF
# (e.g. the 'a-circumflex' of a mangled right single quote) immediately
# followed by a C1 control byte U+0080-U+00BF.
_MOJIBAKE_MARKER = re.compile("[\u00c0-\u00ff][\u0080-\u00bf]")


def _repair_mojibake(text: str) -> str:
    """Undo UTF-8 bytes that were decoded as latin-1 somewhere upstream.

    The NHL.com article body carries apostrophes as ``Oâ€™Reilly`` (the UTF-8
    bytes for ``’`` re-read as latin-1). Transliterating that directly yields
    ``OaReilly``, which can never match a real ``O'Reilly``. Recovering the
    original characters first is what makes the join work.
    """
    if not _MOJIBAKE_MARKER.search(text):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # Not actually latin-1 mojibake; leave the text untouched.
        return text


def normalize_player_name(name: str) -> str:
    """
    Reduce a player name to a join key.

    Downstream joins in this service match players by name, and sources disagree on
    hyphens, apostrophes, accents and character encoding (``Ukko-Pekka Luukkonen``
    vs ``Ukko Pekka Luukkonen``, ``Ryan O'Reilly`` vs ``Ryan O’Reilly`` vs the
    double-encoded ``Ryan Oâ€™Reilly``). Fold all of those away so a cosmetic
    difference cannot break a match.
    """
    folded = _repair_mojibake(name or "")
    folded = unidecode(folded).lower()
    folded = folded.replace("’", "'").replace("`", "'")
    return re.sub(r"[^a-z0-9]+", "", folded)


def _fetch_text(url: str, max_retries: int = 3, base_delay: int = 1) -> Optional[str]:
    """Fetch a page as text with exponential backoff.

    ``exponential_backoff_request`` always json-decodes the body, which cannot work
    for the HTML/JSON-LD sources here, so retry handling is local.

    Returns:
        Response text, or ``None`` if every attempt failed.
    """
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=SCRAPE_HEADERS, timeout=20)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                logger.error(f"Failed to fetch {url} after {max_retries} attempts: {e}")
                return None
            time.sleep(base_delay * (2**attempt))
    return None


def _unit_label(size: int, index: int) -> str:
    """Name a lineup unit from how many skaters it contains."""
    if size == UNIT_SIZE_FORWARD:
        return f"F{index}"
    if size == UNIT_SIZE_DEFENCE:
        return f"D{index}"
    return f"G{index}"


def _log_structure(source: str, games: List[Dict]) -> None:
    """Log how many teams matched the expected lineup shape.

    A silent partial parse is worse than no parse, so surface every deviation.
    """
    total = conformant = 0
    deviations: List[str] = []

    for game in games:
        for team in game["teams"]:
            total += 1
            forwards = [u for u in team["units"] if len(u["players"]) == UNIT_SIZE_FORWARD]
            pairs = [u for u in team["units"] if len(u["players"]) == UNIT_SIZE_DEFENCE]
            goalies = [u for u in team["units"] if len(u["players"]) == UNIT_SIZE_GOALIE]
            n_forwards = sum(len(u["players"]) for u in forwards)
            n_pairs = sum(len(u["players"]) for u in pairs)

            issues = []
            if len(forwards) != EXPECTED_FORWARD_LINES or n_forwards != EXPECTED_FORWARD_LINES * UNIT_SIZE_FORWARD:
                issues.append(f"{len(forwards)} forward lines ({n_forwards} skaters)")
            if len(pairs) != EXPECTED_DEFENCE_PAIRS or n_pairs != EXPECTED_DEFENCE_PAIRS * UNIT_SIZE_DEFENCE:
                issues.append(f"{len(pairs)} defence pairs ({n_pairs} skaters)")
            if len(goalies) != EXPECTED_GOALIES:
                # The article genuinely omits goalies for some teams, so this is
                # informational rather than an error.
                issues.append(f"{len(goalies)} goalies")

            if not issues:
                conformant += 1
            else:
                deviations.append(f"{game['away']}@{game['home']} {team['name']}: " + ", ".join(issues))

    if not total:
        logger.warning(f"{source}: parsed 0 teams")
        return

    logger.info(f"{source}: parsed {total} teams across {len(games)} games, {conformant} matched expected shape")
    for line in deviations:
        logger.warning(f"{source}: unexpected lineup shape -> {line}")


def _parse_nhl_article_body(body: str) -> List[Dict]:
    """Segment the articleBody markdown into games with per-team lineup units."""
    games: List[Dict] = []
    headers = list(_GAME_HEADER.finditer(body))

    for position, header in enumerate(headers):
        start = headers[position + 1].start() if position + 1 < len(headers) else len(body)
        segment = body[header.end() : start]
        teams: List[Dict] = []
        team_headers = list(_TEAM_LINEUP_HEADER.finditer(segment))

        for team_position, team_header in enumerate(team_headers):
            following = (
                team_headers[team_position + 1].start() if team_position + 1 < len(team_headers) else len(segment)
            )
            block = segment[team_header.end() : following]

            units: List[Dict] = []
            scratches: List[str] = []
            injuries: List[str] = []

            for line in block.split("\n"):
                text = _strip_markdown(line)
                if not text:
                    continue
                if _SCRATCHED.match(text):
                    scratches = _split_people(_SCRATCHED.sub("", text))
                    continue
                if _INJURED.match(text):
                    injuries = _split_people(_INJURED.sub("", text))
                    continue
                if _STATUS_REPORT.match(text):
                    break

                players = [p.strip() for p in text.split("--") if p.strip()]
                # Prose is already excluded by the status-report break and the
                # segment bounds, so accept whatever remains. A word-count or
                # capitalisation heuristic would drop legitimate names.
                if players and all(len(p.split()) <= MAX_WORDS_PER_PLAYER for p in players):
                    units.append({"label": _unit_label(len(players), 0), "players": players})

            # Number units per size so labels read F1..F4, D1..D3, G1..G2.
            counters = {UNIT_SIZE_FORWARD: 0, UNIT_SIZE_DEFENCE: 0, UNIT_SIZE_GOALIE: 0}
            for unit in units:
                size = len(unit["players"])
                counters[size] += 1
                unit["label"] = _unit_label(size, counters[size])

            if units:
                teams.append(
                    {
                        "name": team_header.group(1).strip(),
                        "units": units,
                        "scratched": scratches,
                        "injured": injuries,
                    }
                )

        if teams:
            games.append(
                {
                    "away": header.group(1).strip(),
                    "home": header.group(3).strip(),
                    "away_record": header.group(2),
                    "home_record": header.group(4),
                    "teams": teams,
                }
            )

    return games


def get_nhl_com_lineups(url: str = NHL_LINEUP_ARTICLE_URL) -> List[Dict]:
    """
    Fetch and parse projected lineups from the NHL.com daily projections article.

    The lineup text is delivered as JSON-LD (``application/ld+json`` schema.org
    ``NewsArticle``), so this parses a JSON string field rather than scraping HTML.

    Args:
        url: Article URL. Override in tests to point at a fixture.

    Returns:
        List of games; each has ``away``, ``home``, records, and per-team
        ``units`` (forward lines, defence pairs, goalies) plus scratched/injured
        name lists. Returns ``[]`` on any fetch or parse failure.
    """
    try:
        document = _fetch_text(url)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error fetching NHL.com lineups: {e}")
        return []

    if not document:
        return []

    # The ld+json script tag is HTML-escaped in the raw source ('ld&#x2B;json').
    source = html.unescape(document)
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', source, re.S)

    article = None
    for block in blocks:
        try:
            decoded = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and "NewsArticle" in str(decoded.get("@type", "")):
            article = decoded
            break

    if not article or not article.get("articleBody"):
        logger.error("No NHL.com NewsArticle articleBody found in ld+json blocks")
        return []

    body = article["articleBody"]
    if not isinstance(body, str):
        logger.error(f"Unexpected articleBody type: {type(body)}")
        return []

    try:
        games = _parse_nhl_article_body(body)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error parsing NHL.com article body: {e}")
        return []

    _log_structure("nhl.com", games)
    logger.info(f"NHL.com article published {article.get('datePublished')}, parsed {len(games)} games")
    return games


def _rotowire_player(li: Tag) -> Optional[Dict[str, str]]:
    """Pull name, position and RotoWire id out of a ``lineup__player`` list item."""
    link = li.find("a", href=re.compile(r"^/hockey/player/"))
    if link is None:
        return None

    position_div = li.find("div", class_="lineup__pos")
    injury_span = li.find("span", class_="lineup__inj")

    player = {
        # RotoWire prints "T. Thompson" as the link text; the full name is only in
        # the title attribute or recoverable from the slug.
        "name": (link.get("title") or _name_from_slug(link["href"].rsplit("/", 1)[-1])).strip(),
        "position": position_div.get_text(strip=True) if position_div else "",
        "rotowire_id": link["href"].rsplit("-", 1)[-1],
    }
    if injury_span:
        player["injury_status"] = injury_span.get_text(strip=True)
    return player


def get_rotowire_lineups(url: str = ROTOWIRE_LINEUPS_URL) -> List[Dict]:
    """
    Fetch power play units, goalie designations and injuries from RotoWire.

    RotoWire does not publish forward lines publicly, but its lineups page does
    expose power play units, a starter designation and an injury list. Parsed with
    BeautifulSoup against stable ``lineup__*`` class names.

    Args:
        url: Lineups page URL. Override in tests to point at a fixture.

    Returns:
        List of games; each has ``away``, ``home``, and per-team ``pp_units``,
        ``starting_goalie`` and ``injuries``. Returns ``[]`` on failure.
    """
    try:
        response = requests.get(url, headers=SCRAPE_HEADERS, timeout=20)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Error fetching RotoWire lineups: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    games: List[Dict] = []

    for matchup in soup.find_all("div", class_="lineup__matchup"):
        main = matchup.find_next_sibling("div", class_="lineup__main")
        if main is None:
            continue

        sides = matchup.find_all("a", class_=re.compile(r"lineup__mteam"))
        if len(sides) != EXPECTED_TEAMS_PER_GAME:
            continue

        teams = []
        for side in sides:
            # The anchor text is "Sabres (0-0-0)"; the record lives in a nested
            # span, so read the whole anchor and strip the trailing parenthetical.
            team_name = re.sub(r"\s*\(\d[^)]*\)\s*$", "", side.get_text(" ", strip=True)).strip()
            is_visit = "is-visit" in (side.get("class") or [])

            teams.append(_parse_rotowire_team(main, "is-visit" if is_visit else "is-home", team_name))

        away = next((t["name"] for t in teams if t["is_visit"]), None)
        home = next((t["name"] for t in teams if not t["is_visit"]), None)
        if away and home:
            games.append({"away": away, "home": home, "teams": teams})

    pp_total = sum(len(u["players"]) for g in games for t in g["teams"] for u in t["pp_units"])
    logger.info(
        f"RotoWire lineups: {len(games)} games, "
        f"{sum(len(t['pp_units']) for g in games for t in g['teams'])} PP units, {pp_total} PP skaters"
    )
    return games


def _parse_rotowire_team(main: Tag, list_class: str, team_name: str) -> Dict:
    """Walk one team's ``lineup__list`` collecting the goalie, PP units and injuries."""
    listing = main.find("ul", class_=list_class)
    result: Dict = {
        "name": team_name,
        "is_visit": list_class == "is-visit",
        "starting_goalie": None,
        "pp_units": [],
        "injuries": [],
    }
    if listing is None:
        return result

    current_unit: Optional[Dict] = None

    for li in listing.find_all("li", recursive=False):
        classes = li.get("class") or []

        if "lineup__player-highlight" in classes:
            # The goalie anchor carries no title attribute; the slug holds the name.
            link = li.find("a", href=re.compile(r"^/hockey/player/"))
            if link is not None:
                name = _name_from_slug(link["href"].rsplit("/", 1)[-1])
                status_div = li.find("div", class_=re.compile(r"is-(confirmed|expected)"))
                designation = "Unknown"
                if status_div is not None:
                    designation = "Confirmed" if "is-confirmed" in (status_div.get("class") or []) else "Expected"
                result["starting_goalie"] = {
                    "name": name,
                    "rotowire_id": link["href"].rsplit("-", 1)[-1],
                    "status": designation,
                }
            continue

        if "lineup__title" in classes:
            label = li.get_text(strip=True)
            if label.startswith("POWER PLAY"):
                current_unit = {"label": label, "players": []}
                result["pp_units"].append(current_unit)
            else:
                current_unit = None
            continue

        if "lineup__player" not in classes:
            continue

        player = _rotowire_player(li)
        if player is None:
            continue

        if "injury_status" in player:
            result["injuries"].append(player)
        elif current_unit is not None:
            current_unit["players"].append(player)

    return result
