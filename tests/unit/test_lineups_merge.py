"""Unit tests for merging projected lineups into the player list."""

from unittest.mock import patch

from event_handler import handle_get_lineups
from service import merge_lineup_data


def _nhl_games():
    """Trimmed NHL.com payload: Flyers with 4 forward lines, 3 pairs, 2 goalies."""
    return [
        {
            "away": "FLYERS",
            "home": "DEVILS",
            "teams": [
                {
                    "name": "Flyers",
                    "units": [
                        {"label": "F1", "players": ["Owen Tippett", "Trevor Zegras", "Porter Martone"]},
                        {"label": "F2", "players": ["Tyson Foerster", "Christian Dvorak", "Travis Konecny"]},
                        {"label": "F3", "players": ["Alex Bump", "Noah Cates", "Matvei Michkov"]},
                        {"label": "F4", "players": ["Carl Grundstrom", "Sean Couturier", "Noel Acciari"]},
                        {"label": "D1", "players": ["Travis Sanheim", "Rasmus Ristolainen"]},
                        {"label": "D2", "players": ["Cam York", "Jamie Drysdale"]},
                        {"label": "D3", "players": ["Nick Seeler", "Simon Benoit"]},
                        {"label": "G1", "players": ["Joseph Woll"]},
                        {"label": "G2", "players": ["Dan Vladar"]},
                    ],
                    "scratched": ["Garrett Wilson"],
                    "injured": [],
                }
            ],
        }
    ]


def _rotowire_games():
    """Trimmed RotoWire payload: Flyers PP units plus a goalie designation."""
    return [
        {
            "away": "Sabres",
            "home": "Blue Jackets",
            "teams": [
                {
                    "name": "Blue Jackets",
                    "is_visit": False,
                    "starting_goalie": {"name": "Jet Greaves", "rotowire_id": "6602", "status": "Confirmed"},
                    "pp_units": [
                        {
                            "label": "POWER PLAY #1",
                            "players": [
                                {"name": "Adam Fantilli", "position": "C", "rotowire_id": "1"},
                                {"name": "Charlie Coyle", "position": "RD", "rotowire_id": "2"},
                            ],
                        }
                    ],
                    "injuries": [],
                }
            ],
        }
    ]


def test_merge_assigns_forward_line():
    players = [{"name": "Owen Tippett", "team_name": "Flyers"}]

    result = merge_lineup_data(players, _nhl_games(), [])

    assert result[0]["lineup_unit"] == "F1"
    assert result[0]["lineup_position_group"] == "F"
    assert result[0]["lineup_status"] == "PROJECTED"
    assert result[0]["pp_unit"] is None


def test_merge_assigns_defence_pair_as_non_forward_group():
    """D-pairing is parsed but not stored per skater; status stays UNKNOWN."""
    players = [{"name": "Travis Sanheim", "team_name": "Flyers"}]

    result = merge_lineup_data(players, _nhl_games(), [])

    assert result[0]["lineup_unit"] is None
    assert result[0]["lineup_position_group"] == "D"
    assert result[0]["lineup_status"] == "UNKNOWN"


def test_merge_assigns_pp_unit_without_forward_line():
    players = [{"name": "Adam Fantilli", "team_name": "Blue Jackets"}]

    result = merge_lineup_data(players, [], _rotowire_games())

    assert result[0]["pp_unit"] == "POWER PLAY #1"
    assert result[0]["lineup_unit"] is None
    assert result[0]["lineup_status"] == "UNKNOWN"


def test_merge_carries_both_forward_line_and_pp_unit():
    players = [
        {"name": "Adam Fantilli", "team_name": "Blue Jackets"},
    ]
    nhl = [
        {
            "away": "SABRES",
            "home": "BLUE JACKETS",
            "teams": [
                {
                    "name": "Blue Jackets",
                    "units": [{"label": "F1", "players": ["Adam Fantilli", "Kent Johnson", "Johnny Gaudreau"]}],
                    "scratched": [],
                    "injured": [],
                }
            ],
        }
    ]

    result = merge_lineup_data(players, nhl, _rotowire_games())

    assert result[0]["lineup_unit"] == "F1"
    assert result[0]["pp_unit"] == "POWER PLAY #1"


def test_merge_matches_names_despite_hyphen_and_apostrophe():
    """Sources and the player list disagree on punctuation; it must not break a match."""
    players = [
        {"name": "J.T. Miller", "team_name": "Rangers"},
        {"name": "Ryan O'Reilly", "team_name": "Predators"},
    ]
    nhl = [
        {
            "away": "A",
            "home": "B",
            "teams": [
                {
                    "name": "Rangers",
                    "units": [{"label": "F2", "players": ["J. T. Miller", "Pavel Dorofeyev", "Will Cuylle"]}],
                    "scratched": [],
                    "injured": [],
                },
                {
                    "name": "Predators",
                    "units": [{"label": "F1", "players": ["Steven Stamkos", "Ryan O’Reilly", "Alexander Kerfoot"]}],
                    "scratched": [],
                    "injured": [],
                },
            ],
        }
    ]

    result = merge_lineup_data(players, nhl, [])

    assert result[0]["lineup_unit"] == "F2"
    assert result[1]["lineup_unit"] == "F1"


