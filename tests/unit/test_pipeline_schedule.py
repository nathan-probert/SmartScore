"""Guards on the two-phase daily schedule and the pipeline's data ordering.

The deterministic 08:00 UTC execution is the safety net. The mid-morning
executions are what actually pick up the NHL.com projections article and the
RotoWire goalie/injury tables, which are posted/updated after the 03:00-04:00
ET run. These assertions fail if a rule drifts to the wrong hour, if a
mid-morning rule ever passes ``last_game``, or if predictions/archiving move
back ahead of the lineup fetch.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from event_handler import (
    handle_backfill,
    handle_check_completed,
    handle_get_goalies,
    handle_get_injuries,
    handle_get_lineups,
    handle_get_tims,
    handle_load_working_set,
    handle_make_predictions,
    handle_parse_teams,
    handle_save_players,
)
from service import denormalize_players_for_db

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation intrinsic tags inspectable (scalar or sequence values)."""


def _cfn_intrinsic(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node)}
    if isinstance(node, yaml.MappingNode):
        return {suffix: loader.construct_mapping(node)}
    return {suffix: loader.construct_scalar(node)}


CfnLoader.add_multi_constructor("!", _cfn_intrinsic)

TEMPLATE = yaml.load((TEMPLATES / "template.yaml").read_text(encoding="utf-8"), Loader=CfnLoader)


def _asl(name: str) -> dict:
    # envsubst placeholders are not valid JSON, so neutralize them first.
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    patched = text.replace("${AWS_REGION}", "us-east-1").replace("${AWS_ACCOUNT_ID}", "0" * 12)
    return json.loads(patched.replace("${ENV}", "prod"))


def _rule(logical_id: str) -> dict:
    return TEMPLATE["Resources"][logical_id]["Properties"]


def _target_state_machines(logical_id: str) -> list[str]:
    """The state machine name each target ARN !Sub resolves to, e.g. 'PlayerProcessingPipeline'."""
    arns = json.dumps(_rule(logical_id)["Targets"])
    return [name for name in ("PlayerProcessingPipeline", "NotifyUsers") if f"stateMachine:{name}-" in arns]


def test_deterministic_phase_stays_at_0800_utc():
    rule = _rule("DailyStepFunctionTrigger")

    assert rule["ScheduleExpression"] == "cron(0 8 * * ? *)"
    assert _target_state_machines("DailyStepFunctionTrigger") == ["PlayerProcessingPipeline"]


def test_projections_phase_runs_mid_morning_as_normal_run():
    rule = _rule("ProjectionsStepFunctionTrigger")

    # 14:30 UTC = 10:30 EDT / 09:30 EST.
    assert rule["ScheduleExpression"] == "cron(30 14 * * ? *)"
    assert rule["State"] == "ENABLED"
    assert _target_state_machines("ProjectionsStepFunctionTrigger") == ["PlayerProcessingPipeline"]

    targets = rule["Targets"]
    assert len(targets) == 1
    assert json.loads(targets[0]["Input"]) == {"source": "eventBridge"}
    assert "last_game" not in targets[0]["Input"]


def test_confirmation_phase_runs_after_the_morning_skate():
    rule = _rule("ConfirmationStepFunctionTrigger")

    # 15:15 UTC = 11:15 EDT / 10:15 EST.
    assert rule["ScheduleExpression"] == "cron(30 15 * * ? *)"
    assert _target_state_machines("ConfirmationStepFunctionTrigger") == ["PlayerProcessingPipeline"]
    assert "last_game" not in rule["Targets"][0]["Input"]


