"""
Facade for all feature-flag interaction.

Flags are evaluated against PostHog's feature-flag service using the official
``posthog`` SDK (``posthog.evaluate_flags``), so already-deployed Lambdas can be
toggled at runtime without redeploying — for example, to point a dev
environment at mocked NHL API data in the off-season.

Fallback behaviour:
  * A matching ``FEATURE_<NAME>`` environment variable (e.g.
    ``FEATURE_SEND_EMAILS``) always takes precedence when set. This preserves
    the legacy env-var behaviour and makes the module usable (and
    unit-testable) when PostHog is not configured.
  * If PostHog is not configured/unreachable, the env-var fallback is returned.
"""

import os
import time
from typing import Any, Dict, Optional

from config import ENV

# PostHog configuration
POSTHOG_FEATURE_FLAG_KEY = os.environ.get("POSTHOG_FEATURE_FLAG_KEY")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")

# The PostHog flag that switches deployed lambdas to serve frozen NHL fixtures.
NHL_MOCK_FLAG = "mock-nhl-api"

# How long (seconds) an evaluated flag snapshot is cached before PostHog is
# consulted again. Kept short so runtime toggles take effect quickly, while
# avoiding a /flags round-trip on every read within a single pipeline.
_FLAG_TTL_SECONDS = int(os.environ.get("FEATURE_FLAG_TTL_SECONDS", "15"))

# A per-environment distinct id so PostHog targeting rules (e.g. env == dev)
# resolve consistently for every Lambda in the same environment.
_DISTINCT_ID = f"smartscore-lambda-{ENV}"
_PROPERTIES: Dict[str, Any] = {"env": ENV}

# name -> (value, fetched_at)
_cache: Dict[str, tuple] = {}


def _env_var_name(name: str) -> str:
    return f"FEATURE_{name.upper()}"


def _get_env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(_env_var_name(name))
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def invalidate_cache() -> None:
    """Clear cached flag values (used by tests and the test harness)."""
    _cache.clear()


def _evaluate_posthog(name: str) -> Optional[bool]:
    """Evaluate a single flag via the PostHog SDK, or None if unavailable."""
    try:
        import posthog

        posthog.project_api_key = POSTHOG_FEATURE_FLAG_KEY
        posthog.host = POSTHOG_HOST
        if not POSTHOG_FEATURE_FLAG_KEY:
            return None

        flags = posthog.evaluate_flags(
            _DISTINCT_ID,
            person_properties=_PROPERTIES,
            flag_keys=[name],
        )
        return bool(flags.is_enabled(name))
    except Exception:  # noqa: BLE001 - a PostHog outage must not break the app
        return None


def is_feature_enabled(name: str) -> bool:
    """Return whether the named feature flag is enabled for this environment.

    A set ``FEATURE_<NAME>`` env var wins over PostHog; otherwise PostHog is
    consulted (falling back to ``False`` if it cannot be reached).
    """
    env_var = _env_var_name(name)
    if os.environ.get(env_var) is not None:
        return _get_env_flag(name)

    now = time.time()
    cached = _cache.get(name)
    if cached and now - cached[0] < _FLAG_TTL_SECONDS:
        return cached[1]

    value = _evaluate_posthog(name)
    if value is None:
        value = _get_env_flag(name)
    _cache[name] = (now, value)
    return value
