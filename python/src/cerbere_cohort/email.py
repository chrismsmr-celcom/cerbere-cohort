"""Transactional workspace invitation email delivery through Resend."""

from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request


RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM_EMAIL = "Cohort <hello@cerbereag.site>"
DEFAULT_APP_URL = "https://cohort.cerbereag.site/app"


class EmailDeliveryError(RuntimeError):
    """Raised when a transactional email cannot be delivered."""


def resend_is_configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY", "").strip())


def build_workspace_invitation_url(token: str) -> str:
    app_url = os.getenv("COHORT_APP_URL", DEFAULT_APP_URL).strip() or DEFAULT_APP_URL
    parts = urllib.parse.urlsplit(app_url)
    if parts.scheme != "https" or not parts.netloc:
        raise EmailDeliveryError("COHORT_APP_URL must be an absolute HTTPS URL")
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "invite"]
    query.append(("invite", token))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path or "/app", urllib.parse.urlencode(query), parts.fragment)
    )


def send_workspace_invitation_email(
    *,
    to_email: str,
    invite_url: str,
    role: str,
    workspace_name: str,
) -> str | None:
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        raise EmailDeliveryError("RESEND_API_KEY is not configured")

    from_email = os.getenv("RESEND_FROM_EMAIL", DEFAULT_FROM_EMAIL).strip()
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
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    request = urllib.request.Request(
        RESEND_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise EmailDeliveryError(f"Resend rejected the invitation email (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EmailDeliveryError(f"Resend delivery request failed ({type(exc).__name__})") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EmailDeliveryError("Resend returned an invalid response") from None

    email_id = result.get("id") if isinstance(result, dict) else None
    return str(email_id) if email_id else None
