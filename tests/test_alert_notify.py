# SPDX-License-Identifier: AGPL-3.0-or-later
"""osm-alert@.service mails the admins about a failed unit (#383 item 1) - no DB.

Contract under test (src/ops/alert_notify.py):

* a failed unit produces ONE mail to every active Web UI admin - in Bcc, so
  no admin's address reaches another admin - carrying the
  unit, host, exit status, the last 60 lines of THIS run's output and, for the
  nightly reindex, the ``Lifecycle needs attention`` block written by this run;
* the database being down does not lose the alert: the last known admin list
  (cached on each successful read) plus OSM_ALERT_FALLBACK_TO are used; with
  neither, nothing is sent and the journal says so loudly;
* the same alert (unit, exit status, attention block - or the normalized
  cause line without one) is mailed once per 20h, so a failure that persists
  every night mails every night; at most 6 mails go out per hour whatever the
  units, and what the cap holds is listed in the next mail;
* credentials in the log excerpt never leave the host, even when a line is
  truncated or a key block is cut by the 60-line window;
* the hook finishes inside its unit's TimeoutStartSec= (bounded lock wait,
  bounded send, the lock never held while mailing);
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
        self.start_pre = ""  # extra `systemctl show` lines (ExecStartPre=...)
        self.on_deliver = None

    def run_cmd(self, args):
        self.calls.append(args)
        if args[:2] == ["systemctl", "show"]:
            return (
                f"LoadState={self.load_state}\nResult=exit-code\n"
                f"ExecMainCode={self.code}\nExecMainStatus={self.status}\n"
                f"InvocationID={INV}\nExecMainStartTimestamp=@{int(NOW) - 3600}\n"
                + self.start_pre
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
        if self.on_deliver is not None:
            self.on_deliver(msg)
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
    assert msg["To"] == "undisclosed-recipients:;"
    assert msg["Bcc"] == "admin1@example.com, admin2@example.com"
    assert msg["Subject"] == "[OSM ALERT] osm-prod odoo-semantic-reindex.service exit 3"
    body = _body(msg)
    assert "odoo-semantic-reindex.service failed on osm-prod." in body
    assert (
        "exit 3 - indexed, but OSM no longer reflects part of the code"
        " and an operator must act"
    ) in body
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
        # Fake key, split so secret scanners do not flag test data.
        "aws AKIA" "ABCDEFGHIJKLMNOP leaked",
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
    (state / "admins.json").write_text(json.dumps(
        {"emails": ["cached@example.com"], "updated_at": NOW - 3 * 86400}))
    host.admins = RuntimeError("connection refused")
    env = _env(state, OSM_ALERT_FALLBACK_TO="ops@example.com, cached@example.com")
    with caplog.at_level(logging.ERROR, logger="osm-alert"):
        assert an.notify(UNIT, env=env, now=NOW) == "sent"
    assert host.sent[0]["Bcc"] == "cached@example.com, ops@example.com"
    assert "connection refused" in _body(host.sent[0])  # the mail says why
    assert any("not readable from the database" in r.getMessage() for r in caplog.records)


def test_db_down_and_no_cache_uses_the_env_fallback(host, state):
    host.admins = RuntimeError("PG_DSN is not set")
    an.notify(UNIT, env=_env(state, OSM_ALERT_FALLBACK_TO="ops@example.com"), now=NOW)
    assert host.sent[0]["Bcc"] == "ops@example.com"


def test_no_admin_email_in_the_db_falls_back_too(host, state):
    host.admins = []
    an.notify(UNIT, env=_env(state, OSM_ALERT_FALLBACK_TO="ops@example.com"), now=NOW)
    assert host.sent[0]["Bcc"] == "ops@example.com"


def test_no_recipient_at_all_sends_nothing_and_logs_loudly(host, state, caplog):
    host.admins = RuntimeError("down")
    with caplog.at_level(logging.ERROR, logger="osm-alert"):
        assert an.notify(UNIT, env=_env(state), now=NOW) == "no-recipient"
    assert host.sent == []
    assert any("NO RECIPIENT" in r.getMessage() for r in caplog.records)


# --- dedupe and rate cap ----------------------------------------------------

def test_the_same_alert_is_mailed_once_per_20h_and_again_after(host, state, caplog):
    env = _env(state)
    assert an.notify(UNIT, env=env, now=NOW) == "sent"
    with caplog.at_level(logging.WARNING, logger="osm-alert"):
        assert an.notify(UNIT, env=env, now=NOW + 19 * 3600) == "suppressed"
    assert any("suppressed" in r.getMessage() for r in caplog.records)
    assert an.notify(UNIT, env=env, now=NOW + 20 * 3600 + 1) == "sent"
    assert len(host.sent) == 2


def test_a_hold_that_persists_every_night_mails_every_night(host, state):
    """The timer fires at 00:00 every 24h but a run lasts a varying time: a run
    that ends 1h20 earlier than the night last mailed is still a new night's
    alert (a 24h window held it, mailing only every other night)."""
    env = _env(state)
    night1 = NOW                      # run ended 05:30
    night2 = NOW + 24 * 3600 - 4800   # next run ended 04:10
    night3 = night2 + 24 * 3600 - 600
    assert [an.notify(UNIT, env=env, now=n) for n in (night1, night2, night3)] == [
        "sent", "sent", "sent"]
    # A manual re-run the same morning is still the same alert.
    assert an.notify(UNIT, env=env, now=night3 + 3600) == "suppressed"


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


def test_an_unwritable_state_directory_still_sends_the_alert(host, state):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    state.chmod(0o500)
    try:
        assert an.notify(UNIT, env=_env(state), now=NOW) == "sent"
    finally:
        state.chmod(0o700)
    assert len(host.sent) == 1


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


class _RecordingSMTP:
    """Stands in for ``smtplib.SMTP`` at the socket boundary: the REAL
    ``smtplib.SMTP.send_message`` runs (Bcc stripping, flattening) and hands
    its envelope and wire bytes to ``sendmail``, recorded here."""

    instances: list = []

    def __init__(self, host, port, **kwargs):
        self.wire = None
        self.envelope = None
        _RecordingSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, opt):
        return False

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        self.envelope = list(to_addrs)
        self.wire = msg if isinstance(msg, bytes) else msg.encode()
        return {}

    send_message = an.mailer.smtplib.SMTP.send_message


def test_no_admin_sees_another_admins_address_on_the_wire(host, state, monkeypatch):
    """Privacy: the alert reaches every admin, yet the transmitted message
    names none of them (To: undisclosed-recipients, Bcc never transmitted)."""
    sent = []
    monkeypatch.setattr(an, "_deliver", lambda msg: sent.append(msg) or True)
    assert an.notify(UNIT, env=_env(state), now=NOW) == "sent"
    _RecordingSMTP.instances = []
    monkeypatch.setattr(an.mailer.smtplib, "SMTP", _RecordingSMTP)
    assert an.mailer.send(sent[0], timeout=1) is True
    smtp = _RecordingSMTP.instances[0]
    assert sorted(smtp.envelope) == ["admin1@example.com", "admin2@example.com"]
    assert b"admin1@example.com" not in smtp.wire
    assert b"admin2@example.com" not in smtp.wire
    assert b"undisclosed-recipients:;" in smtp.wire


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


# --- a new cause of the same exit status mails at once -------

def _crash_log(cause_lines):
    return [f"osm-run-start 2026-10-20T20:30:00Z invocation={INV}",
            "2026-10-20 03:30:08,287 INFO indexing", *cause_lines]


def test_a_crash_with_a_new_cause_mails_at_once(host, state):
    env = _env(state, MONITOR_EXIT_STATUS="1")
    host.log.write_text("\n".join(_crash_log([
        "Traceback (most recent call last):",
        "neo4j.exceptions.ServiceUnavailable: Couldn't connect to localhost:7687",
    ])) + "\n")
    assert an.notify(UNIT, env=env, now=NOW) == "sent"
    host.log.write_text("\n".join(_crash_log([
        "2026-10-21 03:31:00,001 ERROR repo id=388 failed: fatal: Remote branch 18.0 not found",
    ])) + "\n")
    assert an.notify(UNIT, env=env, now=NOW + 3600) == "sent"
    assert len(host.sent) == 2


def test_the_same_cause_on_another_night_is_the_same_alert(host, state):
    """Timestamps, numbers, hex ids and paths differ every night; the cause
    does not, so the repeat window still holds the second mail."""
    env = _env(state, MONITOR_EXIT_STATUS="1")
    host.log.write_text("\n".join(_crash_log([
        "2026-10-20 03:31:00,001 ERROR repo id=31 failed at /home/x/clones/a1b2c3d4e5 (12 files)",
    ])) + "\n")
    assert an.notify(UNIT, env=env, now=NOW) == "sent"
    host.log.write_text("\n".join(_crash_log([
        "2026-10-21 03:44:17,912 ERROR repo id=31 failed at /home/x/clones/ffee99887766 (13 files)",
    ])) + "\n")
    assert an.notify(UNIT, env=env, now=NOW + 3600) == "suppressed"


def test_failure_signature_masks_volatile_parts():
    a = an.failure_signature(["2026-10-20T03:31:00Z ERROR job 4521 pid 77 at /tmp/x/y.py"])
    b = an.failure_signature(["2026-10-22T05:01:09Z ERROR job 9 pid 12345 at /var/a.py"])
    assert a == b == "ERROR job N pid N at <path>"
    assert an.failure_signature(["all good", "Done"]) == ""


# --- a failed pre-flight names itself ------------------------

_PRE_OK = ("ExecStartPre={ path=/bin/sh ; argv[]=/bin/sh -c echo osm-run-start ; "
           "ignore_errors=no ; start_time=[x] ; stop_time=[x] ; pid=10 ; code=exited ; "
           "status=0/SUCCESS }\n")
_PRE_124 = ("ExecStartPre={ path=/bin/sh ; argv[]=/bin/sh -c timeout 60 sh -c until docker "
            "exec pg pg_isready ; ignore_errors=no ; start_time=[x] ; stop_time=[x] ; pid=11 ; "
            "code=exited ; status=124 }\n")


def test_a_pre_flight_timeout_is_reported_as_the_database_not_ready(host, state):
    """The main process never ran: systemd sets $MONITOR_SERVICE_RESULT but no
    $MONITOR_EXIT_STATUS. ExecMainStatus=3 is the previous night's: never shown."""
    host.start_pre = _PRE_OK + _PRE_124
    env = _env(state)
    del env["MONITOR_EXIT_STATUS"]
    del env["MONITOR_EXIT_CODE"]
    assert an.notify(UNIT, env=env, now=NOW) == "sent"
    msg = host.sent[0]
    assert msg["Subject"].endswith("exit 124 (ExecStartPre)")
    body = _body(msg)
    assert "PostgreSQL not ready within 60s" in body
    assert "exit unknown" not in body
    assert "lifecycle needs an operator" not in body


