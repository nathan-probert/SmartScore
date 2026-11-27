from unittest.mock import patch

import pytest

from event_handler import (
    handle_check_completed,
    handle_emails,
    handle_get_goalies,
    handle_get_injuries,
    handle_get_tims,
    handle_load_working_set,
    handle_make_predictions,
    handle_parse_teams,
    handle_publish_db,
    handle_save_players,
)


@patch("event_handler.save_player_snapshots")
def test_handle_save_players_archives_with_date(mock_save):
    """Players are archived under the run's date."""
    players = [{"id": 1, "name": "Player 1", "tims": 1}]

    result = handle_save_players({"players": players, "date": "2026-04-15", "status": "first_run"}, {})

    assert result == {"statusCode": 200, "status": "first_run"}
    mock_save.assert_called_once_with(players, date="2026-04-15")


@patch("event_handler.get_date")
@patch("event_handler.load_working_set")
@patch("event_handler.save_player_snapshots")
def test_handle_save_players_loads_the_working_set(mock_save, mock_load, mock_get_date):
    """A pipeline state is lean: the roster is re-read from the Working-* tables."""
    mock_get_date.return_value = "2026-04-16"
    players = [{"id": 1, "name": "Player 1", "team_id": 10}]
    teams = [{"team_id": 10, "opponent_id": 11}]
    mock_load.return_value = (players, teams, [])

    result = handle_save_players({"status": "normal_run"}, {})

    saved = mock_save.call_args[0][0]
    assert saved[0]["id"] == 1
    assert mock_save.call_args.kwargs["date"] == "2026-04-16"
    assert result == {"statusCode": 200, "status": "normal_run"}


@patch("event_handler.save_player_snapshots")
def test_handle_save_players_skips_upload_when_empty(mock_save):
    """An empty player list must not hit Supabase (an empty upsert is a no-op error)."""
    result = handle_save_players({"players": [], "date": "2026-04-15", "status": "first_run"}, {})

    assert result == {"statusCode": 200, "status": "first_run"}
    mock_save.assert_not_called()


@patch("event_handler.check_db_for_date")
def test_handle_check_completed_first_run(mock_check_db):
    """Test when no date exists in database (first run)."""
    mock_check_db.return_value = False

    result = handle_check_completed({}, {})

    assert result == {"statusCode": 200, "status": "first_run"}
    mock_check_db.assert_called_once()


@patch("event_handler.check_db_for_date")
def test_handle_check_completed_normal_run(mock_check_db):
    """Test when date exists in database (normal run)."""
    mock_check_db.return_value = True

    result = handle_check_completed({}, {})

    assert result == {"statusCode": 200, "status": "normal_run"}
    mock_check_db.assert_called_once()


@patch("event_handler.check_db_for_date")
def test_handle_check_completed_last_game(mock_check_db):
    """Test when last_game flag is set."""
    mock_check_db.return_value = True

    result = handle_check_completed({"last_game": True}, {})

    assert result == {"statusCode": 200, "status": "last_run"}


@patch("event_handler.check_db_for_date")
def test_handle_check_completed_never_returns_the_roster(mock_check_db):
    """The result must stay status-only: the roster exceeds the SFN 256KB state limit."""
    mock_check_db.return_value = True

    result = handle_check_completed({}, {})

    assert "players" not in result


@patch("event_handler.get_date")
@patch("event_handler.load_working_set")
def test_handle_load_working_set_returns_a_lean_state(mock_load, mock_get_date):
    """The roster never travels through state: LoadWorkingSet is an existence check."""
    mock_get_date.return_value = "2026-04-15"
    mock_load.return_value = ([{"id": 1, "team_id": 10, "stat": 0.8}], [{"team_id": 10}], [])

    result = handle_load_working_set({"status": "normal_run"}, {})

    assert result == {
        "statusCode": 200,
        "status": "normal_run",
        "date": "2026-04-15",
    }
    mock_load.assert_called_once()


