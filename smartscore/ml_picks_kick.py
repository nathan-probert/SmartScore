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

#: 403 = token reached GitHub but was rejected for permissions (scope miss),
#: not a bad-credentials problem (that is 401). Worth a fingerprint + hint.
PERMISSION_DENIED_STATUS = 403


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


def _http_error_body(exc):
    """Best-effort read of an HTTPError's response body (TA: never a black box).

    GitHub's 403/422 bodies are the ONLY way to tell token-scope rejections
    ("Resource not accessible by integration") apart from settings-level
    blocks from inside the Lambda, so the body must survive into the logs.
    Test doubles may hand us an fp-less HTTPError, hence the guard.
    """
    try:
        return (exc.read() or b"").decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 - diagnostics only, must never mask the status
        return ""


def _post_dispatch(payload, token, owner, repo):
    """POST one repository_dispatch; returns (ok, status_code_or_None, body)."""
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
            return True, response.status, ""
    except urllib.error.HTTPError as exc:
        return False, exc.code, _http_error_body(exc)
    except urllib.error.URLError as exc:
        logger.error(f"GitHub dispatch network error: {exc}")
        return False, None, ""


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
            "(fine-grained PAT with Actions AND Contents write, this repo only, "
            "or a classic PAT with the repo scope)."
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

    ok, status, body = _post_dispatch(dispatch_payload, token, owner, repo)
    if not ok and status in TRANSIENT_STATUS_CODES:
        body_note = f" response_body={body}" if body else ""
        logger.warning(f"Transient GitHub dispatch failure ({status}){body_note}; retrying in {RETRY_BACKOFF_SECONDS}s")
        time.sleep(RETRY_BACKOFF_SECONDS)
        ok, status, body = _post_dispatch(dispatch_payload, token, owner, repo)

    if not ok:
        detail = f"GitHub response body: {body}" if body else "GitHub response body: <empty>"
        logger.error(f"GitHub repository_dispatch failed for {owner}/{repo} (status: {status}) - {detail}")
        hint = ""
        if status == PERMISSION_DENIED_STATUS:
            hint = (
                "403 rejection (e.g. 'Resource not accessible by integration') means the token reached "
                "GitHub but lacks permission: a fine-grained PAT needs BOTH Actions: Read & write AND "
                "Contents: Read & write on this repo (Actions: write alone is the common miss); a classic "
                "PAT with the repo scope is the simpler alternative. Not expiry (that is 401)."
            )
        raise RuntimeError(
            f"GitHub repository_dispatch failed (owner={owner}, repo={repo}, status={status}) - {detail}"
            + (f" HINT: {hint}" if hint else "")
        )

    logger.info(f"ml-picks-game dispatched to {owner}/{repo} payload={dispatch_payload['client_payload']}")
    return {"statusCode": 200, "dispatched": True, "client_payload": dispatch_payload["client_payload"]}
