"""Unit tests for the MLPicksKick relay (smartscore/ml_picks_kick.py).

The relay performs exactly ONE GitHub repository_dispatch per pipeline kick
(the KickMLPicks state at the end of the PlayerProcessingPipeline run).
These tests mock urllib entirely - no network, no Supabase, no real AWS -
and pin down: the exact POST body/URL (owner/repo, event_type,
client_payload), year-free ordering of the payload keys, the single
immediate retry on transient 5xx/429, the fail-loud missing-token path, and
that an explicit event date is used verbatim (no ET-today derivation when
the pipeline passes one). docs/ml-picks-runbook.md ("Per-game kick").
"""

import re
from unittest.mock import patch

import pytest

import ml_picks_kick
from ml_picks_kick import (
    DEFAULT_GITHUB_OWNER,
    DEFAULT_GITHUB_REPO,
    DISPATCH_EVENT_TYPE,
    KICK_CONTEXT,
    MissingDispatchTokenError,
    relay_game_kick,
    resolve_pick_date,
)

DISPATCH_URL = f"https://api.github.com/repos/{DEFAULT_GITHUB_OWNER}/{DEFAULT_GITHUB_REPO}/dispatches"


class _FakeResponse:
    """Empty 204, the success shape of repository_dispatch."""

    status = 204

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(status):
    return ml_picks_kick.urllib.error.HTTPError(url=DISPATCH_URL, code=status, msg="error", hdrs=None, fp=None)


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "gh_pat_test_token")
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.delenv("ML_KICK_DRY", raising=False)


def _sent_request(mock_urlopen):
    assert mock_urlopen.call_count >= 1
    request = mock_urlopen.call_args[0][0]
    return request, mock_urlopen


def test_exact_post_body_and_target(mock_dispatch, token_env):
    relay_game_kick({"date": "2026-03-14"})
    request, _ = _sent_request(mock_dispatch)

    # owner/repo resolution: api.github.com/repos/<owner>/<repo>/dispatches
    assert request.full_url == DISPATCH_URL
    assert request.get_method() == "POST"

    body = ml_picks_kick.json.loads(request.data.decode("utf-8"))
    assert request.headers["Authorization"] == "Bearer gh_pat_test_token"
    assert body["event_type"] == DISPATCH_EVENT_TYPE
    assert body["client_payload"] == {
        "date": "2026-03-14",  # the explicit date the KickMLPicks state passed
        "dry": False,
        "context": KICK_CONTEXT,
    }


def test_explicit_date_is_used_verbatim_without_any_date_derivation(mock_dispatch, token_env):
    """An explicit event.date must fully replace the ET slate derivation.

    The date used to be stamped by schedule_run into the EventBridge target
    Input and re-derived from Toronto-today as a fallback; now the pipeline
    passes its own published date, so when it is present the wall clock is
    never consulted at all.
    """
    with patch("ml_picks_kick.datetime.datetime") as mock_now:
        relay_game_kick({"date": "2026-01-14"})
        mock_now.now.assert_not_called()

    request, _ = _sent_request(mock_dispatch)
    body = ml_picks_kick.json.loads(request.data.decode("utf-8"))
    assert body["client_payload"]["date"] == "2026-01-14"


def test_payload_ordering_is_year_free(mock_dispatch, token_env):
    """Key order is stable and no year/month stamping creeps in.

    The payload carries only the slate date - never a created_at/happened
    timestamp - so re-runs on one date produce byte-identical dispatches
    regardless of the year they happen in.
    """
    relay_game_kick({"date": "2026-03-14"})
    request, _ = _sent_request(mock_dispatch)

    body = ml_picks_kick.json.loads(request.data.decode("utf-8"))
    assert list(body) == ["event_type", "client_payload"]
    assert list(body["client_payload"]) == ["date", "dry", "context"]

    # Byte-stability rule: only the slate date travels (no wall-clock
    # timestamp), so re-kicks of one date dispatch identical bodies.
    date = body["client_payload"]["date"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
    assert f'{{"date": "{date}", "dry":' in request.data.decode("utf-8")


@pytest.mark.parametrize("status", sorted(ml_picks_kick.TRANSIENT_STATUS_CODES))
def test_transient_failure_retries_once(status, token_env, monkeypatch):
    monkeypatch.setattr(ml_picks_kick.time, "sleep", lambda seconds: None)
    with patch("ml_picks_kick.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            _http_error(status),
            _FakeResponse(),
        ]
        result = relay_game_kick({"date": "2026-03-14"})

    assert mock_urlopen.call_count == 2  # one immediate retry, then success
    assert result["dispatched"] is True


def test_permanent_failure_is_not_retried(token_env, monkeypatch):
    monkeypatch.setattr(ml_picks_kick.time, "sleep", lambda seconds: None)
    with patch("ml_picks_kick.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [_http_error(403)]
        with pytest.raises(RuntimeError, match="repository_dispatch failed"):
            relay_game_kick({"date": "2026-03-14"})

    assert mock_urlopen.call_count == 1  # 4xx is deterministic: no retry


def test_missing_token_fails_loudly_before_any_http(monkeypatch):
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    with patch("ml_picks_kick.urllib.request.urlopen") as mock_urlopen:
        with pytest.raises(MissingDispatchTokenError):
            relay_game_kick({"date": "2026-03-14"})

    assert mock_urlopen.call_count == 0  # nothing silently dropped either


def test_owner_repo_env_overrides(token_env, monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", "someone-else")
    monkeypatch.setenv("GITHUB_REPO", "OtherRepo")
    with patch("ml_picks_kick.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _FakeResponse()
        relay_game_kick({"date": "2026-03-14"})

    request = mock_urlopen.call_args[0][0]
    assert request.full_url == "https://api.github.com/repos/someone-else/OtherRepo/dispatches"


def test_ml_kick_dry_env_forces_dry(token_env, monkeypatch):
    monkeypatch.setenv("ML_KICK_DRY", "true")
    with patch("ml_picks_kick.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _FakeResponse()
        relay_game_kick({"date": "2026-03-14"})

    request = mock_urlopen.call_args[0][0]
    body = ml_picks_kick.json.loads(request.data.decode("utf-8"))
    assert body["client_payload"]["dry"] is True


def test_resolve_pick_date_falls_back_to_toronto_today():
    # Manual owner invoke without a date: mirror the pipeline's ET-today
    # notion (service.get_date). This fallback exists ONLY for manual mode -
    # the state machine always passes an explicit date.
    with patch("ml_picks_kick.datetime.datetime") as mock_now:
        mock_now.now.return_value.date.return_value.isoformat.return_value = "2026-07-04"
        assert resolve_pick_date({}) == "2026-07-04"
        mock_now.now.assert_called_once()
        (tz_arg,), _ = mock_now.now.call_args
        assert str(tz_arg) == "America/Toronto"


@pytest.fixture
def mock_dispatch():
    """The happy-path urlopen stub: one empty 204."""
    with patch("ml_picks_kick.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _FakeResponse()
        yield mock_urlopen
