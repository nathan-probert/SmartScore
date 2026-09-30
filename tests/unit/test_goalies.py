from unittest.mock import patch

from service import (
    build_team_name_map,
    enrich_starting_goalies,
    get_goalie_stats_for_team,
    get_previous_season,
    get_starting_goalies,
    merge_goalie_data,
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


def test_get_previous_season():
    assert get_previous_season("20262027") == "20252026"
    assert get_previous_season("20252026") == "20242025"
    assert get_previous_season("20262028") is None
    assert get_previous_season("20262") is None
    assert get_previous_season("") is None
    assert get_previous_season(None) is None


@patch("service.exponential_backoff_request")
def test_get_goalie_stats_for_team_falls_back_to_previous_season(mock_request):
    """Preseason: /now reports the new season with no games, so stats must come from last season."""
    empty_current = {"season": "20262027", "gameType": 2, "skaters": [], "goalies": []}
    mock_request.side_effect = [empty_current, _club_stats_payload()]

    result = get_goalie_stats_for_team("CAR")

    assert result["brandon bussi"]["record"] == "31-6-2"
    assert result["brandon bussi"]["season"] == "20252026"
    assert mock_request.call_args_list[1][0][0].endswith("/club-stats/CAR/20252026/2")


@patch("service.exponential_backoff_request")
def test_get_goalie_stats_for_team_does_not_fall_back_when_season_present(mock_request):
    mock_request.return_value = _club_stats_payload()

    result = get_goalie_stats_for_team("CAR")

    assert result["brandon bussi"]["season"] == "20252026"
    assert mock_request.call_count == 1


@patch("service.exponential_backoff_request")
def test_get_goalie_stats_for_team_returns_empty_when_fallback_also_empty(mock_request):
    mock_request.side_effect = [
        {"season": "20262027", "goalies": []},
        {"season": "20252026", "goalies": []},
    ]

    assert get_goalie_stats_for_team("CAR") == {}


@patch("service.exponential_backoff_request")
def test_get_goalie_stats_for_team_no_fallback_without_derivable_season(mock_request):
    mock_request.return_value = {"goalies": []}

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
def test_enrich_starting_goalies_propagates_stats_season(mock_starters, mock_stats):
    mock_starters.return_value = [
        {"team_abbr": "CAR", "goalie_name": "Brandon Bussi", "status": "Confirmed"},
    ]
    mock_stats.return_value = {"brandon bussi": {"record": "31-6-2", "season": "20252026"}}

    result = enrich_starting_goalies("2026-09-29")

    assert result[0]["stats_season"] == "20252026"


@patch("service.get_goalie_stats_for_team")
@patch("service.get_starting_goalies")
def test_enrich_starting_goalies_warns_on_name_miss(mock_starters, mock_stats, caplog):
    mock_starters.return_value = [
        {"team_abbr": "CAR", "goalie_name": "Brandon Bussie", "status": "Expected"},
    ]
    mock_stats.return_value = {"brandon bussi": {"record": "31-6-2", "season": "20252026"}}

    with caplog.at_level("WARNING"):
        result = enrich_starting_goalies("2026-09-29")

    assert result[0]["gaa"] is None
    assert result[0]["stats_season"] is None
    assert "Brandon Bussie" in caplog.text


def test_build_team_name_map_uses_common_name_fallback():
    schedule = {
        "gameWeek": [
            {
                "games": [
                    {
                        "homeTeam": {
                            "abbrev": "UTA",
                            "placeName": {"default": " "},
                            "commonName": {"default": "Mammoth"},
                        },
                        "awayTeam": {
                            "abbrev": "TOR",
                            "placeName": {"default": "Toronto"},
                            "commonName": {"default": "Maple Leafs"},
                        },
                    }
                ]
            }
        ]
    }

    assert build_team_name_map(schedule) == {"Mammoth": "UTA", "Toronto": "TOR"}


def test_build_team_name_map_handles_bad_payload():
    assert build_team_name_map({}) == {}
    assert build_team_name_map({"gameWeek": []}) == {}


def test_merge_goalie_data_attaches_opposing_starter():
    players = [
        {"name": "Skater One", "team_name": "Carolina"},
        {"name": "Skater Two", "team_name": "Florida"},
    ]
    starters = [
        {
            "team_abbr": "CAR",
            "goalie_name": "Brandon Bussi",
            "status": "Confirmed",
            "gaa": 2.5,
            "save_pct": 0.905,
            "record": "31-6-2",
            "nhl_id": 1,
            "shutouts": 1,
            "games_played": 39,
        },
        {
            "team_abbr": "FLA",
            "goalie_name": "Jacob Markstrom",
            "status": "Expected",
            "gaa": 3.07,
            "save_pct": 0.883,
            "record": "23-19-1",
            "nhl_id": 2,
            "shutouts": 0,
            "games_played": 45,
        },
    ]

    result = merge_goalie_data(players, starters, _schedule_payload())

    assert result[0]["opp_goalie_name"] == "Jacob Markstrom"
    assert result[0]["opp_goalie_team"] == "FLA"
    assert result[0]["opp_goalie_status"] == "EXPECTED"
    assert result[0]["opp_goalie_confirmed"] is False
    assert result[0]["opp_goalie_gaa"] == 3.07
    assert result[1]["opp_goalie_name"] == "Brandon Bussi"
    assert result[1]["opp_goalie_status"] == "CONFIRMED"
    assert result[1]["opp_goalie_confirmed"] is True
    assert result[1]["opp_goalie_record"] == "31-6-2"


def test_merge_goalie_data_unknown_team():
    players = [{"name": "Skater One", "team_name": "Nowhere"}]

    result = merge_goalie_data(players, [], _schedule_payload())

    assert result[0]["opp_goalie_name"] is None
    assert result[0]["opp_goalie_status"] == "UNKNOWN"
    assert result[0]["opp_goalie_confirmed"] is False


def test_merge_goalie_data_duplicate_team_keeps_last():
    players = [{"name": "Skater One", "team_name": "Florida"}]
    starters = [
        {"team_abbr": "CAR", "goalie_name": "First Goalie", "status": "Expected"},
        {"team_abbr": "CAR", "goalie_name": "Second Goalie", "status": "Confirmed"},
    ]

    result = merge_goalie_data(players, starters, _schedule_payload())

    assert result[0]["opp_goalie_name"] == "Second Goalie"
    assert result[0]["opp_goalie_status"] == "CONFIRMED"


def test_merge_goalie_data_empty_players():
    assert merge_goalie_data([], [], _schedule_payload()) == []
