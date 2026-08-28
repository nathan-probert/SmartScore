"""Integration test against the deployed AWS-dev stack using mocked NHL data.

When run (opt-in, since it talks to real AWS + PostHog), it:

1. Toggles the ``mock-nhl-api`` PostHog flag ON (it is permanently targeted
   to ``env == dev``, so prod is never mocked).
2. Waits for the deployed Lambdas to pick up the new value (they cache with a
   short TTL).
3. Directly invokes the deployed ``GetTeams-{env}`` and ``GetPlayersFromTeam-{env}``
   Lambdas, which run the schedule -> roster -> player-landing pipeline
   entirely against the frozen fixtures (no live NHL calls).
4. Runs the real ``GetPlayers-{env}`` Step Functions state machine end-to-end
   (GetTeams -> Map -> GetPlayersFromTeam -> ParseData) against the same mocked
   data, verifying variables (team context, players) are threaded through the
   state machine correctly.
5. Asserts the returned players match the dummy fixtures (TOR: Tavares, Marner;
   EDM: McDavid, Draisaitl).
6. Toggles the flag back OFF (always, even on failure).

Skipped unless the required env vars are present.
"""

import json
import os
import time
from typing import Dict, List, Optional

import boto3
import pytest

pytestmark = pytest.mark.integration

