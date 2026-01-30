from unittest.mock import patch

import pytest

from service import calculate_metrics, get_tims, make_predictions_teams


@patch("service.get_min_max")
@patch("service.make_predictions_rust")
def test_make_predictions_teams_basic(mock_rust, mock_min_max):
    """Test making predictions for a list of players."""
    mock_min_max.return_value = {
        "gpg": {"min": 0.0, "max": 2.0},
        "hgpg": {"min": 0.0, "max": 2.0},
        "five_gpg": {"min": 0.0, "max": 2.0},
        "tgpg": {"min": 0.0, "max": 4.0},
        "otga": {"min": 0.0, "max": 4.0},
        "otshga": {"min": 0.0, "max": 1.12},
        "hppg": {"min": 0.0, "max": 0.314},
    }

    mock_rust.predict.return_value = [0.65, 0.75]

    players = [
        {
            "name": "Player 1",
            "gpg": 0.5,
            "hgpg": 0.6,
            "five_gpg": 0.55,
            "tgpg": 3.0,
            "otga": 2.5,
            "otshga": 0.5,
            "hppg": 0.2,
            "home": True,
        },
        {
            "name": "Player 2",
            "gpg": 0.7,
            "hgpg": 0.8,
            "five_gpg": 0.75,
            "tgpg": 3.5,
            "otga": 2.8,
            "otshga": 0.6,
            "hppg": 0.25,
            "home": False,
        },
    ]

    result = make_predictions_teams(players)

    assert len(result) == 2
    assert result[0]["stat"] == 0.65
    assert result[1]["stat"] == 0.75
    mock_rust.predict.assert_called_once()


def test_get_tims_with_matching_players():
    """Test get_tims adds tims values to matching players."""
    players = [
        {"id": 1, "name": "Player 1"},
        {"id": 2, "name": "Player 2"},
        {"id": 3, "name": "Player 3"},
        {"id": 4, "name": "Player 4"},
    ]

    # Mock the group IDs from Tim Hortons
    with patch("service.get_tims_players") as mock_tims:
        mock_tims.return_value = [
            [1, 5],  # Group 1 (tims=1)
            [2, 6],  # Group 2 (tims=2)
            [3, 7],  # Group 3 (tims=3)
        ]

        result = get_tims(players)

    assert result[0]["tims"] == 1  # Player 1 in group 1
    assert result[1]["tims"] == 2  # Player 2 in group 2
    assert result[2]["tims"] == 3  # Player 3 in group 3
    assert result[3]["tims"] == 0  # Player 4 not in any group


def test_get_tims_no_group_data():
    """Test get_tims when no group data is available."""
    players = [
        {"id": 1, "name": "Player 1"},
        {"id": 2, "name": "Player 2"},
    ]

    with patch("service.get_tims_players") as mock_tims:
        mock_tims.return_value = None

        result = get_tims(players)

    # All players should have tims=0
    assert all(player["tims"] == 0 for player in result)


def test_get_tims_empty_player_list():
    """Test get_tims with empty player list."""
    with patch("service.get_tims_players") as mock_tims:
        mock_tims.return_value = [[1], [2], [3]]

        result = get_tims([])

    assert result == []


@patch("service.get_cur_pick_pct")
def test_calculate_metrics_success(mock_get_pct):
    """Test calculating metrics with correct number of players."""
    mock_get_pct.return_value = {"value": 50.0, "correct": 10, "total": 20}

    yesterday_results = [
        {"player_id": 1, "name": "Player 1", "Scored": 1},
        {"player_id": 2, "name": "Player 2", "Scored": 0},
        {"player_id": 3, "name": "Player 3", "Scored": 1},
    ]

    result = calculate_metrics(yesterday_results)

    assert result["correct"] == 12  # 10 + 2 scored
    assert result["total"] == 23  # 20 + 3 players
    assert result["value"] == pytest.approx((12 / 23) * 100, rel=0.01)


@patch("service.get_cur_pick_pct")
def test_calculate_metrics_all_scored(mock_get_pct):
    """Test when all yesterday's players scored."""
    mock_get_pct.return_value = {"value": 50.0, "correct": 10, "total": 20}

    yesterday_results = [
        {"player_id": 1, "name": "Player 1", "Scored": 1},
        {"player_id": 2, "name": "Player 2", "Scored": 1},
        {"player_id": 3, "name": "Player 3", "Scored": 1},
    ]

    result = calculate_metrics(yesterday_results)

    assert result["correct"] == 13  # 10 + 3 scored
    assert result["total"] == 23


@patch("service.get_cur_pick_pct")
def test_calculate_metrics_none_scored(mock_get_pct):
    """Test when none of yesterday's players scored."""
    mock_get_pct.return_value = {"value": 50.0, "correct": 10, "total": 20}

    yesterday_results = [
        {"player_id": 1, "name": "Player 1", "Scored": 0},
        {"player_id": 2, "name": "Player 2", "Scored": 0},
        {"player_id": 3, "name": "Player 3", "Scored": 0},
    ]

    result = calculate_metrics(yesterday_results)

    assert result["correct"] == 10  # No new correct picks
    assert result["total"] == 23


@patch("service.get_cur_pick_pct")
def test_calculate_metrics_wrong_player_count(mock_get_pct):
    """Test when player count is not 3."""
    yesterday_results = [
        {"player_id": 1, "name": "Player 1", "Scored": 1},
        {"player_id": 2, "name": "Player 2", "Scored": 0},
    ]

    result = calculate_metrics(yesterday_results)

    # Should return empty list when count is wrong
    assert result == []
    mock_get_pct.assert_not_called()


@patch("service.get_cur_pick_pct")
def test_calculate_metrics_no_current_data(mock_get_pct):
    """Test when current pick percentage is unavailable."""
    mock_get_pct.return_value = None

    yesterday_results = [
        {"player_id": 1, "name": "Player 1", "Scored": 1},
        {"player_id": 2, "name": "Player 2", "Scored": 0},
        {"player_id": 3, "name": "Player 3", "Scored": 1},
    ]

    result = calculate_metrics(yesterday_results)

    assert result == {"value": "-", "correct": "-", "total": "-"}
