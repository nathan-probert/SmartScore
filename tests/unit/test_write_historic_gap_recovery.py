"""Regression tests for the 2026-10-05 season-stats gap.

On 2026-10-03 there were no games, so the pipeline's UpdateHistory step was
skipped (empty players went straight to PublishToDb). The 2026-10-02 results
(0/3) were therefore never counted and the season row stayed at 3/12 instead
of 3/15. write_historic_db now returns every newly-resolved date (not just
yesterday) and the pipeline routes empty days through UpdateHistory.
"""

from unittest.mock import patch

from service import calculate_metrics, calculate_season_metrics, write_historic_db

TODAY = "2026-10-05"
YESTERDAY = "2026-10-04"
DAY_BEFORE = "2026-10-02"  # 10-03 had no games / no entries


def _entry(date, player_id, scored):
    return {"player_id": player_id, "name": f"P{player_id}", "date": date, "Scored": scored, "tims": 1}


def _fake_get_date(hour=False, add_days=0, subtract_days=0):
    if subtract_days == 1:
        return YESTERDAY
    return TODAY


def _cloudflare_for(date_entries):
    """Build a get_players_for_date mock resolving every date as scored."""

    def _inner(date):
        return [{"id": e["player_id"], "scored": bool(e["Scored"])} for e in date_entries if e["date"] == date]

    return _inner


@patch("service.update_historical_data")
@patch("service.get_players_for_date")
@patch("service.get_historical_data")
@patch("service.get_date", side_effect=_fake_get_date)
def test_write_historic_db_returns_all_newly_scored_dates(
    mock_date, mock_get_historical, mock_get_players, mock_update
):
    # Both 10-02 and 10-04 are unscored at the start of the 10-05 run
    # (10-02 was missed because the 10-03 no-game day skipped UpdateHistory).
    historic = [
        _entry(DAY_BEFORE, 1, None),
        _entry(DAY_BEFORE, 2, None),
        _entry(DAY_BEFORE, 3, None),
        _entry(YESTERDAY, 4, None),
        _entry(YESTERDAY, 5, None),
        _entry(YESTERDAY, 6, None),
    ]
    resolved = [
        _entry(DAY_BEFORE, 1, 0),
        _entry(DAY_BEFORE, 2, 0),
        _entry(DAY_BEFORE, 3, 0),
        _entry(YESTERDAY, 4, 0),
        _entry(YESTERDAY, 5, 0),
        _entry(YESTERDAY, 6, 1),
    ]
    mock_get_historical.return_value = historic
    mock_get_players.side_effect = _cloudflare_for(resolved)

    result = write_historic_db([])  # no-game-today shape also works; picks only add today

    assert len(result) == 6
    assert sum(1 for e in result if e["Scored"] == 1) == 1


@patch("service.update_historical_data")
@patch("service.get_players_for_date", return_value=[])
@patch("service.get_historical_data")
@patch("service.get_date", side_effect=_fake_get_date)
def test_write_historic_db_retry_same_day_returns_empty(mock_date, mock_get_historical, mock_get_players, mock_update):
    # Today already saved -> a retry must not recount yesterday.
    historic = [
        _entry(YESTERDAY, 4, 1),
        _entry(YESTERDAY, 5, 0),
        _entry(YESTERDAY, 6, 0),
        _entry(TODAY, 7, None),
        _entry(TODAY, 8, None),
        _entry(TODAY, 9, None),
    ]
    mock_get_historical.return_value = historic

    assert write_historic_db([]) == []
    mock_get_players.assert_not_called()


@patch("service.update_historical_data")
@patch("service.get_players_for_date")
@patch("service.get_historical_data")
@patch("service.get_date", side_effect=_fake_get_date)
def test_write_historic_db_skips_incomplete_dates(mock_date, mock_get_historical, mock_get_players, mock_update):
    # A date with only 2 entries (or still-None Scored) is left for later.
    historic = [
        _entry(DAY_BEFORE, 1, None),
        _entry(DAY_BEFORE, 2, None),  # only 2 entries -> incomplete slate
        _entry(YESTERDAY, 4, None),
        _entry(YESTERDAY, 5, None),
        _entry(YESTERDAY, 6, None),
    ]
    resolved_yesterday = [_entry(YESTERDAY, 4, 0), _entry(YESTERDAY, 5, 0), _entry(YESTERDAY, 6, 1)]
    mock_get_historical.return_value = historic
    mock_get_players.side_effect = _cloudflare_for(resolved_yesterday)

    result = write_historic_db([])

    assert {e["date"] for e in result} == {YESTERDAY}
    assert len(result) == 3


@patch("service.get_cur_pick_pct")
def test_calculate_metrics_multi_day_batch(mock_get):
    mock_get.return_value = {"value": 25.0, "correct": 3, "total": 12}
    batch = [_entry(YESTERDAY, i, 1 if i == 6 else 0) for i in range(4, 7)] + [
        _entry(DAY_BEFORE, i, 0) for i in range(1, 4)
    ]
    result = calculate_metrics(batch)
    assert result["total"] == 18  # 12 + 6
    assert result["correct"] == 4  # 3 + 1


@patch("service.get_season_pick_pct")
def test_calculate_season_metrics_multi_day_batch(mock_get):
    mock_get.return_value = {"value": 25.0, "correct": 3, "total": 12}
    batch = [_entry(YESTERDAY, i, 1 if i == 6 else 0) for i in range(4, 7)] + [
        _entry(DAY_BEFORE, i, 0) for i in range(1, 4)
    ]
    result = calculate_season_metrics(batch, "20262027")
    assert result["total"] == 18
    assert result["correct"] == 4
