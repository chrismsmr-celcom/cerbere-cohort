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


def test_smtp_delivery_uses_resend_ssl_and_branded_message(monkeypatch):
    import cerbere_cohort.email as email

    monkeypatch.setenv("RESEND_SMTP_HOST", "smtp.resend.com")
    monkeypatch.setenv("RESEND_SMTP_PORT", "465")
    monkeypatch.setenv("RESEND_SMTP_USERNAME", "resend")
    monkeypatch.setenv("RESEND_SMTP_PASSWORD", "re_test_secret")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "Cohort <hello@example.com>")
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout, context):
            captured.update(host=host, port=port, timeout=timeout, context=context)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            captured["ehlo"] = True

        def login(self, username, password):
            captured["username"] = username
            captured["password"] = password

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr(email.smtplib, "SMTP_SSL", FakeSMTP)
    message_id = send_workspace_invitation_email(
        to_email="member@example.com",
        invite_url="https://cohort.example/app?invite=token123",
        role="member",
        workspace_name="Research Team",
    )

    assert captured["host"] == "smtp.resend.com"
    assert captured["port"] == 465
    assert captured["timeout"] == 15
    assert captured["username"] == "resend"
    assert captured["password"] == "re_test_secret"
    assert captured["message"]["From"] == "Cohort <hello@example.com>"
    assert captured["message"]["To"] == "member@example.com"
    assert "Research Team" in captured["message"].get_body(preferencelist=("html",)).get_content()
    assert message_id == captured["message"]["Message-ID"]


def test_smtp_delivery_uses_starttls_for_port_587(monkeypatch):
    import cerbere_cohort.email as email

    monkeypatch.setenv("RESEND_SMTP_PORT", "587")
    monkeypatch.setenv("RESEND_SMTP_PASSWORD", "re_test_secret")
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            captured.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            captured["ehlo"] = captured.get("ehlo", 0) + 1

        def starttls(self, context):
            captured["starttls"] = context

        def login(self, username, password):
            captured["login"] = (username, password)

        def send_message(self, message):
            captured["sent"] = True

    monkeypatch.setattr(email.smtplib, "SMTP", FakeSMTP)
    send_workspace_invitation_email(
        to_email="member@example.com",
        invite_url="https://cohort.example/app?invite=token123",
        role="member",
        workspace_name="Research Team",
    )
    assert captured["port"] == 587
    assert captured["ehlo"] == 2
    assert "starttls" in captured
    assert captured["login"] == ("resend", "re_test_secret")
    assert captured["sent"] is True


def test_smtp_delivery_requires_password(monkeypatch):
    monkeypatch.delenv("RESEND_SMTP_PASSWORD", raising=False)
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


def test_smtp_delivery_rejects_invalid_port(monkeypatch):
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



def test_smtp_delivery_reuses_existing_resend_api_key(monkeypatch):
    import cerbere_cohort.email as email

    monkeypatch.delenv("RESEND_SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_existing_api_key")
    monkeypatch.setenv("RESEND_SMTP_PORT", "465")
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout, context):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def login(self, username, password):
            captured["credentials"] = (username, password)

        def send_message(self, message):
            pass

    monkeypatch.setattr(email.smtplib, "SMTP_SSL", FakeSMTP)
    assert email.smtp_is_configured() is True
    send_workspace_invitation_email(
        to_email="member@example.com",
        invite_url="https://cohort.example/app?invite=token123",
        role="member",
        workspace_name="Research Team",
    )
    assert captured["credentials"] == ("resend", "re_existing_api_key")
