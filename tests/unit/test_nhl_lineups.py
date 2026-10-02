"""Unit tests for NHL/RotoWire lineup parsing.

Fixtures are trimmed from real payloads: the NHL.com fixture reproduces the
JSON-LD article shape (including a game header glued onto the end of the
preceding paragraph) and the RotoWire fixture reproduces the ``lineup__*``
markup for one game.
"""

import json

from bs4 import BeautifulSoup

import nhl_lineups
from nhl_lineups import (
    _name_from_slug,
    _parse_nhl_article_body,
    _parse_rotowire_team,
    _repair_mojibake,
    get_nhl_com_lineups,
    get_rotowire_lineups,
    normalize_player_name,
)

# Mirrors the real article: the second game header is appended to the end of the
# first game's status report paragraph rather than starting its own line.
_ARTICLE_LINES = [
    "## **[FANTASY COVERAGE](https://www.nhl.com/fantasy/)**",
    "",
    "## **FLYERS (0-1-0) at DEVILS (0-0-0)**",
    "",
    "### **7 p.m. ET**",
    "",
    "**Flyers projected lineup**",
    "",
    "Owen Tippett -- Trevor Zegras -- Porter Martone",
    "",
    "Tyson Foerster -- Christian Dvorak -- Travis Konecny",
    "",
    "Alex Bump -- Noah Cates -- Matvei Michkov",
    "",
    "Carl Grundstrom -- Sean Couturier -- Noel Acciari",
    "",
    "Travis Sanheim -- Rasmus Ristolainen",
    "",
    "Cam York -- Jamie Drysdale",
    "",
    "Nick Seeler -- Simon Benoit",
    "",
    "Joseph Woll",
    "",
    "Dan Vladar",
    "",
    "***Scratched:** Garrett Wilson, David Jiricek*",
    "",
    "***Injured:** Denver Barkey (lower body), Nikita Grebenkin (upper body)*",
    "",
    "**Status report**",
    "",
    # The next game header is deliberately glued onto this paragraph's end.
    "No morning skate. ... Poirier, a goalie, was acquired off waivers.## **LIGHTNING (0-0-0) at RANGERS (0-1-0)**",
    "",
    "**Lightning projected lineup**",
    "",
    "Brayden Point -- Nikita Kucherov -- Jake Guentzel",
    "",
    "Anthony Cirelli -- Brandon Hagel -- Conor Geekie",
    "",
    "Zemgus Girgensons -- Pontus Holmberg -- Jeffrey Viel",
    "",
    "Gage Goncalves -- Jansen Harkins -- JJ Moser",
    "",
    "Victor Hedman -- John Carlson",
    "",
    "Ryan McDonagh -- Erik Cernak",
    "",
    "Braden Schneider -- Max Crozier",
    "",
    "Andrei Vasilevskiy",
    "",
    "Dennis Hildeby",
    "",
    "**Rangers projected lineup**",
    "",
    "Mika Zibanejad -- Pavel Dorofeyev -- Alexis Lafreniere",
    "",
    "J.T. Miller -- Oliver Bjorkstrand -- Will Cuylle",
    "",
    "Noah Laba -- Eeli Tolvanen -- Tye Kartye",
    "",
    "Matt Rempe -- Vladislav Gavrikov -- Adam Fox",
    "",
    "Ryan Lindgren -- Marcus Pettersson",
    "",
    "Filip Hronek -- Braden Schneider",
    "",
    "Igor Shesterkin",
    "",
    "Dylan Garand",
    "",
    "**Status report**",
    "",
    "Korpisalo has been placed on injured reserve.",
]

_ARTICLE_BODY = "\n".join(_ARTICLE_LINES)


def _nhl_page(article_body):
    """Wrap a body in the escaped ld+json shape the real page serves."""
    article = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Projected lineups, starting goalies for today",
        "datePublished": "2026-10-01T19:54:00Z",
        "articleBody": article_body,
    }
    # The script tag attribute is HTML-escaped in the real document.
    return f'<script type="application/ld&#x2B;json">{json.dumps(article)}</script>'