@patch("event_handler.load_working_set")
def test_handle_load_working_set_passes_status_through(mock_load):
    """last_run must survive to CheckSaveHistory's routing."""
    mock_load.return_value = ([{"id": 1}], [{"team_id": 10}], [])

    result = handle_load_working_set({"status": "last_run"}, {})

    assert result["status"] == "last_run"


@patch("event_handler.get_date")
@patch("event_handler.save_working_set")
@patch("event_handler.get_tims")
def test_handle_get_tims_last_run_tolerates_empty_read(mock_get_tims, mock_save_working_set, mock_get_date):
    """The day's last run must not fail when the picks API is locked."""
    players = [{"id": 1, "tims": 0}]
    mock_get_tims.return_value = players

    handle_get_tims({"players": players, "status": "last_run"}, {})

    mock_get_tims.assert_called_once_with(players, allow_empty=True)
    # The enriched set is staged for the DB-write tail instead of round-tripping
    # through Step Functions state (256KB limit).
    mock_save_working_set.assert_called_once_with(players, [], None)


@patch("event_handler.get_date")
@patch("event_handler.save_working_set")
@patch("event_handler.get_tims")
def test_handle_get_tims_normal_run_stays_strict(mock_get_tims, mock_save_working_set, mock_get_date):
    """Regular runs still fail loudly on a bad tims read so they retry."""
    players = [{"id": 1, "tims": 1}]
    mock_get_tims.return_value = players

    result = handle_get_tims({"players": players, "status": "normal_run"}, {})

    mock_get_tims.assert_called_once_with(players, allow_empty=False)
    assert result == {"statusCode": 200, "date": mock_get_date.return_value, "status": "normal_run"}


@patch("event_handler.get_date")
@patch("event_handler.save_working_set")
@patch("event_handler.load_working_set")
@patch("event_handler.get_tims")
def test_handle_get_tims_loads_the_staged_set(mock_get_tims, mock_load, mock_save_working_set, mock_get_date):
    """A pipeline state is lean: MakePredictions staged the set for this read."""
    mock_get_date.return_value = "2026-04-15"
    players = [{"id": 1, "stat": 0.8}]
    teams = [{"team_id": 10}]
    goalies = [{"team_id": 10, "goalie_name": "Starter"}]
    mock_load.return_value = (players, teams, goalies)
    mock_get_tims.side_effect = lambda p, allow_empty=False: [{**player, "tims": 1} for player in p]

    result = handle_get_tims({"status": "normal_run"}, {})

    assert result == {"statusCode": 200, "date": "2026-04-15", "status": "normal_run"}
    mock_save_working_set.assert_called_once_with([{"id": 1, "stat": 0.8, "tims": 1}], teams, goalies)


@patch("event_handler.get_date", return_value="2026-04-15")
@patch("event_handler.publish_public_db")
def test_handle_publish_db_with_players(mock_publish, mock_get_date):
    """Test publishing database with player data."""
    players = [
        {"name": "Player 1", "stat": 0.8},
        {"name": "Player 2", "stat": 0.9},
    ]

    event = {"players": players}
    result = handle_publish_db(event, {})

    # The publish returns the pick date it wrote with: KickMLPicks needs it
    # to relay an explicit date to the MLPicksKick dispatch (race fix).
    assert result == {"statusCode": 200, "date": "2026-04-15"}
    mock_publish.assert_called_once_with(players)


@patch("event_handler.get_date", return_value="2026-04-15")
@patch("event_handler.publish_public_db")
def test_handle_publish_db_empty_players(mock_publish, mock_get_date):
    """Test publishing database with empty player list."""
    event = {"players": []}
    result = handle_publish_db(event, {})

    assert result == {"statusCode": 200, "date": "2026-04-15"}
    mock_publish.assert_called_once_with([])


