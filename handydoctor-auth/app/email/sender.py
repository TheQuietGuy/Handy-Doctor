"""
app/email/sender.py — Transactional email via Resend.

All email functions are fire-and-forget coroutines called with
asyncio.create_task() in the router so they don't block the response.
Swap the _send() implementation for SendGrid / SES / SMTP with no
changes needed in the router code.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"


async def _send(*, to: str, subject: str, html: str) -> None:
    """Low-level send — raises on HTTP error so callers can log."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": f"{settings.email_from_name} <{settings.email_from}>",
                "to": [to],
                "subject": subject,
                "html": html,
            },
        )
        resp.raise_for_status()


# ── Templates ────────────────────────────────────────────────────────────────
def _base_template(title: str, body_html: str) -> str:
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f4f4f4; margin: 0; padding: 0; }}
    .wrapper {{ max-width: 560px; margin: 40px auto; background: #fff;
                border-radius: 12px; overflow: hidden;
                box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
    .header {{ background: #0A1628; padding: 28px 36px;
               font-family: Georgia, serif; color: #D4A017;
               font-size: 1.3rem; font-weight: 700; }}
    .body   {{ padding: 32px 36px; color: #1A1A1A; line-height: 1.6; }}
    .btn    {{ display: inline-block; margin: 20px 0;
               background: #D4A017; color: #0A1628;
               text-decoration: none; padding: 12px 28px;
               border-radius: 8px; font-weight: 700; font-size: 0.9rem; }}
    .footer {{ padding: 20px 36px; font-size: 0.72rem; color: #888;
               border-top: 1px solid #eee; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">🤚 HandyDoctor</div>
    <div class="body">{body_html}</div>
    <div class="footer">
      HandyDoctor is not a substitute for professional medical advice.<br>
      &copy; {settings.app_name}. All rights reserved.
    </div>
  </div>
</body>
</html>
"""


# ── Public send functions ─────────────────────────────────────────────────────
async def send_verification_email(to: str, full_name: str, token: str) -> None:
    link = f"{settings.frontend_url}/#verify&token={token}"
    body = f"""
    <p>Hi {full_name},</p>
    <p>Thanks for creating a HandyDoctor account. Please verify your email address
       by clicking the button below. The link expires in
       <strong>{settings.email_verification_expire_hours} hours</strong>.</p>
    <a class="btn" href="{link}">Verify my email</a>
    <p>If you didn't sign up for HandyDoctor, you can safely ignore this email.</p>
    """
    try:
        await _send(
            to=to,
            subject="Verify your HandyDoctor email address",
            html=_base_template("Email verification", body),
        )
    except Exception:
        logger.exception("Failed to send verification email to %s", to)


async def send_email_change_email(to: str, full_name: str, token: str) -> None:
    link = f"{settings.frontend_url}/#change-email&token={token}"
    body = f"""
    <p>Hi {full_name},</p>
    <p>We received a request to change the email address on your HandyDoctor account.
       Click the button below to confirm your new address.
       The link expires in <strong>{settings.email_verification_expire_hours} hours</strong>.</p>
    <a class="btn" href="{link}">Confirm new email</a>
    <p>If you did not request this change, please
       <a href="{settings.frontend_url}/security">secure your account</a> immediately.</p>
    """
    try:
        await _send(
            to=to,
            subject="Confirm your new HandyDoctor email address",
            html=_base_template("Email change confirmation", body),
        )
    except Exception:
        logger.exception("Failed to send email-change email to %s", to)


async def send_password_reset_email(to: str, full_name: str, token: str) -> None:
    link = f"{settings.frontend_url}/#reset&token={token}"
    body = f"""
    <p>Hi {full_name},</p>
    <p>We received a request to reset your HandyDoctor password.
       Click the button below — the link is valid for <strong>15 minutes</strong>.</p>
    <a class="btn" href="{link}">Reset my password</a>
    <p>If you didn't request a password reset, you can safely ignore this email.
       Your password will not change.</p>
    """
    try:
        await _send(
            to=to,
            subject="Reset your HandyDoctor password",
            html=_base_template("Password reset", body),
        )
    except Exception:
        logger.exception("Failed to send password reset email to %s", to)


async def send_deletion_confirmation_email(to: str, full_name: str, execute_after: str) -> None:
    cancel_link = f"{settings.frontend_url}/#account-settings&tab=danger"
    body = f"""
    <p>Hi {full_name},</p>
    <p>We've received a request to permanently delete your HandyDoctor account and all
       associated data in compliance with GDPR.</p>
    <p><strong>Your account will be deleted on {execute_after} UTC</strong>
       ({settings.account_deletion_grace_days}-day grace period).</p>
    <p>Changed your mind? You can cancel at any time before then:</p>
    <a class="btn" href="{cancel_link}">Cancel deletion</a>
    <p>After deletion, all personal data will be permanently erased. Anonymised,
       non-identifiable analytics data may be retained as permitted by GDPR Article 89.</p>
    """
    try:
        await _send(
            to=to,
            subject="Your HandyDoctor account deletion request",
            html=_base_template("Account deletion", body),
        )
    except Exception:
        logger.exception("Failed to send deletion email to %s", to)


async def send_data_export_email(
    to: str,
    full_name: str,
    json_payload: str,
    is_deletion_export: bool = False,
) -> None:
    """
    Sends the GDPR data export JSON as an inline attachment via Resend.
    For deletion exports the subject line makes the context clear.
    """
    subject = (
        "Your HandyDoctor data export (account deletion)"
        if is_deletion_export
        else "Your HandyDoctor data export"
    )
    intro = (
        "As part of your account deletion request, here is a complete copy of "
        "all personal data HandyDoctor held about you, in accordance with GDPR "
        "Article 17 (right to erasure) and Article 20 (data portability)."
        if is_deletion_export
        else "Here is a complete copy of all personal data HandyDoctor holds about you, "
        "in accordance with your request under GDPR Article 20 (right to data portability)."
    )
    body = f"""
    <p>Hi {full_name},</p>
    <p>{intro}</p>
    <p>Your data is attached as a JSON file below. It includes your account details,
       notification preferences, session history, and audit trail.</p>
    <p style="background:#F0F9FF;padding:12px 16px;border-radius:8px;font-size:.85rem">
      📎 <strong>handydoctor_data_export.json</strong> — attached below
    </p>
    <pre style="background:#1A1A1A;color:#D4D4D4;padding:16px;border-radius:8px;
                font-size:.7rem;overflow-x:auto;max-height:300px">{json_payload[:3000]}{"…" if len(json_payload) > 3000 else ""}</pre>
    <p style="font-size:.8rem;color:#888">
      If you did not request this export, please
      <a href="{settings.frontend_url}/security">review your account security</a>.
    </p>
    """
    try:
        await _send(
            to=to,
            subject=subject,
            html=_base_template("Data export", body),
        )
    except Exception:
        logger.exception("Failed to send data export email to %s", to)


async def send_deletion_complete_email(to: str, full_name: str) -> None:
    body = f"""
    <p>Hi {full_name},</p>
    <p>Your HandyDoctor account and all associated personal data have been
       <strong>permanently deleted</strong> in accordance with GDPR Article 17.</p>
    <p>What was deleted:</p>
    <ul style="font-size:.85rem;line-height:1.8">
      <li>Your account profile (name, email, date of birth, country, gender)</li>
      <li>All active sessions and authentication tokens</li>
      <li>Your notification preferences</li>
      <li>Any pending email verification or password reset tokens</li>
    </ul>
    <p>Anonymised, non-identifiable analytics data may be retained as permitted
       by GDPR Article 89. This data cannot be linked back to you.</p>
    <p>If you ever wish to use HandyDoctor again, you are welcome to create a new account.</p>
    """
    try:
        await _send(
            to=to,
            subject="Your HandyDoctor account has been deleted",
            html=_base_template("Account deleted", body),
        )
    except Exception:
        logger.exception("Failed to send deletion-complete email to %s", to)


async def send_notification_email(
    to: str,
    subject: str,
    html: str,
    plain: str,
) -> None:
    """
    Generic transactional email used by the notification worker.
    Unlike the other helpers this RAISES on failure so the worker
    can record status = 'failed' and retry later.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": f"{settings.email_from_name} <{settings.email_from}>",
                "to": [to],
                "subject": subject,
                "html": html,
                "text": plain,
            },
        )
        resp.raise_for_status()


async def send_security_alert_email(to: str, full_name: str, event: str) -> None:
    body = f"""
    <p>Hi {full_name},</p>
    <p>A security event occurred on your HandyDoctor account:</p>
    <p style="background:#FFF3CD;padding:12px 16px;border-radius:8px;font-weight:600">
      ⚠️ {event}
    </p>
    <p>If this was you, no action is needed. If you don't recognise this activity,
       please <a href="{settings.frontend_url}/security">review your active sessions</a>
       and change your password immediately.</p>
    """
    try:
        await _send(
            to=to,
            subject=f"HandyDoctor security alert: {event}",
            html=_base_template("Security alert", body),
        )
    except Exception:
        logger.exception("Failed to send security alert to %s", to)
