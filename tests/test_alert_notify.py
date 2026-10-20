# SPDX-License-Identifier: AGPL-3.0-or-later
"""osm-alert@.service mails the admins about a failed unit (#383 item 1) - no DB.

Contract under test (src/ops/alert_notify.py):

* a failed unit produces ONE mail to every active Web UI admin, carrying the
  unit, host, exit status, the last 60 lines of THIS run's output and, for the
  nightly reindex, the ``Lifecycle needs attention`` block written by this run;
* the database being down does not lose the alert: the last known admin list
  (cached on each successful read) plus OSM_ALERT_FALLBACK_TO are used; with
  neither, nothing is sent and the journal says so loudly;
* the same alert (unit, exit status, attention block) is mailed once per 24h,
  and at most 6 mails go out per hour whatever the units;
* credentials in the log excerpt never leave the host;
* nothing the hook does can fail the alert unit (exit 0), and an SMTP failure
  is logged as DELIVERY_FAILED, never reported as sent.

systemctl / journalctl / PostgreSQL / SMTP are faked; nothing is sent.
"""
from __future__ import annotations

import json
import logging
import time

import pytest

from src.ops import alert_notify as an

NOW = 1_792_500_000.0
INV = "7cd219ba40a54613bccdf7929741e965"
UNIT = "odoo-semantic-reindex"

ATTENTION = [
    "Lifecycle needs attention (exit 3):",
    "  gates_tripped: repo id=31 (git@github.com:Viindoo/tvtmaaddons.git): "
    "entity_prune:viin_freight_mixed_container_patch1@17.0",
    "  gates_tripped: repo id=32 (git@github.com:Viindoo/erponline-enterprise.git): "
    "entity_prune:viin_ai@17.0",
]


def _log_lines(invocation=INV, attention=ATTENTION, extra=()):
    return [
        "osm-run-start 2026-10-19T20:30:00Z invocation=0000previousrun",
        *[
            "Lifecycle needs attention (exit 3):",
            "  gates_tripped: an OLD hold of the previous run",
        ],
        "Done: {'profiles_ok': 27}",
        f"osm-run-start 2026-10-20T20:30:00Z invocation={invocation}",
        "2026-10-20 03:30:08,287 WARNING something mild",
        "HEAD is now at e5de6ca8013 [FIX] something",
        *extra,
        *attention,
        "Done: {'profiles_ok': 27, 'profiles_failed': []}",
    ]


class FakeHost:
    """systemctl / journalctl answers and the mails that went out."""

    def __init__(self, tmp_path, *, log_lines=None, load_state="loaded", status="3",
                 code="1", journal=None):
        self.log = tmp_path / "odoo-semantic-reindex.log"
        if log_lines is not None:
            self.log.write_text("\n".join(log_lines) + "\n")
        self.load_state = load_state
        self.status = status
        self.code = code
        self.journal = journal
        self.calls: list[list[str]] = []
        self.sent: list = []
        self.admins: list[str] | Exception = ["admin1@example.com", "admin2@example.com"]
        self.smtp_error: Exception | None = None

    def run_cmd(self, args):
        self.calls.append(args)
        if args[:2] == ["systemctl", "show"]:
            return (
                f"LoadState={self.load_state}\nResult=exit-code\n"
                f"ExecMainCode={self.code}\nExecMainStatus={self.status}\n"
                f"InvocationID={INV}\nExecMainStartTimestamp=@{int(NOW) - 3600}\n"
            )
        if args[:2] == ["systemctl", "cat"]:
            if self.journal is not None:
                return "[Service]\nStandardOutput=journal\n"
            return (
                "# /etc/systemd/system/odoo-semantic-reindex.service\n[Service]\n"
                "StandardOutput=append:/nowhere/old.log\n"
                f"StandardOutput=append:{self.log}\nStandardError=append:{self.log}\n"
            )
        if args[0] == "journalctl":
            return "\n".join(self.journal or []) + "\n"
        return None

    def fetch(self):
        if isinstance(self.admins, Exception):
            raise self.admins
        return list(self.admins)

    def deliver(self, msg):
        if self.smtp_error is not None:
            raise self.smtp_error
        self.sent.append(msg)
        return True


@pytest.fixture
def host(tmp_path, monkeypatch):
    fake = FakeHost(tmp_path, log_lines=_log_lines())
    monkeypatch.setattr(an, "_run_cmd", fake.run_cmd)
    monkeypatch.setattr(an, "_fetch_admin_emails", fake.fetch)
    monkeypatch.setattr(an, "_deliver", fake.deliver)
    monkeypatch.setattr(an.socket, "gethostname", lambda: "osm-prod")
    monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
    return fake


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state"
    path.mkdir()
    return path