def test_any_other_failed_pre_command_says_a_start_condition_failed(host, state):
    host.start_pre = _PRE_OK.replace("code=exited ; status=0/SUCCESS", "code=exited ; status=2")
    assert an.notify(UNIT, env={"STATE_DIRECTORY": str(state)}, now=NOW) == "sent"
    assert host.sent[0]["Subject"].endswith("exit 2 (ExecStartPre)")
    assert "a start condition failed (ExecStartPre)" in _body(host.sent[0])


def test_a_pre_command_allowed_to_fail_is_not_the_cause():
    text = _PRE_124.replace("ignore_errors=no", "ignore_errors=yes") + _PRE_OK
    assert an.failed_start_pre(text) is None
    assert an.failed_start_pre(_PRE_OK + _PRE_124) == ("exited", "124")


# --- what the hourly cap holds is not lost ------------------

def test_alerts_held_by_the_cap_are_listed_in_the_next_mail(host, state):
    for i in range(8):  # 6 sent, unit-6 and unit-7 held
        an.notify(f"unit-{i}", env=_env(state, MONITOR_UNIT=f"unit-{i}"), now=NOW + i)
    assert len(host.sent) == 6
    assert an.notify("unit-9", env=_env(state, MONITOR_UNIT="unit-9"), now=NOW + 3601) == "sent"
    body = _body(host.sent[-1])
    assert "held by the hourly cap (2)" in body
    assert "unit-6.service" in body and "unit-7.service" in body
    # Listed once: the following mail does not repeat the digest.
    an.notify("unit-10", env=_env(state, MONITOR_UNIT="unit-10"), now=NOW + 3602)
    assert "held by the hourly cap" not in _body(host.sent[-1])


