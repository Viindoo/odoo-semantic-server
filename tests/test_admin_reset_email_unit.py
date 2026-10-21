# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin-issued password reset mail goes through the shared mailer - no DB.

Contract (``src/web_ui/routes/admin_users.py::_send_reset_email``):

* SMTP configured: one mail to the user with the reset link, sent by
  ``src.mailer`` (the one SMTP implementation of OSM: its sender, STARTTLS
  and envelope), each socket operation bounded; returns True;
* SMTP not configured: nothing is sent, the link is logged at WARNING so the
  admin can hand it over, returns False;
* a delivery failure is logged and returns False (the route reports
  ``email_sent: false``), never raises.
"""
from __future__ import annotations

import logging
import smtplib

import pytest

from src import mailer
from src.web_ui.routes import admin_users

LINK = "https://osm.example.com/reset-password?token=abc123"


class _WireSMTP:
    """``smtplib.SMTP`` at the socket boundary: the REAL ``send_message``
    flattens the message and calls ``sendmail``, recorded here."""

    last: _WireSMTP | None = None

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.tls = False
        self.login_as = None
        self.envelope = None
        self.wire = b""
        _WireSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_as = user

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, opt):
        return False

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        self.from_addr = from_addr
        self.envelope = list(to_addrs)
        self.wire = msg if isinstance(msg, bytes) else msg.encode()
        return {}

    send_message = smtplib.SMTP.send_message


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setenv("SMTP_PORT", "2587")
    monkeypatch.setenv("SMTP_USER", "AKIATEST")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_FROM", "noreply@osm.example.com")
    _WireSMTP.last = None
    monkeypatch.setattr(mailer.smtplib, "SMTP", _WireSMTP)
    return _WireSMTP


def test_the_reset_mail_reaches_the_user_with_the_link(smtp):
    assert admin_users._send_reset_email(
        to_email="frank@example.com", username="frank", reset_link=LINK,
    ) is True
    conn = smtp.last
    assert (conn.host, conn.port, conn.tls, conn.login_as) == (
        "smtp.test.invalid", 2587, True, "AKIATEST",
    )
    assert conn.kwargs.get("timeout") == admin_users._RESET_EMAIL_TIMEOUT_S
    assert conn.envelope == ["frank@example.com"]
    assert conn.from_addr == "noreply@osm.example.com"
    wire = conn.wire.decode()
    assert "Subject: Reset your odoo-semantic-mcp password" in wire
    assert "To: frank@example.com" in wire
    assert LINK in wire
    assert "Hello frank," in wire


def test_the_reset_mail_is_sent_by_the_shared_mailer(smtp, monkeypatch):
    """SSOT: no SMTP code of its own - the one sender is src.mailer.send."""
    calls = []
    monkeypatch.setattr(mailer, "send", lambda msg, **kw: calls.append((msg, kw)) or True)
    assert admin_users._send_reset_email(
        to_email="frank@example.com", username="frank", reset_link=LINK,
    ) is True
    assert len(calls) == 1
    assert calls[0][0]["To"] == "frank@example.com"
    assert smtp.last is None  # nothing opened a connection besides the mailer


def test_without_smtp_the_link_is_logged_and_nothing_is_sent(monkeypatch, caplog):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setattr(mailer.smtplib, "SMTP", _WireSMTP)
    _WireSMTP.last = None
    with caplog.at_level(logging.WARNING, logger=admin_users._logger.name):
        assert admin_users._send_reset_email(
            to_email="frank@example.com", username="frank", reset_link=LINK,
        ) is False
    assert _WireSMTP.last is None
    assert any(LINK in r.getMessage() for r in caplog.records)


def test_a_delivery_failure_returns_false_and_is_logged(smtp, monkeypatch, caplog):
    def refuse(self, *a, **kw):
        raise smtplib.SMTPRecipientsRefused({"frank@example.com": (550, b"no")})

    monkeypatch.setattr(_WireSMTP, "sendmail", refuse)
    with caplog.at_level(logging.ERROR, logger=admin_users._logger.name):
        assert admin_users._send_reset_email(
            to_email="frank@example.com", username="frank", reset_link=LINK,
        ) is False
    assert any("SMTP send failed" in r.getMessage() for r in caplog.records)
