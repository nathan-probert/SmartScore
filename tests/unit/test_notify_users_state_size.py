"""Guards on the Step Functions payload size in the daily state machines.

Step Functions caps the JSON state that flows between states at 262,144 bytes.
``CheckCompleted`` used to return the entire Picks-prod roster (roughly 640
skater rows), which serialized to ~440KB once the opp_goalie_* and lineup
columns were added: NotifyUsers failed every afternoon with
``States.DataLimitExceeded`` in step ``GetDate`` and no email was ever sent,
and PlayerProcessingPipeline hit the same wall on its normal_run branch.

``CheckCompleted`` is now status-only. NotifyUsers additionally projects its
result down to ``status`` and ``handle_emails`` re-reads the day's picks;
PlayerProcessingPipeline routes normal_run/last_run to LoadWorkingSet, which
reloads the day's separated lean slices. normal_run re-enters the enrichment
chain at GetInjuries; last_run stops at PublishToDb, which clears the tims
groups without calling the locked picks API. These assertions fail if any of
those boundaries regress.
"""

import json
from pathlib import Path

import pytest

STATE_LIMIT_BYTES = 262_144


def _asl(name: str) -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "templates" / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"could not locate templates/{name}")


def _definition(name: str) -> dict:
    # envsubst placeholders are not valid JSON, so neutralize them first.
    text = _asl(name).read_text(encoding="utf-8")
    patched = text.replace("${AWS_REGION}", "us-east-1").replace("${AWS_ACCOUNT_ID}", "0" * 12)
    patched = patched.replace("${ENV}", "prod")
    return json.loads(patched)


@pytest.fixture(scope="module")
def notify_users() -> dict:
    return _definition("notify_users.asl.json")


def test_notify_users_definition_is_valid_json(notify_users):
    """The ASL is envsubst-ed into an --definition argument, so it must parse."""
    assert notify_users["StartAt"] == "GetDate"


def test_get_date_projects_check_completed_result_to_status(notify_users):
    """GetDate must shrink CheckCompleted's roster before it enters the state.

    ``ResultSelector`` is what keeps this under the service limit; without it
    the raw ~440KB result becomes the state and the state fails outright.
    """
    get_date = notify_users["States"]["GetDate"]
    selector = get_date.get("ResultSelector")

    assert selector is not None, (
        "GetDate has no ResultSelector, so the full Picks-prod roster enters the "
        "Step Functions state and blows the 256KB limit"
    )
    assert selector == {"status.$": "$.status"}, f"GetDate must pass only the run status onward, got {selector!r}"


def test_notify_users_never_carries_the_roster(notify_users):
    """No state in NotifyUsers may reference ``players``."""
    text = json.dumps(notify_users)
    assert "players" not in text, (
        "NotifyUsers references 'players'; the roster must be read in the Lambda, not carried through the state machine"
    )


def test_pipeline_normal_and_last_run_reload_separated_slices():
    """PlayerProcessingPipeline must never load the roster through GetDate.

    CheckCompleted is status-only, so the normal_run and last_run branches
    land on LoadWorkingSet, which returns the day's lean players/teams slices
    from the Working-* tables. The state always carries separated slices --
    the same shape first_run uses -- and never denormalized rows.
    """
    pipeline = _definition("player_processing_pipeline.asl.json")

    routing = {choice["StringEquals"]: choice["Next"] for choice in pipeline["States"]["CheckCompletion"]["Choices"]}
    assert routing["normal_run"] == "LoadWorkingSet"
    assert routing["last_run"] == "LoadWorkingSet"
    assert routing["first_run"] == "Backfill"

    load = pipeline["States"]["LoadWorkingSet"]
    assert load["Type"] == "Task"
    assert load["Resource"].endswith(":function:LoadWorkingSet-prod")
    assert load["Next"] == "CheckLastRun"

    # last_run exits before the enrichment chain: every game has started, so
    # the picks API is locked and a Tims read can only fail (the GetTims
    # Lambda times out before its fallback can even run). PublishToDb clears
    # the tims groups itself instead.
    last_run_routing = {
        choice["StringEquals"]: choice["Next"] for choice in pipeline["States"]["CheckLastRun"]["Choices"]
    }
    assert last_run_routing["last_run"] == "PublishToDb"
    assert pipeline["States"]["CheckLastRun"]["Default"] == "GetInjuries"

    # normal_run goes through SaveToDb so the mid-morning refresh also
    # updates Player-Snapshots; the former CheckInitialRun Choice was a no-op
    # (all three branches were SaveToDb, PR #134/#136) and was removed -
    # GetTims routes straight to SaveToDb.
    assert "CheckInitialRun" not in pipeline["States"]
    assert pipeline["States"]["GetTims"]["Next"] == "SaveToDb"

    assert pipeline["States"]["SaveToDb"]["Next"] == "CheckSaveHistory"
    history_routing = {
        choice["StringEquals"]: choice["Next"] for choice in pipeline["States"]["CheckSaveHistory"]["Choices"]
    }
    assert history_routing["first_run"] == "UpdateHistory"
    assert pipeline["States"]["CheckSaveHistory"]["Default"] == "PublishToDb"

    # GetDate must not trim anything for these branches any more -- its
    # result is already just the status, verified in test_handle_check_completed.
    assert "ResultSelector" not in pipeline["States"]["GetDate"]
