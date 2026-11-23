import json
from urllib.parse import parse_qs, urlsplit

from cerbere_cohort.email import (
    DEFAULT_APP_URL,
    EmailDeliveryError,
    build_workspace_invitation_url,
    send_workspace_invitation_email,
)


def test_build_invitation_url_uses_app_route_and_preserves_other_query(monkeypatch):
    monkeypatch.setenv("COHORT_APP_URL", "https://cohort.example/app?source=admin")
    url = build_workspace_invitation_url("secret-token")
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "cohort.example"
    assert parsed.path == "/app"
    assert parse_qs(parsed.query) == {"source": ["admin"], "invite": ["secret-token"]}


def test_build_invitation_url_uses_cohort_production_default(monkeypatch):
    monkeypatch.delenv("COHORT_APP_URL", raising=False)
    assert build_workspace_invitation_url("abc").startswith(DEFAULT_APP_URL + "?invite=abc")


def test_build_invitation_url_rejects_non_https(monkeypatch):
    monkeypatch.setenv("COHORT_APP_URL", "http://cohort.example/app")
    try:
        build_workspace_invitation_url("abc")
    except EmailDeliveryError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("Non-HTTPS invitation URL must be rejected")


def test_send_workspace_invitation_email_posts_branded_html_and_text(monkeypatch):
    import cerbere_cohort.email as email

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "Cohort <hello@example.com>")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"id":"email_test_123"}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(email.urllib.request, "urlopen", fake_urlopen)
    email_id = send_workspace_invitation_email(
        to_email="member@example.com",
        invite_url="https://cohort.example/app?invite=token123",
        role="member",
        workspace_name="Research Team",
    )

    assert email_id == "email_test_123"
    assert captured["url"] == email.RESEND_API_URL
    assert captured["timeout"] == 10
    assert captured["payload"]["from"] == "Cohort <hello@example.com>"
    assert captured["payload"]["to"] == ["member@example.com"]
    assert "Research Team" in captured["payload"]["html"]
    assert "https://cohort.example/app?invite=token123" in captured["payload"]["html"]
    assert "https://cohort.example/app?invite=token123" in captured["payload"]["text"]


def test_send_workspace_invitation_email_requires_api_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    try:
        send_workspace_invitation_email(
            to_email="member@example.com",
            invite_url="https://cohort.example/app?invite=token",
            role="member",
            workspace_name="Research Team",
        )
    except EmailDeliveryError as exc:
        assert "RESEND_API_KEY" in str(exc)
    else:
        raise AssertionError("Missing Resend API key must fail closed")