@patch("event_handler.publish_public_db")
def test_handle_publish_db_last_run_clears_tims(mock_publish):
    """The day's last run publishes every player at tims=0 without a Tims read.

    By the last run every game has started, so the picks API is locked and a
    GetTims read can only fail (the Lambda times out inside the backoff
    retries). The state machine routes last_run straight here, and clearing
    the tims groups is what makes the frontend render "All games have started
    today".
    """
    players = [
        {"name": "Player 1", "id": 1, "stat": 0.8, "tims": 2},
        {"name": "Player 2", "id": 2, "stat": 0.9},
    ]

    handle_publish_db({"players": players, "status": "last_run"}, {})

    published = mock_publish.call_args[0][0]
    assert [player["tims"] for player in published] == [0, 0]


@patch("event_handler.publish_public_db")
def test_handle_publish_db_normal_run_preserves_tims(mock_publish):
    """Only the last run clears tims; other runs publish whatever GetTims set."""
    players = [{"name": "Player 1", "id": 1, "stat": 0.8, "tims": 2}]

    handle_publish_db({"players": players, "status": "normal_run"}, {})

    published = mock_publish.call_args[0][0]
    assert published[0]["tims"] == 2


@patch("event_handler.load_working_set")
@patch("event_handler.publish_public_db")
def test_handle_publish_db_no_players_key(mock_publish, mock_load):
    """A lean pipeline state re-reads the slices GetTims staged."""
    players = [{"id": 1, "stat": 0.8}]
    teams = [{"team_id": 10}]
    mock_load.return_value = (players, teams, [])

    result = handle_publish_db({}, {})

    assert result["statusCode"] == 200
    assert "date" in result  # the explicit-date relay contract holds on every path
    assert mock_publish.call_args[0][0][0]["id"] == 1


@patch("service.fetch_working_set", return_value=([], [], []))
@patch("event_handler.publish_public_db")
def test_handle_publish_db_missing_working_set_fails_loudly(mock_publish, mock_fetch):
    """No event roster and no staged slices must fail, not silently wipe Picks."""
    with pytest.raises(RuntimeError, match="No working set stored"):
        handle_publish_db({}, {})

    mock_publish.assert_not_called()


@patch("event_handler.publish_public_db")
def test_handle_publish_db_carries_the_events_date(mock_publish):
    """A state-carried date wins over the get_date() fallback.

    last_run reaches PublishToDb with the date LoadWorkingSet stamped, so
    the kick gets the date this run actually published for.
    """
    event = {"players": [{"id": 1, "stat": 0.8}], "date": "2026-01-14"}

    result = handle_publish_db(event, {})

    assert result == {"statusCode": 200, "date": "2026-01-14"}


def test_handle_parse_teams_empty_event():
    """Test handling empty event."""
    result = handle_parse_teams([], {})

    assert result == {"players": [], "teams": []}


@patch("event_handler.merge_players_and_teams")
def test_handle_parse_teams_with_data(mock_merge):
    """Test parsing teams with player and team data."""
    mock_merge.return_value = {
        "players": [
            {"name": "Player 1", "team_id": 1},
            {"name": "Player 2", "team_id": 2},
        ],
        "teams": [{"team_id": 1}, {"team_id": 2}],
    }

    event = [
        {
            "team_name": "Team A",
            "team_id": 1,
            "opponent_id": 2,
            "home": True,
            "team_abbr": "TA",
            "season": "20242025",
            "players": [{"name": "Player 1", "id": 100, "team_id": 1}],
        },
        {
            "team_name": "Team B",
            "team_id": 2,
            "opponent_id": 1,
            "home": False,
            "team_abbr": "TB",
            "season": "20242025",
            "players": [{"name": "Player 2", "id": 200, "team_id": 2}],
        },
    ]

    result = handle_parse_teams(event, {})

    assert len(result["players"]) == 2
    assert len(result["teams"]) == 2
    mock_merge.assert_called_once_with(event)


