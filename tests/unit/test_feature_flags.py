import posthog

import feature_flags


def _reset():
    feature_flags.invalidate_cache()


class _FakeFlags:
    def __init__(self, values):
        self._values = values

    def is_enabled(self, key):
        return self._values.get(key, False)


def test_env_flag_precedence_over_posthog(monkeypatch):
    """A set FEATURE_<NAME> env var wins without contacting PostHog."""
    _reset()
    monkeypatch.setenv("FEATURE_SEND_EMAILS", "true")
    monkeypatch.setattr(feature_flags, "POSTHOG_FEATURE_FLAG_KEY", "phc_test")

    def fail(*args, **kwargs):
        raise AssertionError("PostHog should not be contacted when env var is set")

    monkeypatch.setattr(posthog, "evaluate_flags", fail)
    assert feature_flags.is_feature_enabled("send_emails") is True


def test_env_flag_uppercase_mapping(monkeypatch):
    """FEATURE_NHL_MOCK_MODE maps to is_feature_enabled('nhl_mock_mode')."""
    _reset()
    monkeypatch.setenv("FEATURE_NHL_MOCK_MODE", "1")
    assert feature_flags.is_feature_enabled("nhl_mock_mode") is True


def test_env_flag_off_values(monkeypatch):
    _reset()
    monkeypatch.setenv("FEATURE_NHL_MOCK_MODE", "false")
    assert feature_flags.is_feature_enabled("nhl_mock_mode") is False


def test_no_config_defaults_to_false(monkeypatch):
    """Without a PostHog key or env var, the flag defaults to False."""
    _reset()
    monkeypatch.delenv("FEATURE_NHL_MOCK_MODE", raising=False)
    monkeypatch.setattr(feature_flags, "POSTHOG_FEATURE_FLAG_KEY", None)
    assert feature_flags.is_feature_enabled("nhl_mock_mode") is False


def test_posthog_decide_roundtrip(monkeypatch):
    """When no env var is set, PostHog evaluate_flags is consulted."""
    _reset()
    monkeypatch.delenv("FEATURE_NHL_MOCK_MODE", raising=False)
    monkeypatch.setattr(feature_flags, "POSTHOG_FEATURE_FLAG_KEY", "phc_test")

    calls = {"count": 0}

    def fake_evaluate_flags(distinct_id, person_properties=None, flag_keys=None):
        calls["count"] += 1
        return _FakeFlags({"mock-nhl-api": True})

    monkeypatch.setattr(posthog, "evaluate_flags", fake_evaluate_flags)

    assert feature_flags.is_feature_enabled("mock-nhl-api") is True
    assert calls["count"] == 1


def test_posthog_ttl_cache(monkeypatch):
    """Repeated reads within the TTL window only hit PostHog once."""
    _reset()
    monkeypatch.delenv("FEATURE_SEND_EMAILS", raising=False)
    monkeypatch.setattr(feature_flags, "POSTHOG_FEATURE_FLAG_KEY", "phc_test")

    calls = {"count": 0}

    def fake_evaluate_flags(distinct_id, person_properties=None, flag_keys=None):
        calls["count"] += 1
        return _FakeFlags({"send_emails": False})

    monkeypatch.setattr(posthog, "evaluate_flags", fake_evaluate_flags)

    feature_flags.is_feature_enabled("send_emails")
    feature_flags.is_feature_enabled("send_emails")
    assert calls["count"] == 1


def test_posthog_network_failure_falls_back(monkeypatch):
    """A PostHog outage must not break the app; falls back to False."""
    _reset()
    monkeypatch.delenv("FEATURE_SEND_EMAILS", raising=False)
    monkeypatch.setattr(feature_flags, "POSTHOG_FEATURE_FLAG_KEY", "phc_test")

    def fake_evaluate_flags(distinct_id, person_properties=None, flag_keys=None):
        raise OSError("network down")

    monkeypatch.setattr(posthog, "evaluate_flags", fake_evaluate_flags)

    assert feature_flags.is_feature_enabled("send_emails") is False
