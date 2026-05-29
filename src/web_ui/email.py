# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/email.py
"""Email helpers for account verification and password reset (M9 W-SG).

In dev mode (SMTP_HOST unset) all emails are logged at INFO level — no SMTP
required for local development or unit tests.

Usage:
    send_verification_email(to, username, token, base_url)
    send_password_reset_email(to, username, token, base_url)
    send_waitlist_notify_email(submitter_email, plan)
"""

import logging
import os
import smtplib
from email.message import EmailMessage
from html import escape

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand constants
# ---------------------------------------------------------------------------
# SSOT for brand colours is site/tailwind.config.mjs (viindoo.*) — kept in sync
# here because email HTML cannot read the Tailwind theme. Update both on a
# brand refresh.
_BRAND_CYAN = "#00BBCE"
_BRAND_DARK = "#07131A"
_BRAND_TEXT = "#282F33"
_BRAND_MUTED = "#6B6D70"
_LOGO_URL = "https://odoo-semantic.viindoo.com/logo-email.png"
_FONT_STACK = "'Segoe UI', Roboto, Arial, sans-serif"


# ---------------------------------------------------------------------------
# SMTP helpers (unchanged)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HTML brand helpers
# ---------------------------------------------------------------------------


def _logo_url_for(base_url: str | None) -> str:
    """Resolve the absolute logo URL for an email.

    Emails have no request origin, so the logo MUST be an absolute URL. When a
    caller knows the deployment origin (``base_url``) we derive the logo from it
    so self-hosted/staging deploys point at their own host; otherwise we fall
    back to the canonical production asset.
    """
    if base_url:
        return f"{base_url.rstrip('/')}/logo-email.png"
    # Emails without a request origin (e.g. admin waitlist notify) still honour
    # a deployment-wide PUBLIC_BASE_URL so self-hosted instances don't hot-link
    # the canonical production asset; fall back to it only as a last resort.
    env_base = os.getenv("PUBLIC_BASE_URL")
    if env_base:
        return f"{env_base.rstrip('/')}/logo-email.png"
    return _LOGO_URL


def _email_header_html(logo_url: str, home_url: str) -> str:
    """Return the branded header band (cyan background + white logo).

    Uses table-based layout for maximum email-client compatibility.
    Logo is an absolute-URL PNG (SVG stripped by most clients). ``logo_url`` and
    ``home_url`` are server-controlled (never user input).
    """
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0"'
        f' style="background-color:{_BRAND_CYAN};">'
        "<tr>"
        '<td align="center" style="padding:20px 24px;">'
        f'<a href="{home_url}" style="text-decoration:none;">'
        f'<img src="{logo_url}" alt="Viindoo" height="40"'
        ' style="display:block;border:0;outline:none;" />'
        "</a>"
        "</td>"
        "</tr>"
        "</table>"
    )


def _email_wrapper(body_html: str, title: str, base_url: str | None = None) -> str:
    """Wrap body_html in a full branded HTML email document.

    Applies:
    - Outer reset table (full-width, light grey background)
    - Max-600px centred content card (white)
    - Branded header (cyan band + Viindoo logo)
    - Body content area with brand typography
    - Footer with copyright line

    Args:
        body_html: Pre-escaped inner HTML string (paragraphs, tables, etc.).
        title:     Used in the <title> tag (not visible in most clients but
                   useful for accessibility and pre-header text indexing).
        base_url:  Deployment origin; the logo URL is derived from it when given
                   (falls back to the canonical production asset otherwise).

    Returns:
        Complete HTML string ready to pass to ``msg.add_alternative(..., subtype='html')``.
    """
    logo_url = _logo_url_for(base_url)
    home_url = base_url.rstrip("/") if base_url else "https://odoo-semantic.viindoo.com"
    header = _email_header_html(logo_url, home_url)
    safe_title = escape(title)
    return (
        "<!DOCTYPE html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{safe_title}</title>"
        "</head>"
        "<body"
        ' style="margin:0;padding:0;background-color:#F4F6F8;'
        f'font-family:{_FONT_STACK};color:{_BRAND_TEXT};">'
        # Outer wrapper table
        '<table width="100%" cellpadding="0" cellspacing="0" border="0"'
        ' style="background-color:#F4F6F8;">'
        "<tr>"
        '<td align="center" style="padding:32px 16px;">'
        # Content card
        '<table width="600" cellpadding="0" cellspacing="0" border="0"'
        ' style="max-width:600px;width:100%;background-color:#FFFFFF;'
        'border-radius:8px;overflow:hidden;'
        'box-shadow:0 2px 8px rgba(0,0,0,0.08);">'
        # Header band
        "<tr>"
        f"<td>{header}</td>"
        "</tr>"
        # Body content
        "<tr>"
        '<td style="padding:32px 40px 24px;font-size:15px;line-height:1.6;'
        f'color:{_BRAND_TEXT};">'
        f"{body_html}"
        "</td>"
        "</tr>"
        # Footer
        "<tr>"
        '<td style="padding:16px 40px 28px;border-top:1px solid #E8EAED;'
        f'font-size:12px;color:{_BRAND_MUTED};text-align:center;">'
        "&copy; Viindoo &mdash; Powering Your Business Growth"
        "</td>"
        "</tr>"
        "</table>"
        # /content card
        "</td>"
        "</tr>"
        "</table>"
        # /outer wrapper
        "</body>"
        "</html>"
    )