@pytest.mark.parametrize(
    ("logical_id", "cron"),
    [
        ("LineupRefreshStepFunctionTrigger", "cron(0 17 * * ? *)"),  # 13:00 EDT / 12:00 EST
        ("EveningRefreshStepFunctionTrigger", "cron(0 20 * * ? *)"),  # 16:00 EDT / 15:00 EST
    ],
)
def test_later_refreshes_are_normal_runs_without_last_game(logical_id, cron):
    """Archive/site refreshes: same pipeline, never last_game, never a new email."""
    rule = _rule(logical_id)

    assert rule["ScheduleExpression"] == cron
    assert rule["State"] == "ENABLED"
    assert _target_state_machines(logical_id) == ["PlayerProcessingPipeline"]

    targets = rule["Targets"]
    assert len(targets) == 1
    assert json.loads(targets[0]["Input"]) == {"source": "eventBridge"}
    assert "last_game" not in targets[0]["Input"]


def test_email_runs_after_the_projections_and_confirmation_phases():
    rule = _rule("DailySendEmailTrigger")

    # 15:45 UTC = 11:45 EDT / 10:45 EST, after both refreshes.
    assert rule["ScheduleExpression"] == "cron(45 15 * * ? *)"
    assert _target_state_machines("DailySendEmailTrigger") == ["NotifyUsers"]

    projections = _rule("ProjectionsStepFunctionTrigger")["ScheduleExpression"]
    confirmation = _rule("ConfirmationStepFunctionTrigger")["ScheduleExpression"]
    email = rule["ScheduleExpression"]
    assert (projections, confirmation, email) == (
        "cron(30 14 * * ? *)",
        "cron(30 15 * * ? *)",
        "cron(45 15 * * ? *)",
    )


def test_first_run_fetches_lineups_before_predictions():
    pipeline = _asl("player_processing_pipeline.asl.json")["States"]

    assert pipeline["CheckIfPlayersOutputIsEmpty"]["Default"] == "GetInjuries"
    assert pipeline["GetInjuries"]["Next"] == "GetGoalies"
    assert pipeline["GetGoalies"]["Next"] == "GetLineups"
    assert pipeline["GetLineups"]["Next"] == "MakePredictions"
    assert pipeline["MakePredictions"]["Next"] == "GetTims"


def test_normal_run_archives_before_publishing():
    pipeline = _asl("player_processing_pipeline.asl.json")["States"]

    # The former CheckInitialRun Choice state was a no-op once PR #134 made
    # normal_run archive too (all three branches went to the same SaveToDb);
    # GetTims now routes straight to SaveToDb.
    assert pipeline["GetTims"]["Next"] == "SaveToDb"
    assert "CheckInitialRun" not in pipeline

    assert pipeline["SaveToDb"]["Next"] == "CheckSaveHistory"
    history = {choice["StringEquals"]: choice["Next"] for choice in pipeline["CheckSaveHistory"]["Choices"]}
    assert history["first_run"] == "UpdateHistory"
    assert pipeline["CheckSaveHistory"]["Default"] == "PublishToDb"


_PLAYERS = [
    {
        "id": 1,
        "name": "Player 1",
        "team_id": 10,
        "gpg": 0.5,
        "hgpg": 0.5,
        "five_gpg": 0.5,
        "hppg": 0.1,
        "lineup_unit": "F1",
        "lineup_position_group": "F",
        "pp_unit": "POWER PLAY #1",
        "lineup_status": "PROJECTED",
        "injury_status": "HEALTHY",
        "injury_desc": "",
    }
]
_TEAMS = [
    {"team_id": 10, "opponent_id": 11, "team_name": "Team A", "home": True, "tgpg": 3.0, "otga": 2.5, "otshga": 0.5}
]