@patch("event_handler.save_working_set")
@patch("event_handler.make_predictions_teams")
def test_handle_make_predictions(mock_predictions, mock_save_working_set):
    """Predictions run on relational players + teams and stage the enriched set."""
    input_players = [
        {"name": "Player 1", "team_id": 1, "gpg": 0.5},
        {"name": "Player 2", "team_id": 2, "gpg": 0.7},
    ]
    teams = [{"team_id": 1}, {"team_id": 2}]

    output_players = [
        {"name": "Player 1", "gpg": 0.5, "stat": 0.6},
        {"name": "Player 2", "gpg": 0.7, "stat": 0.8},
    ]

    mock_predictions.return_value = output_players

    event = {"players": input_players, "teams": teams, "status": "first_run"}
    result = handle_make_predictions(event, {})

    # Lean output: GetTims re-reads the staged slices instead of receiving the
    # roster through Step Functions state (256KB limit on heavy slates).
    assert result == {"statusCode": 200, "status": "first_run"}
    mock_predictions.assert_called_once_with(input_players, teams)
    mock_save_working_set.assert_called_once_with(output_players, teams, None)


@patch("event_handler.save_working_set")
@patch("event_handler.merge_injury_data")
@patch("event_handler.get_injury_data")
def test_handle_get_injuries_with_data(mock_get_injuries, mock_merge, mock_save):
    """Injury data is merged and staged; the returned state stays lean."""
    players = [
        {"name": "Player 1", "stat": 0.8},
        {"name": "Player 2", "stat": 0.9},
    ]

    injuries = [
        {"player": "Player 1", "injury": "Upper Body", "status": "Day-to-Day"},
    ]

    merged_players = [
        {"name": "Player 1", "stat": 0.8, "injury_status": "INJURED", "injury_desc": "Day-to-Day"},
        {"name": "Player 2", "stat": 0.9, "injury_status": "HEALTHY", "injury_desc": ""},
    ]

    mock_get_injuries.return_value = injuries
    mock_merge.return_value = merged_players

    event = {"players": players}
    result = handle_get_injuries(event, {})

    assert result == {"statusCode": 200}
    mock_get_injuries.assert_called_once()
    mock_merge.assert_called_once_with(players, injuries)
    # Staged for GetGoalies to re-read instead of travelling through state.
    mock_save.assert_called_once_with(merged_players, None, None)


@patch("event_handler.load_working_set", return_value=([], [], []))
@patch("event_handler.save_working_set")
@patch("event_handler.merge_injury_data")
@patch("event_handler.get_injury_data")
def test_handle_get_injuries_empty_players(mock_get_injuries, mock_merge, mock_save, mock_load):
    """A missing/empty roster re-reads the working set and stages nothing."""
    mock_get_injuries.return_value = []
    mock_merge.return_value = []

    result = handle_get_injuries({}, {})  # lean pipeline state: no players key

    assert result == {"statusCode": 200}
    mock_get_injuries.assert_called_once()
    mock_merge.assert_called_once_with([], [])
    # An empty roster must never wipe the Working-Players table.
    mock_save.assert_not_called()


@patch("event_handler.save_working_set")
@patch("event_handler.merge_injury_data")
@patch("event_handler.get_injury_data")
def test_handle_get_injuries_no_injuries_found(mock_get_injuries, mock_merge, mock_save):
    """Test when no injuries are found."""
    players = [
        {"name": "Player 1", "stat": 0.8},
    ]

    healthy_players = [
        {"name": "Player 1", "stat": 0.8, "injury_status": "HEALTHY", "injury_desc": ""},
    ]

    mock_get_injuries.return_value = []
    mock_merge.return_value = healthy_players

    event = {"players": players}
    result = handle_get_injuries(event, {})

    assert result == {"statusCode": 200}
    mock_merge.assert_called_once_with(players, [])
    mock_save.assert_called_once_with(healthy_players, None, None)


