from unittest.mock import patch

from service import (
    build_goalies_with_team_id,
    denormalize_players_for_db,
    enrich_starting_goalies,
    get_goalie_stats_for_team,
    get_starting_goalies,
    normalize_rotowire_team_abbr,
)


def _rotowire_payload():
    return [
        {
            "gamedate": "5:00 PM",
            "hometeam": "CAR",
            "homelogo": "https://assets.rotowire.com/images/teamlogo/hockey/100CAR.png?v=5",
            "homePlayer": "Brandon Bussi",
            "homePlayerFN": "Brandon",
            "homePlayerLN": "Bussi",
            "homePlayerID": "6635",
            "homePlayerURL": "/hockey/player/brandon-bussi-6635",
            "homeStatus": "Confirmed",
            "visitteam": "FLA",
            "visitlogo": "https://assets.rotowire.com/images/teamlogo/hockey/100FLA.png?v=5",
            "visitPlayer": "Jacob Markstrom",
            "visitPlayerFN": "Jacob",
            "visitPlayerLN": "Markstrom",
            "visitPlayerID": "3008",
            "visitPlayerURL": "/hockey/player/jacob-markstrom-3008",
            "visitStatus": "Expected",
        }
    ]


def _schedule_payload():
    def team(abbr, place):
        return {
            "abbrev": abbr,
            "placeName": {"default": place},
            "commonName": {"default": place},
        }

    return {
        "gameWeek": [
            {
                "games": [
                    {
                        "homeTeam": team("CAR", "Carolina"),
                        "awayTeam": team("FLA", "Florida"),
                    }
                ]
            }
        ]
    }


def _club_stats_payload():
    return {
        "season": "20252026",
        "gameType": 2,
        "skaters": [],
        "goalies": [
            {
                "playerId": 1,
                "firstName": {"default": "Brandon"},
                "lastName": {"default": "Bussi"},
                "gamesPlayed": 39,
                "gamesStarted": 39,
                "wins": 31,
                "losses": 6,
                "overtimeLosses": 2,
                "goalsAgainstAverage": 2.5,
                "savePercentage": 0.905,
                "shutouts": 1,
            }
        ],
    }


def test_normalize_rotowire_team_abbr():
    assert normalize_rotowire_team_abbr("MON") == "MTL"
    assert normalize_rotowire_team_abbr("LAS") == "VGK"
    assert normalize_rotowire_team_abbr("TOR") == "TOR"
    assert normalize_rotowire_team_abbr("mon") == "MTL"
    assert normalize_rotowire_team_abbr("") == ""
    assert normalize_rotowire_team_abbr(None) == ""


@patch("service.exponential_backoff_request")
def test_get_starting_goalies_parses_response(mock_request):
    mock_request.return_value = _rotowire_payload()

    result = get_starting_goalies("2026-09-29")

    assert len(result) == 2
    home = next(s for s in result if s["home"])
    away = next(s for s in result if not s["home"])
    assert home["goalie_name"] == "Brandon Bussi"
    assert home["team_abbr"] == "CAR"
    assert home["status"] == "Confirmed"
    assert home["rotowire_id"] == "6635"
    assert away["goalie_name"] == "Jacob Markstrom"
    assert away["team_abbr"] == "FLA"
    assert away["status"] == "Expected"


@patch("service.exponential_backoff_request")
def test_get_starting_goalies_skips_incomplete_rows(mock_request):
    mock_request.return_value = [{"hometeam": "CAR", "homePlayer": "", "homeStatus": "Confirmed"}]

    assert get_starting_goalies("2026-09-29") == []


@patch("service.exponential_backoff_request", side_effect=Exception("boom"))
def test_get_starting_goalies_handles_failure(mock_request):
    assert get_starting_goalies("2026-09-29") == []


@patch("service.exponential_backoff_request")
def test_get_starting_goalies_rejects_non_list_payload(mock_request):
    mock_request.return_value = {"games": []}

    assert get_starting_goalies("2026-09-29") == []


@patch("service.exponential_backoff_request")
def test_get_goalie_stats_for_team(mock_request):
    mock_request.return_value = _club_stats_payload()

    result = get_goalie_stats_for_team("CAR")

    assert result["brandon bussi"]["record"] == "31-6-2"
    assert result["brandon bussi"]["gaa"] == 2.5
    assert result["brandon bussi"]["save_pct"] == 0.905
    assert result["brandon bussi"]["nhl_id"] == 1


@patch("service.exponential_backoff_request", side_effect=Exception("boom"))
def test_get_goalie_stats_for_team_handles_failure(mock_request):
    assert get_goalie_stats_for_team("CAR") == {}


@patch("service.exponential_backoff_request")
def test_get_goalie_stats_for_team_rejects_malformed_payloads(mock_request):
    mock_request.return_value = ["not", "a", "dict"]
    assert get_goalie_stats_for_team("CAR") == {}

    mock_request.return_value = {"goalies": None}
    assert get_goalie_stats_for_team("CAR") == {}

    mock_request.return_value = {"goalies": ["not-a-dict", 42, None]}
    assert get_goalie_stats_for_team("CAR") == {}