def _env(state, **extra):
    env = {
        "STATE_DIRECTORY": str(state),
        "MONITOR_SERVICE_RESULT": "exit-code",
        "MONITOR_EXIT_CODE": "exited",
        "MONITOR_EXIT_STATUS": "3",
        "MONITOR_INVOCATION_ID": INV,
    }
    env.update(extra)
    return env


def _body(msg) -> str:
    return msg.get_content()


# --- what the mail says -----------------------------------------------------

def test_an_exit_3_mail_names_unit_host_status_and_this_runs_attention_block(host, state):
    assert an.notify(UNIT, env=_env(state), now=NOW) == "sent"
    assert len(host.sent) == 1
    msg = host.sent[0]
    assert msg["To"] == "admin1@example.com, admin2@example.com"
    assert msg["Subject"] == "[OSM ALERT] osm-prod odoo-semantic-reindex.service exit 3"
    body = _body(msg)
    assert "odoo-semantic-reindex.service failed on osm-prod." in body
    assert "exit 3 - indexed, but the module lifecycle needs an operator" in body
    for line in ATTENTION:
        assert line in body
    assert "docs/deploy.md section 3.6" in body
    # Only this run: nothing from the previous invocation leaks in.
    assert "an OLD hold of the previous run" not in body
    assert "this run" in body


def test_the_tail_is_capped_to_the_last_60_lines(host, state, tmp_path):
    noise = [f"noise line {i}" for i in range(500)]
    host.log.write_text("\n".join(_log_lines(attention=[], extra=noise)) + "\n")
    an.notify(UNIT, env=_env(state), now=NOW)
    body = _body(host.sent[0])
    assert "noise line 499" in body
    assert "noise line 440" not in body  # 60 lines = 441..499 + the Done line
    assert "Last 60 lines of output" in body


def test_an_attention_block_of_an_earlier_run_is_not_reported_as_this_runs(host, state):
    # This run crashed before writing its own block: the block in the file is old.
    host.log.write_text("\n".join([
        *ATTENTION,
        "Done: {}",
        f"osm-run-start 2026-10-20T20:30:00Z invocation={INV}",
        "Traceback (most recent call last):",
        "RuntimeError: neo4j unreachable",
    ]) + "\n")
    an.notify(UNIT, env=_env(state, MONITOR_EXIT_STATUS="1"), now=NOW)
    body = _body(host.sent[0])
    assert "RuntimeError: neo4j unreachable" in body
    assert ATTENTION[1] not in body


def test_extract_attention_block_takes_the_last_block_and_stops_at_dedent():
    lines = ["x", *ATTENTION, "Done: {}", "y"]
    assert an.extract_attention_block(lines) == ATTENTION
    assert an.extract_attention_block(["no block here"]) == []


def test_a_journal_unit_reads_the_journal_of_that_invocation(host, state, tmp_path):
    host.journal = ["-- Boot abc --", "2026-10-20T03:30:07 osm-prod python[1]: boom"]
    an.notify("odoo-semantic-mcp", env=_env(state, MONITOR_EXIT_STATUS="1"), now=NOW)
    journal_calls = [c for c in host.calls if c[0] == "journalctl"]
    assert journal_calls and f"_SYSTEMD_INVOCATION_ID={INV}" in journal_calls[0]
    assert "-u" in journal_calls[0] and "odoo-semantic-mcp.service" in journal_calls[0]
    body = _body(host.sent[0])
    assert "boom" in body and "-- Boot abc --" not in body


def test_a_drop_in_that_sends_output_to_the_journal_wins(monkeypatch):
    text = ("[Service]\nStandardOutput=append:/var/log/x.log\n"
            "# /etc/systemd/system/x.service.d/override.conf\n[Service]\n"
            "StandardOutput=journal\nStandardError=journal\n")
    monkeypatch.setattr(an, "_run_cmd", lambda args: text)
    assert an.unit_log_file("x.service") is None
    monkeypatch.setattr(an, "_run_cmd", lambda args: "StandardError=file:/var/log/e.log\n")
    assert str(an.unit_log_file("x.service")) == "/var/log/e.log"


def test_without_monitor_env_the_status_comes_from_systemctl_show(host, state):
    host.status = "1"
    an.notify(UNIT, env={"STATE_DIRECTORY": str(state)}, now=NOW)
    assert host.sent[0]["Subject"].endswith("exit 1")
    assert "the run crashed or a repo or profile failed to index" in _body(host.sent[0])


@pytest.mark.parametrize("given", [
    "odoo-semantic-reindex", "odoo-semantic-reindex.service",
    "odoo-semantic-reindex.service.service",
])
def test_every_instance_spelling_names_the_same_unit(given):
    assert an.normalize_unit(given) == "odoo-semantic-reindex.service"


