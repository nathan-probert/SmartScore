"""Guards on the NotifyUsers state machine's Step Functions payload size.

Step Functions caps the JSON state that flows between states at 262,144 bytes.
``CheckCompleted`` returns the entire Picks-prod roster, which is roughly 640
skater rows; before the opp_goalie_* and lineup columns were added to that table
this state sat at ~68% of the limit, and afterwards it serialized to ~440KB.
NotifyUsers then failed every afternoon with ``States.DataLimitExceeded`` in step
``GetDate`` and no email was ever sent.

The roster was never needed here -- ``choose_picks`` reduces it to one pick per
Tims bucket and the template renders four fields -- so ``GetDate`` projects the
Lambda result down to ``status`` and ``handle_emails`` re-reads the day's picks.
These assertions fail if that projection is ever removed.
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


def test_pipeline_still_receives_the_roster():
    """The roster is only droppable where it is unused.

    PlayerProcessingPipeline feeds CheckCompleted's players into GetTims on the
    normal_run branch, so its GetDate must keep passing them through. This
    guards against a well-meaning 'same fix everywhere' edit breaking that run.
    """
    pipeline = _definition("player_processing_pipeline.asl.json")
    assert "ResultSelector" not in pipeline["States"]["GetDate"]
    assert pipeline["States"]["GetPlayersStateMachine"]["Parameters"]["Input"]["input.$"] == "$"