def _run_chain(status):
    """Drive the real handlers in ASL order with mocked external sources."""
    players = [dict(player) for player in _PLAYERS]
    with (
        patch("event_handler.load_working_set", return_value=(players, _TEAMS)),
        patch("event_handler.save_player_snapshots") as save_snapshot,
        patch("event_handler.save_working_set"),
        patch("event_handler.get_date", return_value="2026-04-15"),
        patch("event_handler.get_injury_data", return_value=[]),
        patch("event_handler.enrich_starting_goalies", return_value=[]),
        patch("event_handler.get_nhl_com_lineups", return_value=[]),
        patch("event_handler.get_rotowire_lineups", return_value=[]),
        patch("event_handler.make_predictions_teams", side_effect=lambda p, t: p),
        patch("event_handler.get_tims", side_effect=lambda p, allow_empty=False: p),
    ):
        state = {"status": status, "players": players, "teams": _TEAMS}
        if status != "first_run":
            state = handle_load_working_set({"status": status}, {})
        state = handle_get_injuries(state, {})
        state = handle_get_goalies(state, {})
        state = handle_get_lineups(state, {})
        if status == "first_run":
            state = handle_make_predictions(state, {})
        state = handle_get_tims(state, {})
        if status != "last_run":
            # ASL: last_run goes straight to PublishToDb and never archives.
            state = handle_save_players(state, {})

    return state, save_snapshot


@pytest.mark.parametrize("status", ["first_run", "normal_run", "last_run"])
def test_status_survives_the_whole_handler_chain(status):
    """B1: every handler must carry the status into CheckSaveHistory."""
    state, save_snapshot = _run_chain(status)

    assert state["status"] == status
    if status == "last_run":
        save_snapshot.assert_not_called()
    else:
        save_snapshot.assert_called_once()


@pytest.mark.parametrize("status", ["first_run", "normal_run", "last_run"])
def test_status_routes_to_the_expected_archive_branch(status):
    pipeline = _asl("player_processing_pipeline.asl.json")["States"]

    if status == "last_run":
        # last_run is routed at CheckLastRun, before the enrichment chain;
        # the enrichment branches (GetTims -> SaveToDb) must be unreachable for it.
        last_run_routing = {choice["StringEquals"]: choice["Next"] for choice in pipeline["CheckLastRun"]["Choices"]}
        assert last_run_routing["last_run"] == "PublishToDb"
        return

    assert pipeline["GetTims"]["Next"] == "SaveToDb"

    history = {choice["StringEquals"]: choice["Next"] for choice in pipeline["CheckSaveHistory"]["Choices"]}
    assert history["first_run"] == "UpdateHistory"
    assert pipeline["CheckSaveHistory"]["Default"] == "PublishToDb"


@pytest.mark.parametrize(
    "handler,event",
    [
        (handle_load_working_set, {}),
        (handle_get_tims, {"players": []}),
        (handle_save_players, {"players": []}),
    ],
)
def test_missing_status_is_an_explicit_error(handler, event):
    """A missing status must fail loudly, not silently become first_run."""
    with pytest.raises(ValueError, match="requires status"):
        handler(event, {})


@patch("event_handler.get_rotowire_lineups", return_value=[])
@patch("event_handler.get_nhl_com_lineups", return_value=[])
def test_empty_lineup_fetch_preserves_existing_projection(mock_nhl, mock_rotowire):
    """S1: a transient empty fetch must not overwrite good lineup data."""
    players = [dict(_PLAYERS[0])]

    result = handle_get_lineups({"players": players, "status": "normal_run"}, {})

    assert result["players"][0]["lineup_unit"] == "F1"
    assert result["players"][0]["pp_unit"] == "POWER PLAY #1"
    assert result["players"][0]["lineup_status"] == "PROJECTED"
    assert result["status"] == "normal_run"


@patch("event_handler.merge_injury_data")
@patch("event_handler.get_injury_data", return_value=None)
def test_injury_fetch_failure_preserves_existing_fields(mock_get, mock_merge):
    """S2: a failed injury fetch is not the same as "everyone healthy"."""
    players = [{"name": "Player 1", "injury_status": "INJURED", "injury_desc": "Upper body"}]

    result = handle_get_injuries({"players": players, "status": "normal_run"}, {})

    assert result["players"] == [{"name": "Player 1", "injury_status": "INJURED", "injury_desc": "Upper body"}]
    mock_merge.assert_not_called()