def _rotowire_game_html():
    """One game of RotoWire lineup markup, goalie + two PP units + injuries."""
    return """
    <div class="lineup__matchup">
      <div class="lineup__inner">
        <a href="/x" class="lineup__mteam is-visit white">Sabres <span class="lineup__wl">(0-0-0)</span></a>
        <a href="/x" class="lineup__mteam is-home white">Blue Jackets <span class="lineup__wl">(0-0-0)</span></a>
      </div>
    </div>
    <div class="lineup__main">
      <ul class="lineup__list is-visit">
        <li class="lineup__player-highlight">
          <div class="lineup__player-highlight-name">
            <a href="/hockey/player/ukko-pekka-luukkonen-5516">U. Luukkonen</a>
          </div>
          <div class="flex-row align-center is-confirmed"><div class="dot is-green"></div>Confirmed</div>
        </li>
        <li class="lineup__title">POWER PLAY #1</li>
        <li class="lineup__player">
          <div class="lineup__pos">C</div>
          <a title="Tage Thompson" href="/hockey/player/tage-thompson-5172">T. Thompson</a>
        </li>
        <li class="lineup__player">
          <div class="lineup__pos">RW</div>
          <a title="Jack Quinn" href="/hockey/player/jack-quinn-6206">Jack Quinn</a>
        </li>
        <li class="lineup__title is-middle">POWER PLAY #2</li>
        <li class="lineup__player">
          <div class="lineup__pos">C</div>
          <a title="Josh Norris" href="/hockey/player/josh-norris-5423">Josh Norris</a>
        </li>
        <li class="lineup__title">INJURIES</li>
        <li class="lineup__player">
          <div class="lineup__pos">D</div>
          <a title="Dante Fabbro" href="/hockey/player/dante-fabbro-6666">D. Fabbro</a>
          <span class="lineup__inj">IR</span>
        </li>
      </ul>
    </div>
    """


# --- NHL.com -----------------------------------------------------------------


def test_parse_labels_forward_defence_and_goalies_by_group_size():
    games = _parse_nhl_article_body(_ARTICLE_BODY)
    flyers = games[0]["teams"][0]
    labels = [unit["label"] for unit in flyers["units"]]

    assert labels == ["F1", "F2", "F3", "F4", "D1", "D2", "D3", "G1", "G2"]
    assert flyers["units"][0]["players"] == ["Owen Tippett", "Trevor Zegras", "Porter Martone"]
    assert flyers["units"][4]["players"] == ["Travis Sanheim", "Rasmus Ristolainen"]
    assert flyers["units"][7]["players"] == ["Joseph Woll"]


def test_parse_segments_games_even_when_header_is_glued_to_previous_paragraph():
    """Regression: game headers are not line-anchored, so \n splitting corrupts."""
    games = _parse_nhl_article_body(_ARTICLE_BODY)

    assert len(games) == 2
    assert (games[0]["away"], games[0]["home"]) == ("FLYERS", "DEVILS")
    assert (games[1]["away"], games[1]["home"]) == ("LIGHTNING", "RANGERS")
    assert (games[0]["away_record"], games[1]["home_record"]) == ("0-1-0", "0-1-0")


def test_parse_keeps_status_report_prose_out_of_units():
    games = _parse_nhl_article_body(_ARTICLE_BODY)
    names = [p for unit in games[0]["teams"][0]["units"] for p in unit["players"]]

    assert not any("waivers" in n or "morning skate" in n for n in names)
    # 4 forward trios + 3 defence pairs + 2 goalies.
    assert len(names) == 20


def test_parse_extracts_scratches_and_injured():
    team = _parse_nhl_article_body(_ARTICLE_BODY)[0]["teams"][0]

    assert team["scratched"] == ["Garrett Wilson", "David Jiricek"]
    assert team["injured"] == ["Denver Barkey (lower body)", "Nikita Grebenkin (upper body)"]


def test_parse_tolerates_team_missing_goalies():
    """Real case: the article omits goalies for some teams; must not raise."""
    body = (
        "## **SABRES (0-0-0) at BLUE JACKETS (0-0-0)**\n\n"
        "**Blue Jackets projected lineup**\n\n"
        "Zach Werenski -- Denton Mateychuk -- Adam Fantilli\n\n"
        "Sean Monahan -- Kent Johnson -- Johnny Gaudreau\n\n"
        "Carson Soucy -- Emil Andrae\n\n"
        "Jake Christianen -- Erik Gudbranson"
    )
    games = _parse_nhl_article_body(body)
    labels = [u["label"] for u in games[0]["teams"][0]["units"]]

    assert labels == ["F1", "F2", "D1", "D2"]


def test_get_nhl_com_lineups_parses_embedded_json(monkeypatch):
    monkeypatch.setattr("nhl_lineups._fetch_text", lambda url, **kw: _nhl_page(_ARTICLE_BODY))

    games = get_nhl_com_lineups()

    assert len(games) == 2
    assert games[0]["teams"][0]["name"] == "Flyers"
    assert games[1]["teams"][0]["units"][0]["players"][0] == "Brayden Point"


