from unittest.mock import patch

from service import calculate_season_metrics, resolve_season_id, update_season_metrics
from utility import (
    get_metric_by_id,
    get_season_id,
    get_season_metric_id,
    upload_metrics,
    upload_season_metrics,
)


def test_get_season_id_oct_to_dec():
    assert get_season_id("2025-10-07") == "20252026"
    assert get_season_id("2025-12-31") == "20252026"


def test_get_season_id_jan_to_jul():
    assert get_season_id("2026-01-01") == "20252026"
    assert get_season_id("2025-06-11") == "20242025"


def test_get_season_id_aug_boundary():
    assert get_season_id("2025-08-01") == "20252026"
    assert get_season_id("2025-07-31") == "20242025"


def test_get_season_metric_id():
    assert get_season_metric_id("20252026") == "season_pick_accuracy_20252026"


@patch("utility.exponential_backoff_supabase_request")
def test_get_metric_by_id_success(mock_request):
    mock_request.return_value = [{"value": 33.33, "correct": 1, "total": 3}]
    result = get_metric_by_id("season_pick_accuracy_20252026")
    assert result == {"value": 33.33, "correct": 1, "total": 3}
    mock_request.assert_called_once()


@patch("utility.exponential_backoff_supabase_request")
def test_get_metric_by_id_missing(mock_request):
    mock_request.return_value = []
    assert get_metric_by_id("season_pick_accuracy_20252026") is None


@patch("service.get_season_pick_pct")
def test_calculate_season_metrics_init_new_season(mock_get):
    mock_get.return_value = None
    yesterday = [
        {"player_id": 1, "Scored": 1, "date": "2025-10-08"},
        {"player_id": 2, "Scored": 0, "date": "2025-10-08"},
        {"player_id": 3, "Scored": 1, "date": "2025-10-08"},
    ]
    result = calculate_season_metrics(yesterday, "20252026")
    assert result == {"value": round(2 / 3 * 100, 2), "total": 3, "correct": 2}


@patch("service.get_season_pick_pct")
def test_calculate_season_metrics_cumulative(mock_get):
    mock_get.return_value = {"value": 50.0, "correct": 3, "total": 6}
    yesterday = [
        {"player_id": 1, "Scored": 1, "date": "2025-10-09"},
        {"player_id": 2, "Scored": 0, "date": "2025-10-09"},
        {"player_id": 3, "Scored": 0, "date": "2025-10-09"},
    ]
    result = calculate_season_metrics(yesterday, "20252026")
    assert result["total"] == 9
    assert result["correct"] == 4


@patch("service.get_season_pick_pct")
def test_calculate_season_metrics_wrong_count(mock_get):
    result = calculate_season_metrics([{"player_id": 1}], "20252026")
    assert result == []
    mock_get.assert_not_called()


def test_resolve_season_id_prefers_result_date():
    yesterday = [{"player_id": 1, "date": "2025-10-08"} for _ in range(3)]
    assert resolve_season_id(yesterday) == "20252026"


def test_resolve_season_id_fallback_date():
    assert resolve_season_id([], fallback_date="2025-06-11") == "20242025"


@patch("service.upload_season_metrics")
def test_update_season_metrics_calls_upload(mock_upload):
    metrics = {"value": 50.0, "total": 6, "correct": 3}
    update_season_metrics(metrics, "20252026")
    mock_upload.assert_called_once_with(metrics, "20252026")


@patch("service.upload_season_metrics")
def test_update_season_metrics_skips_empty(mock_upload):
    update_season_metrics([], "20252026")
    mock_upload.assert_not_called()


@patch("service.upload_season_metrics")
def test_update_season_metrics_skips_no_season(mock_upload):
    update_season_metrics({"value": 1.0}, None)
    mock_upload.assert_not_called()


@patch("utility.exponential_backoff_supabase_request")
def test_upload_metrics_scoped_to_lifetime_row(mock_request):
    upload_metrics({"value": 28.49, "correct": 10, "total": 35})
    args, kwargs = mock_request.call_args
    assert kwargs["eq"] == ("id", "current_pick_accuracy")
    assert kwargs["json_data"]["id"] == "current_pick_accuracy"


@patch("utility.exponential_backoff_supabase_request")
def test_upload_season_metrics_scoped_to_season_row(mock_request):
    upload_season_metrics({"value": 0.0, "correct": 0, "total": 0}, "20252026")
    args, kwargs = mock_request.call_args
    assert kwargs["eq"] == ("id", "season_pick_accuracy_20252026")
    assert kwargs["json_data"]["id"] == "season_pick_accuracy_20252026"
