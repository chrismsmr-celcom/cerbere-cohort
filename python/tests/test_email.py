from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock

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


def test_django_backend_sends_resend_email_over_alternate_ssl_port(monkeypatch):
    import cerbere_cohort.email as email

    monkeypatch.setenv("RESEND_SMTP_HOST", "smtp.resend.com")
    monkeypatch.setenv("RESEND_SMTP_PORT", "2465")
    monkeypatch.setenv("RESEND_SMTP_USERNAME", "resend")
    monkeypatch.setenv("RESEND_SMTP_PASSWORD", "re_test_secret")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "Cohort <hello@example.com>")
    connection = MagicMock()
    connection.send_messages.return_value = 1
    get_connection = MagicMock(return_value=connection)
    monkeypatch.setattr(email, "get_connection", get_connection)

    message_id = send_workspace_invitation_email(
        to_email="member@example.com",
        invite_url="https://cohort.example/app?invite=token123",
        role="member",
        workspace_name="Research Team",
    )

    kwargs = get_connection.call_args.kwargs
    assert kwargs["backend"] == "django.core.mail.backends.smtp.EmailBackend"
    assert kwargs["host"] == "smtp.resend.com"
    assert kwargs["port"] == 2465
    assert kwargs["username"] == "resend"
    assert kwargs["password"] == "re_test_secret"
    assert kwargs["use_ssl"] is True
    assert kwargs["use_tls"] is False
    sent_message = connection.send_messages.call_args.args[0][0]
    assert sent_message.to == ["member@example.com"]
    assert sent_message.from_email == "Cohort <hello@example.com>"
    assert sent_message.subject == "You're invited to join a Cohort workspace"
    assert "Research Team" in sent_message.alternatives[0][0]
    assert message_id == sent_message.extra_headers["Message-ID"]


def test_django_backend_uses_starttls_for_resend_alternate_tls_port(monkeypatch):
    import cerbere_cohort.email as email

    monkeypatch.setenv("RESEND_SMTP_PORT", "2587")
    monkeypatch.setenv("RESEND_SMTP_PASSWORD", "re_test_secret")
    connection = MagicMock()
    connection.send_messages.return_value = 1
    get_connection = MagicMock(return_value=connection)
    monkeypatch.setattr(email, "get_connection", get_connection)

    send_workspace_invitation_email(
        to_email="member@example.com",
        invite_url="https://cohort.example/app?invite=token123",
        role="member",
        workspace_name="Research Team",
    )
    kwargs = get_connection.call_args.kwargs
    assert kwargs["port"] == 2587
    assert kwargs["use_ssl"] is False
    assert kwargs["use_tls"] is True


def test_django_backend_requires_password(monkeypatch):
    monkeypatch.delenv("RESEND_SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    try:
        send_workspace_invitation_email(
            to_email="member@example.com",
            invite_url="https://cohort.example/app?invite=token",
            role="member",
            workspace_name="Research Team",
        )
    except EmailDeliveryError as exc:
        assert "RESEND_SMTP_PASSWORD" in str(exc)
    else:
        raise AssertionError("Missing SMTP password must fail closed")


def test_django_backend_rejects_invalid_port(monkeypatch):
    monkeypatch.setenv("RESEND_SMTP_PORT", "not-a-port")
    monkeypatch.setenv("RESEND_SMTP_PASSWORD", "re_test_secret")
    try:
        send_workspace_invitation_email(
            to_email="member@example.com",
            invite_url="https://cohort.example/app?invite=token",
            role="member",
            workspace_name="Research Team",
        )
    except EmailDeliveryError as exc:
        assert "port" in str(exc).lower()
    else:
        raise AssertionError("Invalid SMTP port must fail closed")


def test_django_backend_reuses_existing_resend_api_key(monkeypatch):
    import cerbere_cohort.email as email

    monkeypatch.delenv("RESEND_SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_existing_api_key")
    connection = MagicMock()
    connection.send_messages.return_value = 1
    get_connection = MagicMock(return_value=connection)
    monkeypatch.setattr(email, "get_connection", get_connection)
    send_workspace_invitation_email(
        to_email="member@example.com",
        invite_url="https://cohort.example/app?invite=token123",
        role="member",
        workspace_name="Research Team",
    )
    assert get_connection.call_args.kwargs["password"] == "re_existing_api_key"