def test_the_body_is_capped(host, state):
    huge = ["x" * 2000 for _ in range(200)]
    host.log.write_text("\n".join(_log_lines(attention=[], extra=huge)) + "\n")
    an.notify(UNIT, env=_env(state), now=NOW)
    body = _body(host.sent[0])
    assert len(body) <= an._MAX_BODY_CHARS + 100
    assert "x" * (an._MAX_LINE_CHARS + 1) not in body


# --- secrets never leave the host -------------------------------------------

def test_credentials_in_the_log_excerpt_are_redacted(host, state):
    secrets = [
        "PG_DSN=postgresql://odoo_semantic:hunter2pw@db:5432/odoo_semantic",
        "connect postgresql://osm_reader:Rd3rSecret@127.0.0.1/x failed",
        "SMTP_PASSWORD=sesSmtpSecret42",
        "neo4j password: 'n4jSecret'",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.tokpart",
        "key osm_AbCdEf0123456789XyZ used",
        "aws AKIAABCDEFGHIJKLMNOP leaked",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "b3BlbnNzaC1rZXktdjEAAAAA",
        "-----END OPENSSH PRIVATE KEY-----",
    ]
    host.log.write_text("\n".join(_log_lines(attention=[], extra=secrets)) + "\n")
    an.notify(UNIT, env=_env(state), now=NOW)
    body = _body(host.sent[0])
    for leaked in ("hunter2pw", "Rd3rSecret", "sesSmtpSecret42", "n4jSecret",
                   "eyJhbGciOiJIUzI1NiJ9", "AbCdEf0123456789XyZ", "ABCDEFGHIJKLMNOP",
                   "b3BlbnNzaC1rZXktdjEAAAAA"):
        assert leaked not in body, leaked
    assert "postgresql://odoo_semantic:***@db:5432" in body  # the context stays


# --- recipients -------------------------------------------------------------

def test_a_successful_db_read_refreshes_the_admin_cache(host, state):
    an.notify(UNIT, env=_env(state), now=NOW)
    cached = json.loads((state / "admins.json").read_text())
    assert cached["emails"] == ["admin1@example.com", "admin2@example.com"]
    assert (state / "admins.json").stat().st_mode & 0o077 == 0


def test_db_down_mails_the_last_known_admins_plus_the_configured_fallback(host, state, caplog):
    (state / "admins.json").write_text(json.dumps({"emails": ["cached@example.com"]}))
    host.admins = RuntimeError("connection refused")
    env = _env(state, OSM_ALERT_FALLBACK_TO="ops@example.com, cached@example.com")
    with caplog.at_level(logging.ERROR, logger="osm-alert"):
        assert an.notify(UNIT, env=env, now=NOW) == "sent"
    assert host.sent[0]["To"] == "cached@example.com, ops@example.com"
    assert "connection refused" in _body(host.sent[0])  # the mail says why
    assert any("not readable from the database" in r.getMessage() for r in caplog.records)


def test_db_down_and_no_cache_uses_the_env_fallback(host, state):
    host.admins = RuntimeError("PG_DSN is not set")
    an.notify(UNIT, env=_env(state, OSM_ALERT_FALLBACK_TO="ops@example.com"), now=NOW)
    assert host.sent[0]["To"] == "ops@example.com"


def test_no_admin_email_in_the_db_falls_back_too(host, state):
    host.admins = []
    an.notify(UNIT, env=_env(state, OSM_ALERT_FALLBACK_TO="ops@example.com"), now=NOW)
    assert host.sent[0]["To"] == "ops@example.com"


def test_no_recipient_at_all_sends_nothing_and_logs_loudly(host, state, caplog):
    host.admins = RuntimeError("down")
    with caplog.at_level(logging.ERROR, logger="osm-alert"):
        assert an.notify(UNIT, env=_env(state), now=NOW) == "no-recipient"
    assert host.sent == []
    assert any("NO RECIPIENT" in r.getMessage() for r in caplog.records)


# --- dedupe and rate cap ----------------------------------------------------

def test_the_same_alert_is_mailed_once_per_24h_and_again_after(host, state, caplog):
    env = _env(state)
    assert an.notify(UNIT, env=env, now=NOW) == "sent"
    with caplog.at_level(logging.WARNING, logger="osm-alert"):
        assert an.notify(UNIT, env=env, now=NOW + 23 * 3600) == "suppressed"
    assert any("suppressed" in r.getMessage() for r in caplog.records)
    assert an.notify(UNIT, env=env, now=NOW + 24 * 3600 + 1) == "sent"
    assert len(host.sent) == 2


def test_a_changed_attention_block_is_mailed_at_once(host, state):
    env = _env(state)
    an.notify(UNIT, env=env, now=NOW)
    changed = [*ATTENTION, "  gates_tripped: repo id=40 (x.git): entity_prune:new@18.0"]
    host.log.write_text("\n".join(_log_lines(attention=changed)) + "\n")
    assert an.notify(UNIT, env=env, now=NOW + 60) == "sent"
    assert len(host.sent) == 2


