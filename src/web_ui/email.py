# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/email.py
"""Email helpers for account verification and password reset (M9 W-SG).

In dev mode (SMTP_HOST unset) all emails are logged at INFO level — no SMTP
required for local development or unit tests.

Usage:
    send_verification_email(to, username, token, base_url)
    send_password_reset_email(to, username, token, base_url)
"""

import logging
import os
import smtplib
from email.message import EmailMessage
from html import escape

logger = logging.getLogger(__name__)


def _smtp_host() -> str | None:
    """Return SMTP_HOST env var or None (dev-mode sentinel)."""
    return os.getenv("SMTP_HOST") or None


def _from_address() -> str:
    return os.getenv("SMTP_FROM", "noreply@odoo-semantic.viindoo.com")


def _send(msg: EmailMessage) -> None:
    """Dispatch an EmailMessage via SMTP (STARTTLS).

    Raises smtplib.SMTPException (or subclasses) on delivery failure.
    The caller is responsible for catching and logging.
    """
    host = _smtp_host()
    if not host:
        logger.info(
            "SMTP unset (dev mode) — email suppressed. To=%s Subject=%r",
            msg["To"],
            msg["Subject"],
        )
        return
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port) as srv:
        srv.starttls()
        smtp_user = os.getenv("SMTP_USER")
        if smtp_user:
            srv.login(smtp_user, os.getenv("SMTP_PASSWORD", ""))
        srv.send_message(msg)
    logger.info("Email sent to %s subject=%r", msg["To"], msg["Subject"])


def send_verification_email(to: str, username: str, token: str, base_url: str) -> None:
    """Send account-verification email.

    In dev mode (SMTP_HOST unset) the email content is logged at INFO level
    so developers can retrieve the token without running a real SMTP server.

    Args:
        to: Recipient email address.
        username: Display name for the greeting (HTML-escaped in body).
        token: 256-bit URL-safe token (secrets.token_urlsafe(32)).
        base_url: Public origin, e.g. ``https://odoo-semantic.viindoo.com``.
    """
    link = f"{base_url}/verify-email?token={token}"
    safe_username = escape(username)
    safe_link = escape(link)

    msg = EmailMessage()
    msg["Subject"] = "Verify your Odoo Semantic MCP account"
    msg["From"] = _from_address()
    msg["To"] = to

    # Plain-text body
    msg.set_content(
        f"Hi {username},\n\n"
        f"Click the link below to verify your email address:\n"
        f"{link}\n\n"
        f"The link expires in 24 hours.\n\n"
        f"If you did not create an account, you can safely ignore this email."
    )

    # HTML alternative — user input ALWAYS escaped
    msg.add_alternative(
        f"<p>Hi {safe_username},</p>"
        f"<p><a href='{safe_link}'>Verify your email address</a></p>"
        f"<p>The link expires in 24 hours.</p>"
        f"<p>If you did not create an account, you can safely ignore this email.</p>",
        subtype="html",
    )

    if not _smtp_host():
        logger.info(
            "DEV MODE — verification link for %s: %s",
            to,
            link,
        )
        return

    try:
        _send(msg)
    except Exception as exc:
        logger.error("Failed to send verification email to %s: %s", to, exc)
        raise


def send_waitlist_notify_email(
    submitter_email: str,
    plan: str | None,
    source: str = "pricing-page",
) -> bool:
    """Send admin notification when a new waitlist entry is created.

    Best-effort: returns False on any failure (caller logs a warning and
    continues — a failed notify must NOT roll back the DB insert).

    Args:
        submitter_email: The email address that joined the waitlist.
        plan:   The pricing tier they expressed interest in ('free'/'pro'/'team')
                or None for a generic signup.
        source: Origin tag, e.g. 'pricing-page' (stored in waitlist_emails.source).

    Returns:
        True on successful delivery (or dev-mode suppression).
        False on SMTP failure.
    """
    import datetime as _dt

    notify_to = os.getenv("WAITLIST_NOTIFY_EMAIL", "admin@viindoo.com")
    plan_display = plan or "(generic)"
    now_utc = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC")

    msg = EmailMessage()
    msg["Subject"] = f"[Waitlist] New signup: {submitter_email}"
    msg["From"] = _from_address()
    msg["To"] = notify_to

    body_text = (
        f"A new user joined the waitlist.\n\n"
        f"Email:  {submitter_email}\n"
        f"Plan:   {plan_display}\n"
        f"Source: {source}\n"
        f"Time:   {now_utc}\n\n"
        f"-- Odoo Semantic MCP (automated)"
    )
    msg.set_content(body_text)

    safe_email = escape(submitter_email)
    msg.add_alternative(
        f"<p>A new user joined the waitlist.</p>"
        f"<table>"
        f"<tr><td><strong>Email</strong></td><td>{safe_email}</td></tr>"
        f"<tr><td><strong>Plan</strong></td><td>{escape(plan_display)}</td></tr>"
        f"<tr><td><strong>Source</strong></td><td>{escape(source)}</td></tr>"
        f"<tr><td><strong>Time</strong></td><td>{now_utc}</td></tr>"
        f"</table>"
        f"<p><em>Odoo Semantic MCP (automated)</em></p>",
        subtype="html",
    )

    if not _smtp_host():
        logger.info(
            "DEV MODE — waitlist notify suppressed. submitter=%s plan=%s",
            submitter_email, plan_display,
        )
        return True

    try:
        _send(msg)
        return True
    except Exception as exc:
        logger.warning(
            "Failed to send waitlist notify email to %s: %s", notify_to, exc
        )
        return False


def send_password_reset_email(to: str, username: str, token: str, base_url: str) -> None:
    """Send password-reset email.

    Args:
        to: Recipient email address.
        username: Display name (HTML-escaped in body).
        token: 256-bit URL-safe reset token.
        base_url: Public origin for building the reset link.
    """
    link = f"{base_url}/reset-password?token={token}"
    safe_username = escape(username)
    safe_link = escape(link)

    msg = EmailMessage()
    msg["Subject"] = "Reset your Odoo Semantic MCP password"
    msg["From"] = _from_address()
    msg["To"] = to

    msg.set_content(
        f"Hi {username},\n\n"
        f"Click the link below to reset your password:\n"
        f"{link}\n\n"
        f"The link expires in 1 hour.\n\n"
        f"If you did not request a password reset, you can safely ignore this email."
    )

    msg.add_alternative(
        f"<p>Hi {safe_username},</p>"
        f"<p><a href='{safe_link}'>Reset your password</a></p>"
        f"<p>The link expires in 1 hour.</p>"
        f"<p>If you did not request a password reset, "
        f"you can safely ignore this email.</p>",
        subtype="html",
    )

    if not _smtp_host():
        logger.info(
            "DEV MODE — password reset link for %s: %s",
            to,
            link,
        )
        return

    try:
        _send(msg)
    except Exception as exc:
        logger.error("Failed to send password-reset email to %s: %s", to, exc)
        raise
