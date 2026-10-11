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


def test_pipeline_normal_and_last_run_recompute_rather_than_carry_the_roster():
    """PlayerProcessingPipeline must never load the roster through GetDate.

    CheckCompleted is status-only, so the normal_run and last_run branches
    route through the GetPlayers sub-machine -- the same enrichment chain as
    first_run -- instead of skipping straight to GetTims/PublishToDb with a
    roster that no longer exists in the state.
    """
    pipeline = _definition("player_processing_pipeline.asl.json")

    routing = {choice["StringEquals"]: choice["Next"] for choice in pipeline["States"]["CheckCompletion"]["Choices"]}
    assert routing["normal_run"] == "GetPlayersStateMachine"
    assert routing["last_run"] == "GetPlayersStateMachine"
    assert routing["first_run"] == "Backfill"

    # The sub-machine output is nested (not spliced over the state) so the
    # run status survives; FlattenGetPlayers then rebuilds the lean state.
    get_players = pipeline["States"]["GetPlayersStateMachine"]
    assert get_players["ResultPath"] == "$.getPlayers"
    assert "OutputPath" not in get_players

    flatten = pipeline["States"]["FlattenGetPlayers"]
    assert flatten["Parameters"]["status.$"] == "$.status"
    assert flatten["Parameters"]["players.$"] == "$.getPlayers.parsed.players"
    assert flatten["Parameters"]["teams.$"] == "$.getPlayers.parsed.teams"

    # last_run now arrives at CheckInitialRun via the chain and must route
    # to PublishToDb, not fall through the Choice unhandled.
    initial_routing = {
        choice["StringEquals"]: choice["Next"] for choice in pipeline["States"]["CheckInitialRun"]["Choices"]
    }
    assert initial_routing["last_run"] == "PublishToDb"
    assert initial_routing["normal_run"] == "PublishToDb"
    assert initial_routing["first_run"] == "SaveToDb"
