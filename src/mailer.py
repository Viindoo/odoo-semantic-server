# SPDX-License-Identifier: AGPL-3.0-or-later
"""SMTP delivery shared by the Web UI mails and the ops alert hook (#383).

A leaf module (stdlib only): ``src.web_ui.email`` and ``src.ops.alert_notify``
both send through it, so neither layer imports the other.

Configuration (environment):
    SMTP_HOST       relay host; unset means "no SMTP" (see :func:`smtp_host`)
    SMTP_PORT       default 587 (STARTTLS)
    SMTP_USER       optional login
    SMTP_PASSWORD   password for SMTP_USER
    SMTP_FROM       sender address, default noreply@odoo-semantic.viindoo.com
"""
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import getaddresses

logger = logging.getLogger(__name__)

DEFAULT_FROM = "noreply@odoo-semantic.viindoo.com"


def smtp_host() -> str | None:
    """SMTP_HOST, or None when it is unset or empty (no relay configured)."""
    return os.getenv("SMTP_HOST") or None


def from_address() -> str:
    """The sender address (SMTP_FROM, else :data:`DEFAULT_FROM`)."""
    return os.getenv("SMTP_FROM", DEFAULT_FROM)


def envelope_recipients(msg: EmailMessage) -> list[str]:
    """Every address of the To, Cc and Bcc headers (the SMTP envelope).

    An empty group such as ``undisclosed-recipients:;`` contributes nothing:
    left to ``smtplib`` it would become an empty ``RCPT TO:<>``.
    """
    values = [v for header in ("To", "Cc", "Bcc") for v in (msg.get_all(header) or [])]
    return [addr for _name, addr in getaddresses(values) if addr]


def send(msg: EmailMessage, *, timeout: float | None = None) -> bool:
    """Deliver *msg* over SMTP with STARTTLS.

    The envelope is :func:`envelope_recipients`; a ``Bcc`` header is never
    transmitted (``smtplib.SMTP.send_message`` drops it), so a mail to many
    recipients can keep their addresses from each other.

    Returns True when the relay accepted the message, False when SMTP_HOST is
    unset (nothing is sent; logged at INFO). Raises ``smtplib.SMTPException``
    or ``OSError`` on a delivery failure: the caller decides how to report it.
    *timeout* (seconds) bounds every socket operation; None keeps the socket
    default (the Web UI behaviour before the module was shared).
    """
    rcpts = envelope_recipients(msg)
    host = smtp_host()
    if not host:
        logger.info(
            "SMTP unset (dev mode) - email suppressed. To=%s Subject=%r",
            ", ".join(rcpts),
            msg["Subject"],
        )
        return False
    port = int(os.getenv("SMTP_PORT", "587"))
    kwargs = {} if timeout is None else {"timeout": timeout}
    with smtplib.SMTP(host, port, **kwargs) as srv:
        srv.starttls()
        smtp_user = os.getenv("SMTP_USER")
        if smtp_user:
            srv.login(smtp_user, os.getenv("SMTP_PASSWORD", ""))
        srv.send_message(msg, to_addrs=rcpts)
    logger.info("Email sent to %d recipient(s) subject=%r", len(rcpts), msg["Subject"])
    return True