def _cta_button_html(href: str, label: str) -> str:
    """Return an email-safe inline-styled CTA button.

    Args:
        href:  The pre-escaped URL string.
        label: Button label text (HTML-escaped here defensively).
    """
    safe_label = escape(label)
    return (
        '<table cellpadding="0" cellspacing="0" border="0"'
        ' style="margin:24px 0;">'
        "<tr>"
        "<td"
        f' style="background-color:{_BRAND_CYAN};border-radius:6px;">'
        f'<a href="{href}"'
        ' style="display:inline-block;padding:12px 28px;'
        f"color:#FFFFFF;font-family:{_FONT_STACK};font-size:15px;"
        'font-weight:600;text-decoration:none;border-radius:6px;">'
        f"{safe_label}"
        "</a>"
        "</td>"
        "</tr>"
        "</table>"
    )


# ---------------------------------------------------------------------------
# Public email senders
# ---------------------------------------------------------------------------


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

    # Plain-text body (unchanged)
    msg.set_content(
        f"Hi {username},\n\n"
        f"Click the link below to verify your email address:\n"
        f"{link}\n\n"
        f"The link expires in 24 hours.\n\n"
        f"If you did not create an account, you can safely ignore this email."
    )

    # HTML alternative — user input ALWAYS escaped; no str.format() (a brace in
    # escaped user data would raise KeyError), brand colours via f-string.
    body = (
        f"<p>Hi {safe_username},</p>"
        "<p>Thanks for signing up! Please verify your email address to activate"
        " your account.</p>"
        f"{_cta_button_html(safe_link, 'Verify Email Address')}"
        "<p>The link expires in 24 hours.</p>"
        f"<p style=\"color:{_BRAND_MUTED};font-size:13px;\">If you did not create an account,"
        " you can safely ignore this email.</p>"
        f"<p style=\"color:{_BRAND_MUTED};font-size:13px;\">Or copy and paste this URL into"
        f" your browser:<br><a href=\"{safe_link}\" style=\"color:{_BRAND_CYAN};\">"
        f"{safe_link}</a></p>"
    )
    msg.add_alternative(
        _email_wrapper(body, "Verify your Odoo Semantic MCP account", base_url),
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
    body = (
        "<p>A new user joined the waitlist.</p>"
        '<table cellpadding="6" cellspacing="0" border="0"'
        ' style="border-collapse:collapse;font-size:14px;">'
        f"<tr><td style=\"padding-right:16px;\"><strong>Email</strong></td>"
        f"<td>{safe_email}</td></tr>"
        f"<tr><td><strong>Plan</strong></td>"
        f"<td>{escape(plan_display)}</td></tr>"
        f"<tr><td><strong>Source</strong></td>"
        f"<td>{escape(source)}</td></tr>"
        f"<tr><td><strong>Time</strong></td>"
        f"<td>{now_utc}</td></tr>"
        "</table>"
        f"<p style=\"color:{_BRAND_MUTED};font-size:13px;margin-top:24px;\">"
        "<em>Odoo Semantic MCP (automated)</em></p>"
    )
    msg.add_alternative(
        _email_wrapper(body, f"[Waitlist] New signup: {submitter_email}"),
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

    # Plain-text body (unchanged)
    msg.set_content(
        f"Hi {username},\n\n"
        f"Click the link below to reset your password:\n"
        f"{link}\n\n"
        f"The link expires in 1 hour.\n\n"
        f"If you did not request a password reset, you can safely ignore this email."
    )

    # HTML alternative — user input ALWAYS escaped; no str.format() (a brace in
    # escaped user data would raise KeyError), brand colours via f-string.
    body = (
        f"<p>Hi {safe_username},</p>"
        "<p>We received a request to reset your Odoo Semantic MCP password.</p>"
        f"{_cta_button_html(safe_link, 'Reset Password')}"
        "<p>The link expires in 1 hour.</p>"
        f"<p style=\"color:{_BRAND_MUTED};font-size:13px;\">If you did not request a password"
        " reset, you can safely ignore this email. Your password will not change.</p>"
        f"<p style=\"color:{_BRAND_MUTED};font-size:13px;\">Or copy and paste this URL into"
        f" your browser:<br><a href=\"{safe_link}\" style=\"color:{_BRAND_CYAN};\">"
        f"{safe_link}</a></p>"
    )
    msg.add_alternative(
        _email_wrapper(body, "Reset your Odoo Semantic MCP password", base_url),
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
