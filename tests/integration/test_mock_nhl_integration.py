"""Integration test against the deployed AWS-dev stack using mocked NHL data.

When run (opt-in, since it talks to real AWS + PostHog), it:

1. Toggles the ``mock-nhl-api`` PostHog flag ON for ``smartscore-lambda-{env}``.
2. Waits for the deployed Lambdas to pick up the new value (they cache with a
   short TTL).
3. Invokes the deployed ``GetTeams-{env}`` and ``GetPlayersFromTeam-{env}``
   Lambdas, which run the schedule -> roster -> player-landing pipeline
   entirely against the frozen fixtures (no live NHL calls).
4. Asserts the returned players match the dummy fixtures (TOR: Tavares, Marner;
   EDM: McDavid, Draisaitl).
5. Toggles the flag back OFF (always, even on failure).

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
        if bool(resp.json().get("featureFlags", {}).get(FLAG_KEY, False)) == enabled:
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


@pytest.mark.integration
def test_get_teams_and_players_run_against_mock_data():
    if not _enabled():
        pytest.skip(
            "integration disabled: set POSTHOG_FEATURE_FLAG_KEY, "
            "POSTHOG_PERSONAL_API_KEY, POSTHOG_PROJECT_ID, and AWS_ACCOUNT_ID"
        )

    lambda_client = boto3.client("lambda", region_name=REGION)

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
    finally:
        _set_flag(False)
