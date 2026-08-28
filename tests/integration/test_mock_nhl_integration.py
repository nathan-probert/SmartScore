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

Skipped unless invoked with ``--run-integration`` and the required env vars.
"""

import json
import os
from typing import Dict, List, Optional

import boto3
import pytest
from tests.integration.posthog_flag import set_flag, wait_for_flag_propagation

pytestmark = pytest.mark.integration

ENVIRONMENT = os.environ.get("ENV", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Players seeded into tests/fixtures/nhl per team abbreviation.
EXPECTED_PLAYERS = {
    "TOR": {"John Tavares", "Mitch Marner"},
    "EDM": {"Connor McDavid", "Leon Draisaitl"},
}


def _enabled() -> bool:
    return all(os.environ.get(name) for name in ("POSTHOG_FEATURE_FLAG_KEY", "POSTHOG_PROJECT_ID", "AWS_ACCOUNT_ID"))


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
        pytest.skip("integration disabled: set POSTHOG_FEATURE_FLAG_KEY, " "POSTHOG_PROJECT_ID, and AWS_ACCOUNT_ID")

    lambda_client = boto3.client("lambda", region_name=REGION)

    set_flag(True, ENVIRONMENT)
    wait_for_flag_propagation(True, ENVIRONMENT)
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
        set_flag(False, ENVIRONMENT)