@patch("service.exponential_backoff_request")
def test_get_goalie_stats_for_team_uses_now_endpoint_only(mock_request):
    """Stats are current-season only; a prior season must never be backfilled in."""
    mock_request.return_value = _club_stats_payload()

    result = get_goalie_stats_for_team("CAR")

    assert result["brandon bussi"]["record"] == "31-6-2"
    assert mock_request.call_count == 1
    assert mock_request.call_args[0][0].endswith("/club-stats/CAR/now")


@patch("service.exponential_backoff_request")
def test_get_goalie_stats_for_team_empty_preseason(mock_request):
    """Before a season starts /now has no goalies; that is expected, not an error."""
    mock_request.return_value = {"season": "20262027", "gameType": 2, "skaters": [], "goalies": []}

    assert get_goalie_stats_for_team("CAR") == {}
    assert mock_request.call_count == 1


@patch("service.get_goalie_stats_for_team")
@patch("service.get_starting_goalies")
def test_enrich_starting_goalies_fetches_once_per_team(mock_starters, mock_stats):
    mock_starters.return_value = [
        {"team_abbr": "CAR", "goalie_name": "Brandon Bussi", "status": "Confirmed"},
        {"team_abbr": "FLA", "goalie_name": "Jacob Markstrom", "status": "Confirmed"},
    ]
    mock_stats.side_effect = lambda team: {"brandon bussi": {"record": "31-6-2"}} if team == "CAR" else {}

    result = enrich_starting_goalies("2026-09-29")

    assert mock_stats.call_count == 2
    assert result[0]["record"] == "31-6-2"
    assert result[1]["record"] is None


@patch("service.get_goalie_stats_for_team")
@patch("service.get_starting_goalies")
def test_enrich_starting_goalies_warns_on_name_miss(mock_starters, mock_stats, caplog):
    mock_starters.return_value = [
        {"team_abbr": "CAR", "goalie_name": "Brandon Bussie", "status": "Expected"},
    ]
    mock_stats.return_value = {"brandon bussi": {"record": "31-6-2"}}

    with caplog.at_level("WARNING"):
        result = enrich_starting_goalies("2026-09-29")

    assert result[0]["gaa"] is None
    assert "Brandon Bussie" in caplog.text


def test_build_goalies_with_team_id_resolves_abbr():
    teams = [
        {"team_id": 1, "team_abbr": "CAR", "opponent_id": 2},
        {"team_id": 2, "team_abbr": "FLA", "opponent_id": 1},
    ]
    starters = [
        {"team_abbr": "CAR", "goalie_name": "Brandon Bussi", "status": "Confirmed"},
        {"team_abbr": "FLA", "goalie_name": "Jacob Markstrom", "status": "Expected"},
    ]

    result = build_goalies_with_team_id(starters, teams)

    assert result[0]["team_id"] == 1
    assert result[1]["team_id"] == 2


def test_build_goalies_with_team_id_skips_unknown_team():
    teams = [{"team_id": 1, "team_abbr": "CAR", "opponent_id": 2}]
    starters = [{"team_abbr": "FLA", "goalie_name": "Jacob Markstrom", "status": "Expected"}]

    result = build_goalies_with_team_id(starters, teams)

    assert result[0].get("team_id") is None


def test_denormalize_players_for_db_joins_teams_and_goalies():
    players = [{"name": "Skater One", "team_id": 1}]
    teams = [
        {
            "team_id": 1,
            "team_name": "Carolina",
            "home": True,
            "tgpg": 3.0,
            "otga": 2.5,
            "otshga": 0.5,
            "opponent_id": 2,
        },
        {
            "team_id": 2,
            "team_name": "Florida",
            "home": False,
            "tgpg": 2.8,
            "otga": 3.0,
            "otshga": 0.4,
            "opponent_id": 1,
        },
    ]
    goalies = [
        {
            "team_id": 2,
            "team_abbr": "FLA",
            "goalie_name": "Jacob Markstrom",
            "status": "Expected",
            "gaa": 3.07,
            "save_pct": 0.883,
            "record": "23-19-1",
            "nhl_id": 2,
            "shutouts": 0,
            "games_played": 45,
        }
    ]

    result = denormalize_players_for_db(players, teams, goalies)

    assert result[0]["tgpg"] == 3.0
    assert result[0]["home"] is True
    assert result[0]["opp_goalie_name"] == "Jacob Markstrom"
    assert result[0]["opp_goalie_team"] == "FLA"
    assert result[0]["opp_goalie_status"] == "EXPECTED"
    assert result[0]["opp_goalie_confirmed"] is False
    # Inputs are not mutated.
    assert "tgpg" not in players[0]
    assert "opp_goalie_name" not in players[0]


def test_denormalize_players_for_db_unknown_opponent():
    players = [{"name": "Skater One", "team_id": 1}]
    teams = [{"team_id": 1, "team_name": "Carolina", "home": True, "opponent_id": 99}]

    result = denormalize_players_for_db(players, teams, [])

    assert result[0]["opp_goalie_name"] is None
    assert result[0]["opp_goalie_status"] == "UNKNOWN"
    assert result[0]["opp_goalie_confirmed"] is False
