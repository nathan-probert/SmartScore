"""Per-game ML picks kick relay.

The PlayerProcessingPipeline state machine's KickMLPicks Task state invokes
this Lambda at the END of the run (after SaveToDb/UpdateHistory/PublishToDb).
The relay turns the kick into a single ``repository_dispatch`` call on the
GitHub repo so .github/workflows/ml-picks.yml runs the daily picks pass on
the Actions runner. Serialization is the whole point (PR #146 follow-up
race fix): the kick can only fire once today's snapshot rows are published,
so the Actions job never computes picks from stale groups.

RELAY ONLY: stdlib urllib for the HTTP call, no ML dependencies, and no
Supabase/Picks access of any kind - the workflow performs those upserts.
docs/ml-picks-runbook.md ("Per-game kick").
"""

import datetime
import json
import os
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from aws_lambda_powertools import Logger

logger = Logger()

GITHUB_API_BASE = "https://api.github.com"

DEFAULT_GITHUB_OWNER = "nathan-probert"
DEFAULT_GITHUB_REPO = "SmartScore"

#: repository_dispatch event_type the ml-picks.yml workflow listens to.
DISPATCH_EVENT_TYPE = "ml-picks-game"
#: client_payload context marker: this dispatch came from a game-start kick,
#: not a manual run (the workflow logs it for triage).
KICK_CONTEXT = "post-game-start"

#: One immediate retry, after this backoff, on transient GitHub responses.
RETRY_BACKOFF_SECONDS = 5

#: 429 (secondary rate limit) plus the 5xx family are worth one retry;
#: 401/403/422 are deterministic failures and must not be retried.
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class MissingDispatchTokenError(RuntimeError):
    """Raised when GITHUB_DISPATCH_TOKEN is absent/empty (fail loudly)."""


def pick_date_now(zone_name=None):
    """Today's pick-date in the pick timezone, ``YYYY-MM-DD``.

    Mirrors the pipeline's America/Toronto notion of "today":
    service.get_date() (smartscore/service.py) is
    ``datetime.now(pytz.timezone("America/Toronto")).strftime("%Y-%m-%d")``
    and run_daily_picks.py defaults its --date to exactly that. zoneinfo is
    the stdlib spelling of the same boundary.
    """
    tz = ZoneInfo(zone_name or "America/Toronto")
    return datetime.datetime.now(tz).date().isoformat()


def resolve_pick_date(event):
    """Pick-date for this kick: explicit ``event.date`` wins.

    The invoking pipeline (KickMLPicks at the end of the state machine run)
    passes the pick date it actually published for, so a warm-Lambda delay
    can never shift the slate. The Toronto-today fallback is ONLY for an
    owner manual invoke with no date at all - schedule_run no longer
    dates-stamps anything, and the state machine always passes one.
    """
    from_event = (event or {}).get("date")
    if isinstance(from_event, str) and from_event.strip():
        return from_event.strip()
    return pick_date_now()


def _post_dispatch(payload, token, owner, repo):
    """POST one repository_dispatch; returns (ok, status_code_or_None)."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        # Success is an empty 204; read-and-discard keeps urllib happy.
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
            return True, response.status
    except urllib.error.HTTPError as exc:
        return False, exc.code
    except urllib.error.URLError as exc:
        logger.error(f"GitHub dispatch network error: {exc}")
        return False, None


def relay_game_kick(event):
    """Relay one per-game pipeline-finish kick into the ml-picks workflow.

    Args:
        event: the invoking state's (or a manual) payload. Recognized keys:
            ``date`` (pick-date YYYY-MM-DD, always set by the KickMLPicks
            state) and ``dry`` (bool, smoke-run the pass).

    Returns:
        dict: outcome summary, e.g. {"dispatched": True, ...}.

    Raises:
        MissingDispatchTokenError: when GITHUB_DISPATCH_TOKEN is unset -
            a silently dropped kick would masquerade as "nothing fired",
            so this fails loudly instead.
        RuntimeError: when both POST attempts fail (transient-then-transient).
    """
    token = os.environ.get("GITHUB_DISPATCH_TOKEN")
    if not token:
        logger.error(
            "GITHUB_DISPATCH_TOKEN is empty - cannot relay ml-picks-game; "
            "set the CloudFormation GITHUB_DISPATCH_TOKEN parameter "
            "(fine-grained PAT, Actions write, this repo only)."
        )
        raise MissingDispatchTokenError("GITHUB_DISPATCH_TOKEN env var is missing or empty")

    owner = os.environ.get("GITHUB_OWNER", DEFAULT_GITHUB_OWNER)
    repo = os.environ.get("GITHUB_REPO", DEFAULT_GITHUB_REPO)

    # ML_KICK_DRY lets prod force smoke runs without touching payloads.
    dry = bool((event or {}).get("dry", False)) or os.environ.get("ML_KICK_DRY", "").lower() in {"1", "true"}

    dispatch_payload = {
        "event_type": DISPATCH_EVENT_TYPE,
        "client_payload": {
            "date": resolve_pick_date(event),
            "dry": dry,
            "context": KICK_CONTEXT,
        },
    }

    ok, status = _post_dispatch(dispatch_payload, token, owner, repo)
    if not ok and status in TRANSIENT_STATUS_CODES:
        logger.warning(f"Transient GitHub dispatch failure ({status}); retrying in {RETRY_BACKOFF_SECONDS}s")
        time.sleep(RETRY_BACKOFF_SECONDS)
        ok, status = _post_dispatch(dispatch_payload, token, owner, repo)

    if not ok:
        logger.error(f"GitHub repository_dispatch failed for {owner}/{repo} (status: {status})")
        raise RuntimeError(f"GitHub repository_dispatch failed (owner={owner}, repo={repo}, status={status})")

    logger.info(f"ml-picks-game dispatched to {owner}/{repo} payload={dispatch_payload['client_payload']}")
    return {"statusCode": 200, "dispatched": True, "client_payload": dispatch_payload["client_payload"]}