def test_a_failed_delivery_keeps_the_held_alerts_for_the_next_mail(host, state):
    for i in range(7):
        an.notify(f"unit-{i}", env=_env(state, MONITOR_UNIT=f"unit-{i}"), now=NOW + i)
    host.smtp_error = OSError("SES down")
    assert an.notify("unit-8", env=_env(state, MONITOR_UNIT="unit-8"),
                     now=NOW + 3601) == "delivery-failed"
    host.smtp_error = None
    assert an.notify("unit-8", env=_env(state, MONITOR_UNIT="unit-8"), now=NOW + 3700) == "sent"
    assert "unit-6.service" in _body(host.sent[-1])


# --- bounded in time: the hook ends before its TimeoutStartSec= (ops 11) ----

def test_a_busy_state_lock_does_not_hold_the_alert_past_its_timeout(host, state, monkeypatch,
                                                                     caplog):
    import fcntl
    import os
    import threading

    monkeypatch.setattr(an, "LOCK_TIMEOUT_S", 0.3)
    fd = os.open(state / "state.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another alert that never lets go
    out = {}
    try:
        with caplog.at_level(logging.ERROR, logger="osm-alert"):
            worker = threading.Thread(
                target=lambda: out.setdefault("r", an.notify(UNIT, env=_env(state), now=NOW)),
                daemon=True)
            worker.start()
            worker.join(5)
        assert not worker.is_alive(), "notify blocked on the state lock"
    finally:
        os.close(fd)
    assert out["r"] == "sent"
    assert any("busy" in r.getMessage() for r in caplog.records)


def test_the_state_lock_is_free_while_the_mail_is_sent(host, state):
    import fcntl
    import os

    def try_lock(_msg):
        fd = os.open(state / "state.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if held
        finally:
            os.close(fd)

    host.on_deliver = try_lock
    assert an.notify(UNIT, env=_env(state), now=NOW) == "sent"


def test_a_hanging_smtp_relay_is_cut_at_the_send_deadline(host, state, monkeypatch, caplog):
    monkeypatch.setattr(an, "SEND_DEADLINE_S", 0.2)
    host.on_deliver = lambda _msg: time.sleep(3)
    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="osm-alert"):
        assert an.notify(UNIT, env=_env(state), now=NOW) == "delivery-failed"
    assert time.monotonic() - started < 2
    assert any("DELIVERY_FAILED" in r.getMessage() for r in caplog.records)
    # Not recorded: the next failure retries.
    host.on_deliver = None
    assert an.notify(UNIT, env=_env(state), now=NOW + 60) == "sent"


def test_the_worst_case_time_budget_fits_the_unit_timeout():
    from tests.test_systemd_templates import _ALERT_UNIT, _directive

    (timeout,) = _directive(_ALERT_UNIT, "TimeoutStartSec")
    budget = (an.CMD_CALLS_MAX * an.CMD_TIMEOUT_S
              + an.DB_CONNECT_TIMEOUT_S + an.DB_STATEMENT_TIMEOUT_S
              + 2 * an.LOCK_TIMEOUT_S + an.SEND_DEADLINE_S)
    assert budget < float(timeout), (budget, timeout)
    assert an.SMTP_TIMEOUT_S < an.SEND_DEADLINE_S


# --- redaction happens before truncation and covers more secrets (ops 12) ---

def test_a_long_line_is_redacted_before_it_is_truncated(host, state):
    # The 500-character cut falls inside the password, before its "@".
    line = "x" * 467 + " postgresql://odoo_semantic:Sup3rSecretPassw0rd@db:5432/osm"
    assert 0 < an._MAX_LINE_CHARS - line.index("Sup3r") < len("Sup3rSecretPassw0rd")
    host.log.write_text("\n".join(_log_lines(attention=[], extra=[line])) + "\n")
    an.notify(UNIT, env=_env(state), now=NOW)
    body = _body(host.sent[0])
    assert "Sup3r" not in body


def test_a_key_block_cut_by_the_window_is_still_redacted(host, state):
    # 65 body lines: the BEGIN line falls outside the 60-line window.
    body_lines = [
        f"MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj{i:02d}AgEAAoIBAQC7Vx" for i in range(65)
    ]
    host.log.write_text("\n".join(_log_lines(attention=[], extra=[
        "-----BEGIN " "PRIVATE KEY-----", *body_lines, "Zm9v",
        "-----END " "PRIVATE KEY-----"])) + "\n")
    an.notify(UNIT, env=_env(state), now=NOW)
    body = _body(host.sent[0])
    assert "MIIEvQIBADAN" not in body
    assert "Zm9v" not in body


# Fake secrets; token-shaped literals are split so secret scanners do not
# flag test data (the runtime strings are unchanged).
@pytest.mark.parametrize("secret, leaked", [
    ("NEO4J_AUTH=neo4j/N3o4jPassw0rd", "N3o4jPassw0rd"),
    ("token ghp_" "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5 used", "aB3dE5gH7jK9mN1pQ3sT5vX7z"),
    ("pat github_pat_" "11ABCDEFG0123456789_abcdefghijklmnop", "11ABCDEFG0123456789"),
    ("oauth gho_" "16C7e42F292c6912E7710c838347Ae178B4a", "16C7e42F292c6912E7710c838347Ae178B4a"),
    # url-safe alphabet ("-", "_"): not caught by the generic base64-run rule.
    ("key Xh3qzK5T2vB9mN4p-L7wR1sD6_G8jH0kQyUaEoIcVbZ= loaded", "Xh3qzK5T2vB9mN4p"),
    ("aws AKIA" "IOSFODNN7EXAMPLE secret wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY",
     "wJalrXUtnFEMI/K7MDENG"),
    ("smtp smtps://AKIA123:BEXz1+sesPass/w0rd@email-smtp.us-west-2.amazonaws.com:465",
     "BEXz1+sesPass"),
])
def test_more_secret_shapes_are_redacted(secret, leaked):
    assert leaked not in an.redact(secret)


def test_commit_ids_and_paths_survive_redaction():
    sha = "e5de6ca8013f1b2c3d4e5f60718293a4b5c6d7e8"
    path = "/home/odoo-semantic/odoo-semantic-mcp/src/indexer/pipeline_repo.py"
    text = f"HEAD is now at {sha}\n  File \"{path}\", line 206"
    assert an.redact(text) == text


# --- the last known admin list expires ----------------------

def test_a_stale_admin_cache_is_not_used_when_the_db_is_down(host, state, caplog):
    (state / "admins.json").write_text(json.dumps(
        {"emails": ["demoted@example.com"], "updated_at": NOW - 8 * 86400}))
    host.admins = RuntimeError("connection refused")
    env = _env(state, OSM_ALERT_FALLBACK_TO="ops@example.com")
    with caplog.at_level(logging.ERROR, logger="osm-alert"):
        assert an.notify(UNIT, env=env, now=NOW) == "sent"
    assert host.sent[0]["Bcc"] == "ops@example.com"
    assert "8.0 days old" in _body(host.sent[0])


def test_an_admin_cache_without_a_write_time_is_not_trusted(host, state):
    (state / "admins.json").write_text(json.dumps({"emails": ["old@example.com"]}))
    host.admins = RuntimeError("down")
    assert an.notify(UNIT, env=_env(state), now=NOW) == "no-recipient"


def test_refresh_admins_rewrites_the_cache_without_mailing(host, state, monkeypatch):
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    assert an.main(["--refresh-admins"]) == 0
    cached = json.loads((state / "admins.json").read_text())
    assert cached["emails"] == ["admin1@example.com", "admin2@example.com"]
    assert time.time() - cached["updated_at"] < 60
    assert host.sent == []
    host.admins = RuntimeError("down")  # a failed refresh keeps the old list, exits 0
    assert an.main(["--refresh-admins"]) == 0
    assert json.loads((state / "admins.json").read_text())["emails"] == cached["emails"]