ENVIRONMENT = os.environ.get("ENV", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
FLAG_KEY = "mock-nhl-api"

# Players seeded into tests/fixtures/nhl per team abbreviation.
EXPECTED_PLAYERS = {
    "TOR": {"John Tavares", "Mitch Marner"},
    "EDM": {"Connor McDavid", "Leon Draisaitl"},
}


# ---------------------------------------------------------------------------
# PostHog flag toggling.
#
# The ``mock-nhl-api`` flag is permanently targeted to ``env == dev`` so prod
# can never be mocked. The harness toggles the flag's ``active`` field:
#   * PATCH active=true  -> dev lambdas serve mock fixtures
#   * PATCH active=false -> dev lambdas hit the live NHL API (post-test)
#
# Two keys are involved:
#   * POSTHOG_FEATURE_FLAG_KEY (project API key) - used by the deployed
#     Lambdas and by the /flags propagation check below.
#   * POSTHOG_PERSONAL_API_KEY - required to call the private project admin
#     API to locate the flag and toggle its ``active`` field.
# ---------------------------------------------------------------------------
def _project_api_key() -> str:
    key = os.environ.get("POSTHOG_FEATURE_FLAG_KEY")
    if not key:
        raise RuntimeError("POSTHOG_FEATURE_FLAG_KEY env var is required")
    return key


def _admin_api_key() -> str:
    key = os.environ.get("POSTHOG_PERSONAL_API_KEY")
    if not key:
        raise RuntimeError("POSTHOG_PERSONAL_API_KEY env var is required")
    return key


def _posthog_project_id() -> int:
    value = os.environ.get("POSTHOG_PROJECT_ID")
    if not value:
        raise RuntimeError("POSTHOG_PROJECT_ID env var is required")
    return int(value)


def _flag_id() -> int:
    import requests

    url = f"{POSTHOG_HOST}/api/projects/{_posthog_project_id()}/feature_flags/"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {_admin_api_key()}"},
        params={"key": FLAG_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    for flag in resp.json().get("results", []):
        if flag.get("key") == FLAG_KEY:
            return flag["id"]
    raise RuntimeError(f"Could not find PostHog flag with key {FLAG_KEY!r}")


def _set_flag(enabled: bool) -> None:
    """Toggle the flag's ``active`` field so dev mock mode is on/off."""
    import requests

    url = f"{POSTHOG_HOST}/api/projects/{_posthog_project_id()}/feature_flags/" f"{_flag_id()}/"
    resp = requests.patch(
        url,
        headers={
            "Authorization": f"Bearer {_admin_api_key()}",
            "Content-Type": "application/json",
        },
        json={"active": bool(enabled)},
        timeout=30,
    )
    resp.raise_for_status()


def _wait_for_flag(enabled: bool) -> None:
    import requests

    distinct_id = f"smartscore-lambda-{ENVIRONMENT}"
    url = f"{POSTHOG_HOST}/flags?v=2"
    for _ in range(30):
        resp = requests.post(
            url,
            json={
                "api_key": _project_api_key(),
                "distinct_id": distinct_id,
                "person_properties": {"env": ENVIRONMENT},
                "groups": {},
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        flag = (data.get("flags") or {}).get(FLAG_KEY) or {}
        if bool(flag.get("enabled", False)) == enabled:
            return
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for flag {FLAG_KEY!r} to reach value {enabled!r}")


# ---------------------------------------------------------------------------
# AWS Lambda helpers.
# ---------------------------------------------------------------------------
def _enabled() -> bool:
    return all(
        os.environ.get(name)
        for name in (
            "POSTHOG_FEATURE_FLAG_KEY",
            "POSTHOG_PERSONAL_API_KEY",
            "POSTHOG_PROJECT_ID",
            "AWS_ACCOUNT_ID",
        )
    )


def _invoke(lambda_client, function_name: str, payload: Optional[dict] = None) -> dict:
    resp = lambda_client.invoke(
        FunctionName=f"{function_name}-{ENVIRONMENT}",
        Payload=json.dumps(payload or {}),
    )
    assert resp["StatusCode"] == 200, resp
    return json.loads(resp["Payload"].read())


def _fetch_roster_players(lambda_client, teams: List[dict]) -> Dict[str, set]:
    by_abbr: Dict[str, set] = {}
    for team in teams:
        abbr = team["team_abbr"]
        result = _invoke(lambda_client, "GetPlayersFromTeam", team)
        names = {p["name"] for p in result.get("players", []) if p.get("name")}
        by_abbr[abbr] = names
    return by_abbr


def _state_machine_arn(name: str) -> str:
    acct = os.environ["AWS_ACCOUNT_ID"]
    return f"arn:aws:states:{REGION}:{acct}:stateMachine:{name}-{ENVIRONMENT}"


def _run_get_players_state_machine(sfn_client, timeout: int = 300) -> List[dict]:
    """Start the GetPlayers-{env} state machine and return its final output.

    The state machine runs GetTeams -> Map(GetPlayersFromTeam) -> ParseData,
    so a successful run proves variables (team context + players) are threaded
    through the real Step Functions orchestration against the mocked data.
    """
    arn = _state_machine_arn("GetPlayers")
    name = f"integration-{int(time.time() * 1000)}"
    start = sfn_client.start_execution(stateMachineArn=arn, input="{}", name=name)
    execution_arn = start["executionArn"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = sfn_client.describe_execution(executionArn=execution_arn)
        status = detail["status"]
        if status == "SUCCEEDED":
            return json.loads(detail.get("output", "null"))
        if status in ("FAILED", "ABORTED", "TIMED_OUT"):
            raise AssertionError(
                f"GetPlayers-{ENVIRONMENT} state machine finished with status {status}: "
                f"{detail.get('error')} {detail.get('cause')}"
            )
        time.sleep(5)
    raise AssertionError(f"Timed out waiting for GetPlayers-{ENVIRONMENT} state machine")


def _assert_state_machine_players(entries: List[dict]) -> None:
    """Assert the state machine's ParseData output matches the fixtures."""
    assert entries, "GetPlayers state machine returned no players"
    names = {e.get("name") for e in entries if e.get("name")}
    expected_names = {player for players in EXPECTED_PLAYERS.values() for player in players}
    missing = expected_names - names
    assert not missing, f"State machine output missing players: {missing}"

    team_names = {e.get("team_name") for e in entries}
    assert team_names == {"Toronto", "Edmonton"}, f"Unexpected teams: {team_names}"

    # The Map iterator must preserve per-team context (home/away).
    by_team = {e.get("team_name"): e.get("home") for e in entries}
    assert by_team.get("Toronto") is True
    assert by_team.get("Edmonton") is False


@pytest.mark.integration
def test_get_teams_and_players_run_against_mock_data():
    if not _enabled():
        pytest.skip(
            "integration disabled: set POSTHOG_FEATURE_FLAG_KEY, "
            "POSTHOG_PERSONAL_API_KEY, POSTHOG_PROJECT_ID, and AWS_ACCOUNT_ID"
        )

    lambda_client = boto3.client("lambda", region_name=REGION)
    sfn_client = boto3.client("stepfunctions", region_name=REGION)

    _set_flag(True)
    _wait_for_flag(True)
    try:
        teams_result = _invoke(lambda_client, "GetTeams", {})
        assert teams_result.get("statusCode") == 200, teams_result
        teams = teams_result.get("teams", [])
        abbrs = {t["team_abbr"] for t in teams}
        assert abbrs == {"TOR", "EDM"}, f"Unexpected teams: {abbrs}"

        rosters = _fetch_roster_players(lambda_client, teams)
        for abbr, expected in EXPECTED_PLAYERS.items():
            actual = rosters.get(abbr, set())
            missing = expected - actual
            assert not missing, f"{abbr} missing players: {missing}"

        entries = _run_get_players_state_machine(sfn_client)
        _assert_state_machine_players(entries)
    finally:
        _set_flag(False)