@patch("event_handler.enrich_starting_goalies", return_value=None)
def test_goalie_fetch_failure_preserves_existing_goalies(mock_enrich):
    """S2: a failed goalie fetch passes the previous goalies through."""
    goalies = [{"team_id": 10, "goalie_name": "Starter", "status": "Confirmed"}]

    result = handle_get_goalies(
        {"players": [], "teams": [], "goalies": goalies, "status": "normal_run"},
        {},
    )

    assert result["goalies"] == goalies
    assert result["status"] == "normal_run"


def test_failed_goalie_fetch_leaves_opp_columns_absent():
    """S2: goalies=None tells the denormalizer to leave opp_goalie_* untouched."""
    rows = denormalize_players_for_db(
        [{"name": "Player 1", "team_id": 10}],
        teams=[{"team_id": 10, "opponent_id": 11, "team_name": "Team A"}],
        goalies=None,
    )

    assert not any(key.startswith("opp_goalie_") for key in rows[0])


def test_empty_goalie_payload_still_marks_unknown():
    """A valid empty payload is different from a failure and keeps the columns."""
    rows = denormalize_players_for_db(
        [{"name": "Player 1", "team_id": 10}],
        teams=[{"team_id": 10, "opponent_id": 11, "team_name": "Team A"}],
        goalies=[],
    )

    assert rows[0]["opp_goalie_status"] == "UNKNOWN"
    assert rows[0]["opp_goalie_confirmed"] is False


def _trace_asl(pipeline, status):
    """Walk the pipeline graph for a status, returning the visited Task states."""
    states = pipeline["States"]
    name = "CheckCompletion"
    tasks = []
    for _ in range(50):
        state = states[name]
        if state["Type"] == "Choice":
            nxt = next(
                (choice["Next"] for choice in state["Choices"] if choice.get("StringEquals") == status),
                state.get("Default"),
            )
        else:
            tasks.append(name)
            if state.get("End"):
                return tasks
            nxt = state.get("Next")
        assert nxt, f"{name} has no next state for {status!r}"
        name = nxt
    raise AssertionError("pipeline graph did not terminate")


def test_asl_routing_per_run_status():
    """S4: the graph itself decides archiving, not the handler chain."""
    pipeline = _asl("player_processing_pipeline.asl.json")

    first = _trace_asl(pipeline, "first_run")
    assert first[:3] == ["Backfill", "GetPlayersStateMachine", "EnsureFirstRunStatus"]
    assert first.index("SaveToDb") < first.index("UpdateHistory") < first.index("PublishToDb")

    normal = _trace_asl(pipeline, "normal_run")
    assert normal[0] == "LoadWorkingSet"
    assert "SaveToDb" in normal
    assert "UpdateHistory" not in normal
    assert normal[-1] == "PublishToDb"

    last = _trace_asl(pipeline, "last_run")
    assert last[0] == "LoadWorkingSet"
    assert "SaveToDb" not in last
    assert "UpdateHistory" not in last
    assert last[-1] == "PublishToDb"


@patch("event_handler.backfill_dates")
@patch("event_handler.check_db_for_date", return_value=False)
def test_first_run_status_survives_check_completed_backfill_and_parse(mock_check_db, mock_backfill):
    """B1: the real first-run entry must still have first_run at GetInjuries."""
    state = handle_check_completed({}, {})
    state = handle_backfill(state, {})
    assert state["status"] == "first_run"

    parsed = handle_parse_teams({"status": state["status"], "teams": []}, {})
    assert parsed["status"] == "first_run"

    pipeline = _asl("player_processing_pipeline.asl.json")["States"]
    assert pipeline["GetPlayersStateMachine"]["Next"] == "EnsureFirstRunStatus"
    restore = pipeline["EnsureFirstRunStatus"]
    assert restore["Type"] == "Pass"
    assert restore["Parameters"]["status"] == "first_run"
    assert restore["Next"] == "CheckIfPlayersOutputIsEmpty"