@patch("event_handler.save_working_set")
@patch("event_handler.build_goalies_with_team_id")
@patch("event_handler.enrich_starting_goalies")
def test_handle_get_goalies_with_data(mock_enrich, mock_build, mock_save):
    """Goalies stay relational, get staged, and the returned state stays lean."""
    players = [
        {"name": "Player 1", "team_id": 1, "stat": 0.8},
        {"name": "Player 2", "team_id": 2, "stat": 0.9},
    ]
    teams = [{"team_id": 1, "opponent_id": 2}, {"team_id": 2, "opponent_id": 1}]
    starters = [{"team_abbr": "CAR", "goalie_name": "Brandon Bussi"}]
    goalies = [{"team_abbr": "CAR", "goalie_name": "Brandon Bussi", "team_id": 1}]

    mock_enrich.return_value = starters
    mock_build.return_value = goalies

    event = {"players": players, "teams": teams}
    result = handle_get_goalies(event, {})

    assert result == {"statusCode": 200}
    mock_enrich.assert_called_once()
    mock_build.assert_called_once_with(starters, teams)
    mock_save.assert_called_once_with(players, teams, goalies)


@patch("event_handler.save_working_set")
@patch("event_handler.build_goalies_with_team_id")
@patch("event_handler.enrich_starting_goalies")
def test_handle_get_goalies_empty_players(mock_enrich, mock_build, mock_save):
    """An empty roster stages nothing."""
    mock_enrich.return_value = []
    mock_build.return_value = []

    event = {"players": [], "teams": []}
    result = handle_get_goalies(event, {})

    assert result == {"statusCode": 200}
    mock_build.assert_called_once_with([], [])
    mock_save.assert_not_called()


def _roster() -> list:
    """A Picks-prod shaped roster spanning the three Tims buckets choose_picks needs."""
    return [
        {"name": "Player 1", "team_name": "Team A", "tims": 1, "stat": 0.80},
        {"name": "Player 2", "team_name": "Team A", "tims": 1, "stat": 0.95},
        {"name": "Player 3", "team_name": "Team B", "tims": 2, "stat": 0.90},
        {"name": "Player 4", "team_name": "Team C", "tims": 3, "stat": 0.70},
    ]


@patch("event_handler.send_emails")
@patch("event_handler.get_all_emails", return_value=[{"email": "test@example.com", "display_name": "Tester"}])
@patch("event_handler.get_published_picks")
def test_handle_emails_reads_picks_when_not_in_state(mock_get_picks, mock_get_emails, mock_send):
    """NotifyUsers only passes ``status``, so picks come from the DB.

    The roster used to ride in the Step Functions state, where it exceeded the
    256KB limit. It is re-read here instead.
    """
    mock_get_picks.return_value = _roster()

    result = handle_emails({"status": "normal_run"}, {})

    assert result == {"statusCode": 200}
    mock_get_picks.assert_called_once()
    picks = mock_send.call_args[0][1]
    assert [p["name"] for p in picks] == ["Player 2", "Player 3", "Player 4"]


@patch("event_handler.send_emails")
@patch("event_handler.get_all_emails", return_value=[{"email": "test@example.com", "display_name": "Tester"}])
@patch("event_handler.get_published_picks")
def test_handle_emails_prefers_players_from_state(mock_get_picks, mock_get_emails, mock_send):
    """A direct invocation carrying players still uses them."""
    mock_get_picks.return_value = _roster()

    handle_emails({"players": _roster()}, {})

    mock_get_picks.assert_not_called()
    assert [p["name"] for p in mock_send.call_args[0][1]] == ["Player 2", "Player 3", "Player 4"]


@patch("event_handler.send_emails")
@patch("event_handler.get_all_emails", return_value=[{"email": "test@example.com", "display_name": "Tester"}])
@patch("event_handler.get_published_picks", return_value=[])
def test_handle_emails_skips_softly_when_picks_are_empty(mock_get_picks, mock_get_emails, mock_send):
    """A racing/failed publish must not fail NotifyUsers; log and skip."""
    result = handle_emails({"status": "normal_run"}, {})

    assert result == {"statusCode": 200, "skipped": True}
    mock_send.assert_not_called()
