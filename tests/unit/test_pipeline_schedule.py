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

import yaml

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation intrinsic tag scalars inspectable."""


CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: {suffix: loader.construct_scalar(node)})

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

    # 16:30 UTC = 12:30 EDT / 11:30 EST.
    assert rule["ScheduleExpression"] == "cron(30 16 * * ? *)"
    assert _target_state_machines("ConfirmationStepFunctionTrigger") == ["PlayerProcessingPipeline"]
    assert "last_game" not in rule["Targets"][0]["Input"]


def test_email_runs_after_the_projections_phase():
    rule = _rule("DailySendEmailTrigger")

    # 15:30 UTC = 11:30 EDT / 10:30 EST, one hour after the projections phase.
    assert rule["ScheduleExpression"] == "cron(30 15 * * ? *)"
    assert _target_state_machines("DailySendEmailTrigger") == ["NotifyUsers"]


def test_first_run_fetches_lineups_before_predictions():
    pipeline = _asl("player_processing_pipeline.asl.json")["States"]

    assert pipeline["CheckIfPlayersOutputIsEmpty"]["Default"] == "GetInjuries"
    assert pipeline["GetInjuries"]["Next"] == "GetGoalies"
    assert pipeline["GetGoalies"]["Next"] == "GetLineups"
    assert pipeline["GetLineups"]["Next"] == "MakePredictions"
    assert pipeline["MakePredictions"]["Next"] == "GetTims"


def test_normal_run_archives_before_publishing():
    pipeline = _asl("player_processing_pipeline.asl.json")["States"]

    routing = {choice["StringEquals"]: choice["Next"] for choice in pipeline["CheckInitialRun"]["Choices"]}
    assert routing["normal_run"] == "SaveToDb"
    assert routing["first_run"] == "SaveToDb"

    assert pipeline["SaveToDb"]["Next"] == "CheckSaveHistory"
    history = {choice["StringEquals"]: choice["Next"] for choice in pipeline["CheckSaveHistory"]["Choices"]}
    assert history["first_run"] == "UpdateHistory"
    assert pipeline["CheckSaveHistory"]["Default"] == "PublishToDb"
