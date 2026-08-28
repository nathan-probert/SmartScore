"""Helpers to toggle the ``mock-nhl-api`` PostHog flag from CI / the CLI.

The flag is toggled via a user-level override scoped to the Lambda's shared
distinct id (``smartscore-lambda-{environment}``) rather than by editing the
flag's release conditions. This keeps the change deterministic and limited to
the mock distinct id, so real users are never affected -- and it works with
any targeting rules already defined on the flag.
"""

import os

import requests

FLAG_KEY = "mock-nhl-api"
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")


def _api_key() -> str:
    key = os.environ.get("POSTHOG_FEATURE_FLAG_KEY")
    if not key:
        raise RuntimeError("POSTHOG_FEATURE_FLAG_KEY env var is required")
    return key


def _project_id() -> int:
    value = os.environ.get("POSTHOG_PROJECT_ID")
    if not value:
        raise RuntimeError("POSTHOG_PROJECT_ID env var is required")
    return int(value)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }


def _flag_id() -> int:
    url = f"{POSTHOG_HOST}/api/projects/{_project_id()}/feature_flags/"
    resp = requests.get(url, headers=_headers(), params={"key": FLAG_KEY}, timeout=30)
    resp.raise_for_status()
    for flag in resp.json().get("results", []):
        if flag.get("key") == FLAG_KEY:
            return flag["id"]
    raise RuntimeError(f"Could not find PostHog flag with key {FLAG_KEY!r}")


def set_flag(enabled: bool, environment: str = "dev") -> None:
    """Set a user override for ``mock-nhl-api`` for the given environment."""
    distinct_id = f"smartscore-lambda-{environment}"
    url = f"{POSTHOG_HOST}/api/projects/{_project_id()}/feature_flags/" f"{_flag_id()}/override_users/"
    body = [
        {
            "override_user_id": distinct_id,
            "override_value": bool(enabled),
        }
    ]
    resp = requests.patch(url, headers=_headers(), json=body, timeout=30)
    resp.raise_for_status()


def wait_for_flag_propagation(enabled: bool, environment: str = "dev") -> None:
    """Poll the public evaluate endpoint until the flag value matches.

    The deployed Lambdas cache flag state for a few seconds (TTL), so after
    toggling we confirm the evaluated value before running the pipeline.
    """
    distinct_id = f"smartscore-lambda-{environment}"
    url = f"{POSTHOG_HOST}/flags?v=2"
    import time

    for _ in range(30):
        resp = requests.post(
            url,
            json={
                "api_key": _api_key(),
                "distinct_id": distinct_id,
                "person_properties": {"env": environment},
                "groups": {},
            },
            timeout=30,
        )
        resp.raise_for_status()
        value = resp.json().get("featureFlags", {}).get(FLAG_KEY, False)
        if bool(value) == bool(enabled):
            return
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for flag {FLAG_KEY!r} to reach value {enabled!r}")
