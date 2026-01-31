from utility import get_emails


def test_get_emails_returns_list():
    emails = get_emails()
    assert isinstance(emails, list)


def test_get_emails_only_notify_true(monkeypatch):
    # Patch the RPC call to return a controlled set
    class DummyResponse:
        data = [
            {"email": "user1@example.com"},
            {"email": "user2@example.com"},
        ]

    monkeypatch.setattr(
        "smartscore.utility.SUPABASE_ADMIN_AUTH_CLIENT",
        type(
            "DummyClient",
            (),
            {"rpc": lambda self, fn: type("DummyRPC", (), {"execute": lambda self: DummyResponse()})()},
        )(),
    )
    emails = get_emails()
    assert emails == ["user1@example.com", "user2@example.com"]


def test_get_emails_empty(monkeypatch):
    class DummyResponse:
        data = []

    monkeypatch.setattr(
        "smartscore.utility.SUPABASE_ADMIN_AUTH_CLIENT",
        type(
            "DummyClient",
            (),
            {"rpc": lambda self, fn: type("DummyRPC", (), {"execute": lambda self: DummyResponse()})()},
        )(),
    )
    emails = get_emails()
    assert emails == []