def test_another_exit_status_is_mailed_at_once(host, state):
    an.notify(UNIT, env=_env(state), now=NOW)
    host.log.write_text("\n".join(_log_lines(attention=[])) + "\n")
    assert an.notify(UNIT, env=_env(state, MONITOR_EXIT_STATUS="1"), now=NOW + 60) == "sent"


def test_at_most_6_mails_an_hour_over_all_units(host, state):
    outcomes = [
        an.notify(f"unit-{i}", env=_env(state, MONITOR_UNIT=f"unit-{i}"), now=NOW + i)
        for i in range(8)
    ]
    assert outcomes.count("sent") == 6
    assert outcomes[6:] == ["suppressed", "suppressed"]
    assert an.notify("unit-9", env=_env(state, MONITOR_UNIT="unit-9"),
                     now=NOW + 3601) == "sent"


def test_force_bypasses_dedupe(host, state):
    env = _env(state)
    an.notify(UNIT, env=env, now=NOW)
    assert an.notify(UNIT, env=env, now=NOW + 60, force=True) == "sent"


def test_the_dummy_smoke_test_always_mails_but_obeys_the_cap(host, state):
    host.load_state = "not-found"
    env = {"STATE_DIRECTORY": str(state)}  # manual start: no MONITOR_*
    assert an.notify("dummy", env=env, now=NOW) == "sent"
    assert an.notify("dummy", env=env, now=NOW + 5) == "sent"  # no 24h dedupe
    assert "test alert" in host.sent[0]["Subject"]
    assert "does not exist" in _body(host.sent[0])
    for i in range(10):
        an.notify("dummy", env=env, now=NOW + 10 + i)
    assert len(host.sent) == 6


def test_a_failed_delivery_is_not_recorded_so_the_next_failure_retries(host, state):
    host.smtp_error = OSError("network unreachable")
    assert an.notify(UNIT, env=_env(state), now=NOW) == "delivery-failed"
    host.smtp_error = None
    assert an.notify(UNIT, env=_env(state), now=NOW + 60) == "sent"


# --- the hook never fails its unit ------------------------------------------

def test_smtp_failure_is_logged_and_main_exits_0(host, state, monkeypatch, caplog):
    host.smtp_error = OSError("SES unreachable")
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    for k, v in _env(state).items():
        monkeypatch.setenv(k, v)
    with caplog.at_level(logging.ERROR, logger="osm-alert"):
        assert an.main(["--unit", UNIT]) == 0
    assert any("DELIVERY_FAILED" in r.getMessage() and "SES unreachable" in r.getMessage()
               for r in caplog.records)


def test_smtp_host_unset_is_not_reported_as_sent(host, state, monkeypatch, caplog):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with caplog.at_level(logging.ERROR, logger="osm-alert"):
        assert an.notify(UNIT, env=_env(state), now=NOW) == "no-smtp"
    assert host.sent == []
    assert any("SMTP_HOST unset" in r.getMessage() for r in caplog.records)


def test_main_exits_0_on_bad_arguments_and_unexpected_errors(monkeypatch):
    assert an.main([]) == 0
    monkeypatch.setattr(an, "notify", lambda *a, **k: 1 / 0)
    assert an.main(["--unit", "x"]) == 0


def test_dry_run_prints_and_neither_sends_nor_records(host, state, capsys):
    assert an.notify(UNIT, env=_env(state), now=NOW, dry_run=True) == "dry-run"
    assert host.sent == []
    assert not (state / "state.json").exists()
    assert not (state / "admins.json").exists()
    assert "[OSM ALERT]" in capsys.readouterr().out


def test_the_real_mailer_is_called_with_a_timeout(monkeypatch):
    seen = {}

    def fake_send(msg, *, timeout=None):
        seen["timeout"] = timeout
        return True

    monkeypatch.setattr(an.mailer, "send", fake_send)
    an._deliver(an.EmailMessage())
    assert seen["timeout"] == an.SMTP_TIMEOUT_S


def test_state_dir_falls_back_without_systemd(monkeypatch, tmp_path):
    monkeypatch.setattr(an.Path, "home", lambda: tmp_path)
    assert an.state_dir_from_env({}) == tmp_path / ".local" / "state" / "osm-alert"
    assert an.state_dir_from_env({"OSM_ALERT_STATE_DIR": str(tmp_path / "s")}) == tmp_path / "s"


def test_time_is_real_time_by_default(host, state):
    before = time.time()
    an.notify(UNIT, env=_env(state))
    sent_at = json.loads((state / "state.json").read_text())["history"][0]
    assert before <= sent_at <= time.time()