def test_merge_unknown_for_unlisted_player():
    players = [{"name": "Some Scratch", "team_name": "Flyers"}]

    result = merge_lineup_data(players, _nhl_games(), [])

    assert result[0]["lineup_unit"] is None
    assert result[0]["pp_unit"] is None
    assert result[0]["lineup_status"] == "UNKNOWN"


def test_merge_unknown_for_unlisted_team():
    players = [{"name": "Owen Tippett", "team_name": "Bruins"}]

    result = merge_lineup_data(players, _nhl_games(), [])

    assert result[0]["lineup_status"] == "UNKNOWN"


def test_merge_handles_both_sources_empty():
    players = [{"name": "Owen Tippett", "team_name": "Flyers"}]

    result = merge_lineup_data(players, [], [])

    assert result[0]["lineup_status"] == "UNKNOWN"
    assert result[0]["lineup_unit"] is None


def test_merge_empty_players():
    assert merge_lineup_data([], _nhl_games(), _rotowire_games()) == []


def test_merge_tolerates_missing_team_key():
    players = [{"name": "Owen Tippett"}]

    result = merge_lineup_data(players, _nhl_games(), [])

    assert result[0]["lineup_status"] == "UNKNOWN"


def test_merge_tolerates_malformed_source_rows():
    players = [{"name": "Owen Tippett", "team_name": "Flyers"}]
    nhl = [{"away": "A", "home": "B", "teams": [{"name": "", "units": [{"label": "F1", "players": ["X"]}]}]}]
    rotowire = [{"teams": [{"name": "Flyers", "pp_units": [{"label": "PP1", "players": [{}]}]}]}]

    result = merge_lineup_data(players, nhl, rotowire)

    assert result[0]["lineup_status"] == "UNKNOWN"


def test_merge_assigns_goalies_group():
    players = [{"name": "Joseph Woll", "team_name": "Flyers"}]

    result = merge_lineup_data(players, _nhl_games(), [])

    assert result[0]["lineup_position_group"] == "G"
    assert result[0]["lineup_unit"] is None


def test_merge_warns_when_article_lists_player_on_two_lines(caplog):
    """Live case: the NHL.com article listed Elias Pettersson on F1 and F2.

    The lookup is name-keyed so only one assignment survives; the warning is what
    stops that from looking like a clean parse.
    """
    players = [{"name": "Elias Pettersson", "team_name": "Canucks"}]
    nhl = [
        {
            "away": "OILERS",
            "home": "CANUCKS",
            "teams": [
                {
                    "name": "Canucks",
                    "units": [
                        {"label": "F1", "players": ["Elias Pettersson", "Liam Ohgren", "Linus Karlsson"]},
                        {"label": "F2", "players": ["Elias Pettersson", "Marco Rossi", "Brock Boeser"]},
                    ],
                    "scratched": [],
                    "injured": [],
                }
            ],
        }
    ]

    with caplog.at_level("WARNING"):
        result = merge_lineup_data(players, nhl, [])

    assert result[0]["lineup_unit"] == "F1"
    assert "Elias Pettersson" in caplog.text
    assert "F1" in caplog.text and "F2" in caplog.text


@patch("event_handler.get_nhl_com_lineups")
@patch("event_handler.get_rotowire_lineups")
def test_handle_get_lineups_merges(mock_rotowire, mock_nhl):
    mock_nhl.return_value = _nhl_games()
    mock_rotowire.return_value = []

    result = handle_get_lineups({"players": [{"name": "Owen Tippett", "team_name": "Flyers"}]}, None)

    assert result["statusCode"] == 200
    assert result["players"][0]["lineup_unit"] == "F1"


@patch("event_handler.get_nhl_com_lineups", return_value=[])
@patch("event_handler.get_rotowire_lineups", return_value=[])
def test_handle_get_lineups_survives_both_sources_failing(mock_rotowire, mock_nhl):
    """A lineup outage must not fail the pipeline; status UNKNOWN carries the signal."""
    result = handle_get_lineups({"players": [{"name": "Owen Tippett", "team_name": "Flyers"}]}, None)

    assert result["statusCode"] == 200
    assert result["players"][0]["lineup_status"] == "UNKNOWN"


@patch("event_handler.get_nhl_com_lineups")
@patch("event_handler.get_rotowire_lineups")
def test_handle_get_lineups_skips_fetch_when_no_players(mock_rotowire, mock_nhl):
    result = handle_get_lineups({"players": []}, None)

    assert result == {"statusCode": 200, "players": []}
    mock_nhl.assert_not_called()
    mock_rotowire.assert_not_called()
