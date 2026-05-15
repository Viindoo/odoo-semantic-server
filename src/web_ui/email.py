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
