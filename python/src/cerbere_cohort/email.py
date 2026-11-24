"""Transactional workspace invitation email delivery through Resend SMTP."""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid


DEFAULT_SMTP_HOST = "smtp.resend.com"
DEFAULT_SMTP_PORT = 465
DEFAULT_SMTP_USERNAME = "resend"
DEFAULT_FROM_EMAIL = "Cohort <hello@cerbereag.site>"
DEFAULT_APP_URL = "https://cohort.cerbereag.site/app"


class EmailDeliveryError(RuntimeError):
    """Raised when a transactional email cannot be delivered."""


def smtp_is_configured() -> bool:
    return bool((os.getenv("RESEND_SMTP_PASSWORD") or os.getenv("RESEND_API_KEY") or "").strip())


def build_workspace_invitation_url(token: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    app_url = os.getenv("COHORT_APP_URL", DEFAULT_APP_URL).strip() or DEFAULT_APP_URL
    parts = urlsplit(app_url)
    if parts.scheme != "https" or not parts.netloc:
        raise EmailDeliveryError("COHORT_APP_URL must be an absolute HTTPS URL")
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "invite"]
    query.append(("invite", token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/app", urlencode(query), parts.fragment))


def send_workspace_invitation_email(
    *,
    to_email: str,
    invite_url: str,
    role: str,
    workspace_name: str,
) -> str | None:
    host = os.getenv("RESEND_SMTP_HOST", DEFAULT_SMTP_HOST).strip() or DEFAULT_SMTP_HOST
    try:
        port = int(os.getenv("RESEND_SMTP_PORT", str(DEFAULT_SMTP_PORT)).strip())
    except ValueError:
        raise EmailDeliveryError("RESEND_SMTP_PORT must be a valid port number") from None
    username = os.getenv("RESEND_SMTP_USERNAME", DEFAULT_SMTP_USERNAME).strip() or DEFAULT_SMTP_USERNAME
    # Resend's SMTP password is the API key. Prefer an explicitly named SMTP
    # secret, but reuse the existing RESEND_API_KEY to avoid duplicate secrets.
    password = (os.getenv("RESEND_SMTP_PASSWORD") or os.getenv("RESEND_API_KEY") or "").strip()
    from_email = os.getenv("RESEND_FROM_EMAIL", DEFAULT_FROM_EMAIL).strip()

    if not password:
        raise EmailDeliveryError("Set RESEND_SMTP_PASSWORD or RESEND_API_KEY to the Resend API key")
    if not 1 <= port <= 65535:
        raise EmailDeliveryError("RESEND_SMTP_PORT must be between 1 and 65535")
    if not from_email:
        raise EmailDeliveryError("RESEND_FROM_EMAIL is not configured")

    safe_workspace = html.escape(workspace_name or "your Cohort workspace")
    safe_role = html.escape(role.title())
    safe_url = html.escape(invite_url, quote=True)
    subject = "You're invited to join a Cohort workspace"
    html_body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:32px 16px;background:#f4f6fa;font-family:Arial,Helvetica,sans-serif;color:#172033;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
    <tr><td style="padding:30px 36px 22px;">
      <div style="font-size:23px;font-weight:700;letter-spacing:-.8px;color:#172033;">Cohort<span style="color:#635bff;">.</span></div>
    </td></tr>
    <tr><td style="padding:16px 36px 38px;">
      <p style="margin:0 0 12px;font-size:12px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;color:#635bff;">Workspace invitation</p>
      <h1 style="margin:0 0 16px;font-size:27px;line-height:1.3;letter-spacing:-.7px;">You've been invited.</h1>
      <p style="margin:0 0 12px;font-size:15px;line-height:1.7;color:#667085;">You've been invited to join <strong style="color:#172033;">{safe_workspace}</strong> on Cohort as a {safe_role}.</p>
      <p style="margin:0 0 28px;font-size:15px;line-height:1.7;color:#667085;">Accept the invitation below to join your team and start coordinating your AI agents.</p>
      <table role="presentation" cellspacing="0" cellpadding="0"><tr><td bgcolor="#635bff" style="border-radius:7px;">
        <a href="{safe_url}" target="_blank" style="display:inline-block;padding:14px 24px;color:#fff;text-decoration:none;font-size:14px;font-weight:600;">Accept invitation</a>
      </td></tr></table>
      <p style="margin:28px 0 8px;font-size:13px;line-height:1.6;color:#667085;">If the button doesn't work, copy this link into your browser:</p>
      <p style="margin:0;font-size:12px;line-height:1.6;word-break:break-all;"><a href="{safe_url}" style="color:#635bff;">{safe_url}</a></p>
      <p style="margin:28px 0 0;font-size:13px;line-height:1.6;color:#98a2b3;">This invitation expires automatically. If you weren't expecting it, you can ignore this email.</p>
    </td></tr>
    <tr><td style="padding:22px 36px;background:#f9fafc;border-top:1px solid #eaecf0;">
      <p style="margin:0;font-size:12px;line-height:1.6;color:#98a2b3;">Cohort · Coordination infrastructure for AI agents.</p>
      <p style="margin:6px 0 0;font-size:12px;color:#98a2b3;">© 2026 Cohort. All rights reserved.</p>
    </td></tr>
  </table>
</body></html>"""
    text_body = (
        f"You've been invited to join {workspace_name or 'a Cohort workspace'} "
        f"on Cohort as a {role}.\n\n"
        f"Accept your invitation: {invite_url}\n\n"
        "This invitation expires automatically. If you weren't expecting it, ignore this email."
    )
    message = EmailMessage()
    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="cerbereag.site")
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    try:
        if port in {465, 2465}:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as server:
                server.ehlo()
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(username, password)
                server.send_message(message)
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        # Do not expose credentials or the full SMTP response in logs/API errors.
        raise EmailDeliveryError(f"Resend SMTP delivery failed ({type(exc).__name__})") from None

    return message["Message-ID"]