def test_get_nhl_com_lineups_handles_fetch_failure(monkeypatch):
    monkeypatch.setattr("nhl_lineups._fetch_text", lambda url, **kw: None)

    assert get_nhl_com_lineups() == []


def test_get_nhl_com_lineups_handles_missing_article(monkeypatch):
    monkeypatch.setattr("nhl_lineups._fetch_text", lambda url, **kw: "<html><body>nope</body></html>")

    assert get_nhl_com_lineups() == []


def test_get_nhl_com_lineups_skips_unparsable_json_block(monkeypatch):
    page = '<script type="application/ld&#x2B;json">{not json}</script>' + _nhl_page(_ARTICLE_BODY)
    monkeypatch.setattr("nhl_lineups._fetch_text", lambda url, **kw: page)

    assert len(get_nhl_com_lineups()) == 2


# --- RotoWire ----------------------------------------------------------------


def test_parse_rotowire_team_reads_goalie_pp_units_and_injuries():
    soup = BeautifulSoup(_rotowire_game_html(), "html.parser")
    main = soup.find("div", class_="lineup__main")
    team = _parse_rotowire_team(main, "is-visit", "Sabres")

    assert team["is_visit"] is True
    assert team["starting_goalie"]["rotowire_id"] == "5516"
    assert team["starting_goalie"]["status"] == "Confirmed"
    assert [u["label"] for u in team["pp_units"]] == ["POWER PLAY #1", "POWER PLAY #2"]
    assert team["pp_units"][0]["players"][0] == {
        "name": "Tage Thompson",
        "position": "C",
        "rotowire_id": "5172",
    }
    assert [i["name"] for i in team["injuries"]] == ["Dante Fabbro"]
    assert team["injuries"][0]["injury_status"] == "IR"


def test_get_rotowire_lineups_parses_matchup(monkeypatch):
    class Response:
        text = _rotowire_game_html()

        def raise_for_status(self):
            return None

    monkeypatch.setattr("nhl_lineups.requests.get", lambda *a, **kw: Response())

    games = get_rotowire_lineups()

    assert len(games) == 1
    assert (games[0]["away"], games[0]["home"]) == ("Sabres", "Blue Jackets")


def test_get_rotowire_lineups_handles_fetch_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise nhl_lineups.requests.RequestException("down")

    monkeypatch.setattr("nhl_lineups.requests.get", boom)

    assert get_rotowire_lineups() == []


def test_get_rotowire_lineups_skips_matchup_without_two_sides(monkeypatch):
    class Response:
        text = '<div class="lineup__matchup"></div><div class="lineup__main"></div>'

        def raise_for_status(self):
            return None

    monkeypatch.setattr("nhl_lineups.requests.get", lambda *a, **kw: Response())

    assert get_rotowire_lineups() == []


# --- helpers -----------------------------------------------------------------


def test_name_from_slug_drops_trailing_id():
    assert _name_from_slug("tage-thompson-5172") == "Tage Thompson"
    assert _name_from_slug("mason-marchment-8448") == "Mason Marchment"


def test_normalize_player_name_folds_hyphens_apostrophes_and_accents():
    assert normalize_player_name("Ukko-Pekka Luukkonen") == normalize_player_name("Ukko Pekka Luukkonen")
    assert normalize_player_name("Ryan O'Reilly") == normalize_player_name("Ryan O’Reilly")
    assert normalize_player_name("J.T. Miller") == normalize_player_name("J T Miller")
    assert normalize_player_name("") == ""


def test_normalize_player_name_repairs_double_encoded_apostrophe():
    """The live article encodes O’Reilly as UTF-8 bytes read back as latin-1.

    Transliterating that directly yields "OaReilly", which can never match a real
    "O'Reilly" — this is a real join-breaking bug, not a hypothetical one.
    """
    # U+2019 encoded as UTF-8 then decoded as latin-1, exactly as the live article has it.
    mojibake = "Ryan O\u00e2\u0080\u0099Reilly"

    assert normalize_player_name(mojibake) == normalize_player_name("Ryan O'Reilly")


def test_normalize_player_name_repairs_accents_left_intact():
    assert normalize_player_name("Zdeněk Čermák") == "zdenekcermak"


def test_repair_mojibake_leaves_clean_text_alone():
    assert _repair_mojibake("Ryan O'Reilly") == "Ryan O'Reilly"
    assert _repair_mojibake("") == ""


def test_repair_mojibake_survives_unencodable_text():
    """Text that is not latin-1 mojibake must come back unchanged, not raise."""
    assert _repair_mojibake("John �|NAME") == "John �|NAME"
