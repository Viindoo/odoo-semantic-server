# SPDX-License-Identifier: AGPL-3.0-or-later
# src/ops/alert_notify.py
"""Mail the OSM admins when a systemd unit fails (``osm-alert@.service``, #383).

Every OSM unit has ``OnFailure=osm-alert@%n``. systemd then starts
``osm-alert@<unit>.service``, which runs::

    python -m src.ops.alert_notify --unit %i

What deserves an alert: a failure that stops OSM from reflecting the code and
that the system cannot heal by itself - a unit crash or non-zero exit (the
database or Neo4j down included), and exit 3 of the nightly reindex: indexed,
but OSM no longer reflects part of the code and an operator must act. The
indexer exits 3 (``src.indexer.__main__._lifecycle_exit_code``) for an
untrusted checkout (G-A), a total wipe that is not in the branch history
(``total_wipe``), an unparseable manifest, a graph missing Module nodes of
present modules (``embedding_sweep:graph_unhealthy``), an undecidable name, a
reconcile error, a registered repo it could not clone (``unavailable_repos``),
the same repo's pre-scan fetch failing in two consecutive runs
(``stale_repos``), the fetch failing for every repo of the run (at least two:
a DNS / network outage or a shared credential, ``outages: fetch_outage``) and
the embedder returning no vector for any chunk the run sent (``outages:
embedder_down``). Not alerts: one fetch failure that heals by the next run, a
partial embedding failure, and data kept because git cannot prove it gone
(``lifecycle-audit`` ``parser_gaps`` / ``unproven_drops``). Precedent: from 2026-08-25 to
2026-09-25 the nightly reindex failed 32 nights out of 32 and nobody noticed,
because this hook only wrote a journal line; on 2026-10-18/19 a DNS failure
made two nightly runs no-ops.

What it does, in order (it never fails the unit: exit 0 whatever happens):

1. Writes the journal line ``osm-alert: unit=<u> state=failed host=<h> ...``
   (kept for log scrapers).
2. Reads the failure: ``$MONITOR_*`` (set by systemd for OnFailure= units),
   completed by ``systemctl show``. A unit systemd does not know (the smoke
   test ``osm-alert@dummy.service``) is a test alert.
3. Reads the last 60 lines of the unit's output: the log file of its
   ``StandardOutput=append:`` (``systemctl cat``) or else the journal of that
   invocation. In a log file the run starts after the line
   ``osm-run-start ... invocation=<id>`` that the unit's first ExecStartPre=
   writes; the ``Lifecycle needs attention`` block is only reported when it
   was written by this run.
4. Recipients: every active Web UI admin with an email
   (``webui_users.is_admin AND is_active``), read from PostgreSQL at send time.
   Each successful read refreshes ``admins.json`` in the state directory (so
   does ``--refresh-admins``, run by the nightly reindex unit); when the
   database is unreachable (exactly when alerts matter) the alert goes to that
   last known list - unless it is older than ``OSM_ALERT_ADMIN_CACHE_MAX_DAYS``
   (7), so a demoted admin stops receiving log excerpts - plus
   ``OSM_ALERT_FALLBACK_TO``.
5. Dedupe: one mail per (unit, exit status, attention block - or, without
   one, the normalized last error line of the output) every
   ``OSM_ALERT_REPEAT_HOURS`` (20: shorter than the 24h timer period, so a
   failure that persists every night mails every night); at most
   ``OSM_ALERT_MAX_PER_HOUR`` (6) mails per hour over all units. An alert the
   cap holds is recorded and listed in the next mail that goes out. A test
   alert skips the dedupe, not the cap. The state lock is held only to decide
   and reserve (bounded wait), never while the mail is sent.
6. Sends through :mod:`src.mailer` (the SES SMTP settings of the Web UI)
   ONE mail with every admin in ``Bcc`` and ``To: undisclosed-recipients:;``:
   no recipient sees the other admins' addresses (the Bcc header is never
   transmitted). Secrets in the log excerpt are redacted first (before any
   truncation). The whole send has a hard deadline well under the unit's
   ``TimeoutStartSec=``.

State lives in ``$STATE_DIRECTORY`` (``StateDirectory=osm-alert``), else
``OSM_ALERT_STATE_DIR``, else ``~/.local/state/osm-alert``.

Manual use::

    python -m src.ops.alert_notify --unit odoo-semantic-reindex --dry-run
    python -m src.ops.alert_notify --unit odoo-semantic-reindex --force
    python -m src.ops.alert_notify --refresh-admins
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from src import mailer

logger = logging.getLogger("osm-alert")

TAIL_LINES = 60
# Shorter than the 24h period of the nightly timer: the alert of a run that
# ends a little earlier than the night last mailed must not be held as "the
# same alert" (a persistent failure mails every night, not every other night).
DEFAULT_REPEAT_HOURS = 20.0
DEFAULT_MAX_PER_HOUR = 6
# The last known admin list is used for at most this long when the database
# is down; an older list may still name an admin demoted since.
DEFAULT_ADMIN_CACHE_MAX_DAYS = 7.0
# Time budget of one alert, all bounded below osm-alert@.service
# TimeoutStartSec= (tests/test_alert_notify.py checks the sum): systemctl /
# journalctl reads, the DB read, the state lock wait and the whole SMTP send.
SMTP_TIMEOUT_S = 10.0       # each socket operation
SEND_DEADLINE_S = 45.0      # the whole send (connect, STARTTLS, login, data)
LOCK_TIMEOUT_S = 20.0       # waiting for another alert's state lock
DB_CONNECT_TIMEOUT_S = 5
DB_STATEMENT_TIMEOUT_S = 5
CMD_TIMEOUT_S = 15
CMD_CALLS_MAX = 4           # systemctl show (+ fallback), systemctl cat, journalctl
_HELD_KEEP = 50             # cap-held alerts kept for the next mail's digest
# Bytes read from the end of a unit log file: the nightly log is rotated daily
# and one run writes a few KB, so this always covers the current run.
_LOG_READ_BYTES = 512 * 1024
_MAX_LINE_CHARS = 500
_MAX_BODY_CHARS = 20_000
_HISTORY_KEEP_S = 7 * 24 * 3600

RUN_MARKER = "osm-run-start"
# The To: of an alert; the admins are in Bcc (an RFC 5322 empty group).
UNDISCLOSED_RECIPIENTS = "undisclosed-recipients:;"
ATTENTION_HEADER_RE = re.compile(r"^Lifecycle needs attention \(exit \d+\):")
RUNBOOK = (
    "What to do: docs/deploy/db-tier-operations.md section \"Alert wiring\" "
    "(what each failure means), docs/deploy.md section 3.6 for lifecycle holds."
)

# Exit statuses whose meaning is fixed by systemd (systemd.exec(5)).
_SYSTEMD_STATUS = {
    "200": "systemd could not set up the process (CHDIR / setup)",
    "203": "the ExecStart= binary could not be executed (EXEC)",
    "217": "the User= does not exist (USER)",
    "243": "a LoadCredential= source is missing (CREDENTIALS)",
}
# Exit statuses of the OSM indexer CLI (src/indexer/__main__.py).
_INDEXER_STATUS = {
    "1": "the run crashed or a repo or profile failed to index",
    "3": "indexed, but OSM no longer reflects part of the code and an operator must act",
    "4": "the lifecycle audit found drift",
}
_INDEXER_UNITS = ("odoo-semantic-reindex", "odoo-semantic-lifecycle-audit")
# A failed ExecStartPre= (the main process never ran, so systemd sets no
# $MONITOR_EXIT_STATUS): exit 124 is `timeout`, which the reindex and
# lifecycle-audit units wrap around their 60s pg_isready pre-flight.
_PRE_STATUS = {
    "124": "pre-flight timed out: PostgreSQL not ready within 60s (the pg_isready wait)",
}
# $MONITOR_SERVICE_RESULT / Result= when no exit status says more.
_RESULT_MEANING = {
    "timeout": "the unit exceeded its start or stop timeout",
    "start-limit-hit": "the unit was started too often (StartLimitBurst=), systemd gave up",
    "resources": "systemd could not set up the unit's resources",
    "oom-kill": "the kernel OOM killer stopped the unit",
    "watchdog": "the unit's watchdog expired",
    "core-dump": "the main process dumped core",
    "signal": "the main process was killed by a signal",
}


# ---------------------------------------------------------------------------
# Unit identity and state
# ---------------------------------------------------------------------------


def normalize_unit(name: str) -> str:
    """``x``, ``x.service`` and ``x.service.service`` all name ``x.service``.

    ``OnFailure=osm-alert@%n`` passes ``x`` as the instance, the older
    ``osm-alert@%n.service`` spelling passes ``x.service``: both must reach the
    same dedupe key.
    """
    name = name.strip()
    while name.endswith(".service"):
        name = name[: -len(".service")]
    return f"{name}.service"


@dataclass
class UnitFailure:
    unit: str
    result: str = "unknown"
    exit_code: str = ""
    exit_status: str = "unknown"
    invocation_id: str = ""
    started_at: float | None = None
    known: bool = True
    from_monitor: bool = False
    # Where it failed when the main process never ran ("ExecStartPre").
    phase: str = ""

    @property
    def is_test(self) -> bool:
        """A unit systemd does not know: the documented smoke test."""
        return not self.known

    def status_label(self) -> str:
        word = "signal" if self.exit_code in ("killed", "dumped") else "exit"
        label = f"{word} {self.exit_status}"
        return f"{label} ({self.phase})" if self.phase else label

    def meaning(self) -> str | None:
        if self.exit_status in _SYSTEMD_STATUS:
            return _SYSTEMD_STATUS[self.exit_status]
        if self.phase:
            pre = _PRE_STATUS.get(self.exit_status) if self.exit_code in ("", "exited") else None
            return pre or f"a start condition failed ({self.phase}); the main process never ran"
        base = self.unit.removesuffix(".service")
        if base in _INDEXER_UNITS and self.exit_code in ("", "exited"):
            known = _INDEXER_STATUS.get(self.exit_status)
            if known:
                return known
        if self.exit_status == "unknown":
            return _RESULT_MEANING.get(self.result)
        return None


def _run_cmd(args: list[str]) -> str | None:
    """stdout of *args*, or None when it cannot run or exits non-zero."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=CMD_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("osm-alert: %s failed: %s", args[0], exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_show(text: str | None) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    return props


# ``systemctl show -p ExecMainCode`` is the waitid() si_code; $MONITOR_EXIT_CODE
# gives the same as a word.
_EXEC_MAIN_CODE = {"1": "exited", "2": "killed", "3": "dumped"}


def _unix_ts(value: str | None) -> float | None:
    """``@1760905807`` (``systemctl show --timestamp=unix``) -> epoch seconds."""
    if not value or not value.startswith("@"):
        return None
    try:
        return float(value[1:])
    except ValueError:
        return None


_EXEC_ENTRY_RE = re.compile(r"\{([^{}]*)\}")
_EXEC_FIELD_RE = re.compile(r"(?:^|;)\s*([a-z_]+)=([^;]*)")


def failed_start_pre(text: str | None) -> tuple[str, str] | None:
    """(code, status) of the ExecStartPre= command that failed, from
    ``systemctl show -p ExecStartPre`` (one ``{ path=... ; code=exited ;
    status=124/n/a }`` entry per command); None when every one succeeded or
    none ran. Commands prefixed ``-`` (``ignore_errors=yes``) never fail a unit."""
    for line in (text or "").splitlines():
        if not line.startswith("ExecStartPre="):
            continue
        for entry in _EXEC_ENTRY_RE.findall(line):
            fields = {k: v.strip() for k, v in _EXEC_FIELD_RE.findall(entry)}
            if fields.get("ignore_errors") == "yes":
                continue
            code = fields.get("code", "")
            status = fields.get("status", "").split("/", 1)[0]
            if code == "exited" and status not in ("", "0"):
                return "exited", status
            if code in ("killed", "dumped"):
                return code, status or "unknown"
    return None


def read_unit_failure(unit: str, env: dict[str, str]) -> UnitFailure:
    """The failure of *unit*: ``$MONITOR_*`` first, ``systemctl show`` for the rest.

    systemd sets ``$MONITOR_EXIT_CODE/STATUS`` only when the main process ran.
    Without them (an ``ExecStartPre=`` failed, e.g. the 60s pg_isready
    pre-flight: database down) the failed command and its status are read
    from ``systemctl show -p ExecStartPre``."""
    unit = normalize_unit(env.get("MONITOR_UNIT") or unit)
    failure = UnitFailure(unit=unit)
    if env.get("MONITOR_SERVICE_RESULT") or env.get("MONITOR_EXIT_STATUS"):
        failure.from_monitor = True
        failure.result = env.get("MONITOR_SERVICE_RESULT") or failure.result
        failure.exit_code = env.get("MONITOR_EXIT_CODE", "")
        failure.exit_status = env.get("MONITOR_EXIT_STATUS") or failure.exit_status
        failure.invocation_id = env.get("MONITOR_INVOCATION_ID", "")
    fields = ("LoadState,Result,ExecMainCode,ExecMainStatus,InvocationID,"
              "ExecMainStartTimestamp,ExecStartPre")
    text = _run_cmd(["systemctl", "show", "--timestamp=unix", "-p", fields, unit])
    if text is None:  # systemd < 248 has no --timestamp=; the start time stays unknown
        text = _run_cmd(["systemctl", "show", "-p", fields, unit])
    props = _parse_show(text)
    if props.get("LoadState") == "not-found":
        failure.known = False
    if not failure.from_monitor:
        failure.result = props.get("Result") or failure.result
        failure.invocation_id = props.get("InvocationID", "")
    main_status_known = bool(env.get("MONITOR_EXIT_STATUS"))
    if not main_status_known:
        # Each start re-runs every ExecStartPre=, so a failed entry is this run's
        # and the main process (whose ExecMain* would be an older run's) never ran.
        pre = failed_start_pre(text) if failure.result != "success" else None
        if pre is not None:
            failure.exit_code, failure.exit_status = pre
            failure.phase = "ExecStartPre"
        elif not failure.from_monitor:
            failure.exit_code = _EXEC_MAIN_CODE.get(props.get("ExecMainCode", ""), "")
            status = props.get("ExecMainStatus")
            if status:
                failure.exit_status = status
    failure.started_at = _unix_ts(props.get("ExecMainStartTimestamp"))
    return failure


# ---------------------------------------------------------------------------
# Unit output: log file or journal
# ---------------------------------------------------------------------------


_STD_RE = re.compile(r"^Standard(Output|Error)=(.*)$")
_FILE_PREFIXES = ("append:", "file:", "truncate:")


def unit_log_file(unit: str) -> Path | None:
    """The file the unit's ``StandardOutput=`` (else ``StandardError=``) writes
    to, read from ``systemctl cat`` so drop-ins count (the last assignment
    wins); None for journal units."""
    text = _run_cmd(["systemctl", "cat", "--no-pager", unit])
    found: dict[str, str] = {}
    for line in (text or "").splitlines():
        match = _STD_RE.match(line.strip())
        if match:
            found[match.group(1)] = match.group(2).strip()
    for stream in ("Output", "Error"):
        value = found.get(stream, "")
        if value.startswith(_FILE_PREFIXES):
            return Path(value.split(":", 1)[1])
    return None


def _read_tail(path: Path) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - _LOG_READ_BYTES))
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > _LOG_READ_BYTES and lines:
        lines = lines[1:]  # the first line is cut
    return lines


@dataclass
class UnitOutput:
    source: str
    lines: list[str] = field(default_factory=list)
    this_run: bool = False  # True when the lines are known to be this run's only
    note: str = ""


def this_run_lines(lines: list[str], invocation_id: str) -> list[str] | None:
    """The lines after this invocation's run-start marker, None without one."""
    if not invocation_id:
        return None
    needle = f"invocation={invocation_id}"
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        if RUN_MARKER in line and needle in line:
            return lines[idx + 1:]
    return None


def read_unit_output(failure: UnitFailure) -> UnitOutput:
    path = unit_log_file(failure.unit)
    if path is not None:
        try:
            lines = _read_tail(path)
            mtime = path.stat().st_mtime
        except OSError as exc:
            return UnitOutput(source=str(path), note=f"log file not readable: {exc}")
        scoped = this_run_lines(lines, failure.invocation_id)
        if scoped is not None:
            return UnitOutput(source=f"{path}, this run", lines=scoped, this_run=True)
        note = "no run-start marker for this run in the log file"
        if failure.started_at is not None and mtime < failure.started_at:
            note = "the log file was not written during this run"
        return UnitOutput(source=str(path), lines=lines, note=note)
    args = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(TAIL_LINES),
            "-u", failure.unit]
    if failure.invocation_id:
        args.append(f"_SYSTEMD_INVOCATION_ID={failure.invocation_id}")
    text = _run_cmd(args)
    if text is None:
        return UnitOutput(source="journal", note="journal not readable")
    lines = [ln for ln in text.splitlines() if not ln.startswith("-- ")]
    return UnitOutput(
        source="journal, this run" if failure.invocation_id else "journal",
        lines=lines, this_run=bool(failure.invocation_id),
    )


def extract_attention_block(lines: list[str]) -> list[str]:
    """The last ``Lifecycle needs attention (exit N):`` block of *lines*: the
    header and the two-space indented lines after it (``_lifecycle_exit_code``)."""
    start = None
    for idx in range(len(lines) - 1, -1, -1):
        if ATTENTION_HEADER_RE.match(lines[idx]):
            start = idx
            break
    if start is None:
        return []
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        block.append(line)
    return block


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_PEM_END = r"-----END [A-Z ]*PRIVATE KEY-----"
_B64_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=_-])")


def _looks_like_key_material(run: str) -> bool:
    """A long base64 run that is key material, not a commit id (hex only) nor
    a path (mostly lower case, a slash every few characters)."""
    body = run.rstrip("=")
    if re.fullmatch(r"[0-9a-fA-F]+", body):
        return False
    upper = sum(ch.isupper() for ch in body)
    lower = sum(ch.islower() for ch in body)
    digits = sum(ch.isdigit() for ch in body)
    return upper >= 4 and lower >= 4 and digits >= 1 and body.count("/") * 12 <= len(body)


def _redact_b64_run(match: re.Match[str]) -> str:
    return "[base64 redacted]" if _looks_like_key_material(match.group(0)) else match.group(0)


_REDACTIONS: tuple[tuple[re.Pattern[str], object], ...] = (
    # A whole PEM block, or its start when the window cuts the END line off.
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:" + _PEM_END + r"|\Z)", re.S,
    ), "[private key redacted]"),
    # The body lines of a PEM block whose BEGIN line is outside the window.
    (re.compile(r"(?m)^[A-Za-z0-9+/=]{16,}$(?=\n(?:[A-Za-z0-9+/=]+\n)*" + _PEM_END + ")"),
     "[private key redacted]"),
    (re.compile(r"(?m)^[A-Za-z0-9+/=]+$(?=\n" + _PEM_END + ")"), "[private key redacted]"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/@\s]*):([^@\s]+)@", re.I), r"\1:***@"),
    # NEO4J_AUTH=neo4j/<password> (the docker image's user/password form).
    (re.compile(r"(\b[A-Za-z0-9_]*_AUTH)(\s*[=:]\s*)([\"']?)([^/\s\"']+)/[^\s\"']+"),
     r"\1\2\3\4/***"),
    (re.compile(
        r"(\b[A-Za-z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY|FERNET_KEY)"
        r"[A-Za-z0-9_]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)",
        re.I,
    ), r"\1\2***"),
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I), r"\1 ***"),
    (re.compile(r"\bosm_[A-Za-z0-9_-]{16,}"), "osm_***"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "gh***"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), r"\1***"),
    # A bare Fernet key: 32 bytes as url-safe base64 = 43 characters and one "=".
    (re.compile(r"(?<![A-Za-z0-9_=-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=-])"),
     "[fernet key redacted]"),
    (_B64_RUN_RE, _redact_b64_run),
)


def redact(text: str) -> str:
    """Mask credentials a log excerpt may carry before it leaves the host.

    Run it on whole lines, BEFORE any truncation: a DSN cut before its ``@``
    no longer looks like a URL with a password and would leak its prefix."""
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

ADMIN_EMAILS_SQL = (
    "SELECT email FROM webui_users"
    " WHERE is_admin IS TRUE AND is_active IS TRUE"
    " AND email IS NOT NULL AND btrim(email) <> ''"
    " ORDER BY id"
)


def _clean_addresses(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        addr = str(value or "").strip()
        if "@" not in addr or any(ch.isspace() for ch in addr):
            continue
        if addr.lower() in seen:
            continue
        seen.add(addr.lower())
        out.append(addr)
    return out


def _split_env_list(raw: str | None) -> list[str]:
    return _clean_addresses(re.split(r"[,;\s]+", raw or ""))


def _fetch_admin_emails() -> list[str]:
    """Active admins' emails from PostgreSQL (owner DSN ``PG_DSN``). Raises
    when the database cannot be read."""
    import psycopg2  # noqa: PLC0415 - optional at import time

    from src import config  # noqa: PLC0415

    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        raise RuntimeError("PG_DSN is not set")
    conn = psycopg2.connect(
        dsn, connect_timeout=DB_CONNECT_TIMEOUT_S,
        options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT_S * 1000}",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(ADMIN_EMAILS_SQL)
            return _clean_addresses(row[0] for row in cur.fetchall())
    finally:
        conn.close()


@dataclass
class Recipients:
    addresses: list[str]
    source: str


def _read_cache(path: Path, *, now: float, max_age_s: float) -> tuple[list[str], str]:
    """(addresses, why-ignored) of the last known admin list. A list older than
    *max_age_s* (or without a write time) is ignored: it may name an admin
    demoted since, who must not keep receiving log excerpts."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], ""
    if not isinstance(data, dict):
        return [], ""
    emails = _clean_addresses(data.get("emails") or [])
    try:
        updated_at = float(data.get("updated_at"))
    except (TypeError, ValueError):
        updated_at = None
    if emails and (updated_at is None or now - updated_at > max_age_s):
        age = "of unknown age" if updated_at is None else (
            f"{(now - updated_at) / 86400:.1f} days old")
        return [], f"last known admin list ignored: {age}, max {max_age_s / 86400:g} days"
    return emails, ""


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def refresh_admin_cache(state_dir: Path | None, now: float | None = None) -> bool:
    """Read the admins from the database and rewrite ``admins.json``; False
    (logged) when the database or the state directory cannot be used."""
    if state_dir is None:
        return False
    try:
        admins = _fetch_admin_emails()
    except Exception as exc:  # noqa: BLE001 - a refresh is best effort
        logger.warning("osm-alert: admin list not refreshed: %s: %s", type(exc).__name__, exc)
        return False
    if not admins:
        logger.warning("osm-alert: admin list not refreshed: no active admin has an email")
        return False
    return _store_admin_cache(state_dir / "admins.json", admins,
                              time.time() if now is None else now)


def _store_admin_cache(cache: Path, admins: list[str], now: float) -> bool:
    try:
        _write_json_atomic(cache, {"emails": admins, "updated_at": now})
    except OSError as exc:
        logger.warning("osm-alert: cannot write %s: %s", cache, exc)
        return False
    return True


def resolve_recipients(state_dir: Path | None, env: dict[str, str], *,
                       write_cache: bool = True, now: float | None = None) -> Recipients:
    """DB admins (cached on success unless *write_cache* is False); when the DB
    cannot give one, the cached list (if not older than
    ``OSM_ALERT_ADMIN_CACHE_MAX_DAYS``) plus ``OSM_ALERT_FALLBACK_TO``."""
    now = time.time() if now is None else now
    cache = state_dir / "admins.json" if state_dir is not None else None
    reason: str
    try:
        admins = _fetch_admin_emails()
    except Exception as exc:  # noqa: BLE001 - DB down is the case this exists for
        detail = " ".join(str(exc).split())[:200]
        reason = f"admin list not readable from the database ({type(exc).__name__}: {detail})"
    else:
        if admins:
            if cache is not None and write_cache:
                _store_admin_cache(cache, admins, now)
            return Recipients(admins, "active Web UI admins (database)")
        reason = "no active Web UI admin has an email (webui_users)"
    logger.error("osm-alert: %s; using the fallback recipients", reason)
    max_age_s = _float_env(env, "OSM_ALERT_ADMIN_CACHE_MAX_DAYS",
                           DEFAULT_ADMIN_CACHE_MAX_DAYS) * 86400
    cached, ignored = (_read_cache(cache, now=now, max_age_s=max_age_s)
                       if cache is not None else ([], ""))
    if ignored:
        logger.error("osm-alert: %s", ignored)
        reason = f"{reason}; {ignored}"
    configured = _split_env_list(env.get("OSM_ALERT_FALLBACK_TO"))
    merged = _clean_addresses([*cached, *configured])
    parts = []
    if cached:
        parts.append("last known admin list")
    if configured:
        parts.append("OSM_ALERT_FALLBACK_TO")
    return Recipients(merged, f"{' + '.join(parts) or 'none'} ({reason})")


# ---------------------------------------------------------------------------
# Dedupe and rate cap
# ---------------------------------------------------------------------------


# Lines that name the cause of a failure without an attention block: an
# exception line (``RuntimeError: neo4j unreachable``), a logged ERROR /
# CRITICAL, the indexer's ``Finished with failures`` summary.
_CAUSE_LINE_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception|Exit)\b|\b(?:ERROR|CRITICAL|FATAL)\b"
    r"|Finished with failures|\bfailed\b",
)
_SIG_TS_RE = re.compile(r"^\S*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*\s*")


def failure_signature(lines: list[str]) -> str:
    """The cause of a run, normalized so that the same cause on another night
    gives the same signature: the last error-looking line of *lines* with its
    timestamp, paths, hex ids and numbers masked. "" when none is found."""
    for line in reversed(lines):
        if not _CAUSE_LINE_RE.search(line):
            continue
        sig = _SIG_TS_RE.sub("", line.strip())
        sig = re.sub(r"(?<![\w.])/[^\s:'\",)]+", "<path>", sig)
        sig = re.sub(r"\b[0-9a-fA-F]{7,}\b", "<hex>", sig)
        sig = re.sub(r"\d+", "N", sig)
        return " ".join(sig.split())[:300]
    return ""


def dedupe_key(failure: UnitFailure, attention: list[str], signature: str = "") -> str:
    """Same key = same alert. The attention block names the cause of an exit
    3; without one (a crash, a pre-flight failure, a journal unit) the
    normalized last error line does, so a NEW cause of the same exit status
    mails at once instead of being held as "the same alert"."""
    cause = [ln.strip() for ln in attention] if attention else [f"cause: {signature}"]
    material = "\n".join([failure.unit, failure.exit_status, failure.phase, *cause])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class Reservation:
    """A send reserved under the state lock, undone when delivery fails."""
    key: str
    now: float
    previous: float | None
    held: list[dict]


class AlertState:
    """Sent-alert history in ``state.json``, serialized by an flock.

    The lock is held only to decide and reserve a send, never while the mail
    goes out, and is waited for at most ``LOCK_TIMEOUT_S``: past that the
    alert is sent without dedupe rather than lost to the unit's timeout."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "state.json"
        self._lock_path = state_dir / "state.lock"
        self.data: dict = {"sent": {}, "history": [], "held": []}
        self.writable = True

    def _load(self) -> None:
        self.data = {"sent": {}, "history": [], "held": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self.data = {
                "sent": dict(raw.get("sent") or {}),
                "history": list(raw.get("history") or []),
                "held": [h for h in (raw.get("held") or []) if isinstance(h, dict)],
            }

    @contextlib.contextmanager
    def locked(self, timeout_s: float | None = None):
        """Hold the state lock; an unusable or busy lock degrades to "no
        dedupe, no state write" (the alert still goes out) rather than losing
        the alert."""
        timeout_s = LOCK_TIMEOUT_S if timeout_s is None else timeout_s
        try:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            logger.error("osm-alert: state lock %s unusable, sending without dedupe: %s",
                         self._lock_path, exc)
            self.writable = False
            yield self
            return
        try:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        logger.error(
                            "osm-alert: state lock %s busy for %gs, sending without dedupe",
                            self._lock_path, timeout_s)
                        self.writable = False
                        break
                    time.sleep(0.1)
            if self.writable:
                self._load()
            yield self
        finally:
            os.close(fd)

    def decide(self, key: str, now: float, *, repeat_s: float, max_per_hour: int,
               dedupe: bool = True) -> tuple[str, str] | None:
        """None to send, else (``dedupe`` | ``cap``, why it is suppressed)."""
        if not self.writable:
            return None
        last = self.data["sent"].get(key)
        if dedupe and last is not None and now - float(last) < repeat_s:
            hours = (now - float(last)) / 3600
            return "dedupe", (f"same alert sent {hours:.1f}h ago "
                              f"(repeat after {repeat_s / 3600:g}h)")
        recent = [t for t in self.data["history"] if now - float(t) < 3600]
        if len(recent) >= max_per_hour:
            return "cap", (f"rate cap reached ({len(recent)} mails in the last hour, "
                           f"max {max_per_hour}); listed in the next mail")
        return None

    def _save(self) -> None:
        if self.writable:
            _write_json_atomic(self.path, self.data)

    def hold(self, failure: UnitFailure, now: float) -> None:
        """Remember an alert the hourly cap held, for the next mail's digest."""
        held = [*self.data["held"], {
            "unit": failure.unit, "status": failure.status_label(), "at": now,
        }]
        self.data["held"] = held[-_HELD_KEEP:]
        self._save()

    def reserve(self, key: str, now: float) -> Reservation:
        """Record the send before it happens (so a concurrent alert with the
        same key is deduped) and take the held digest; undo() on failure."""
        res = Reservation(key, now, self.data["sent"].get(key), list(self.data["held"]))
        self.data["sent"] = {
            k: t for k, t in self.data["sent"].items() if now - float(t) < _HISTORY_KEEP_S
        }
        self.data["sent"][key] = now
        self.data["history"] = [
            t for t in self.data["history"] if now - float(t) < 3600
        ] + [now]
        self.data["held"] = []
        self._save()
        return res

    def undo(self, res: Reservation) -> None:
        """The reserved mail did not go out: the next failure must retry and
        the held alerts stay for the next mail."""
        if self.data["sent"].get(res.key) == res.now:
            if res.previous is None:
                self.data["sent"].pop(res.key, None)
            else:
                self.data["sent"][res.key] = res.previous
        with contextlib.suppress(ValueError):
            self.data["history"].remove(res.now)
        self.data["held"] = (res.held + self.data["held"])[-_HELD_KEEP:]
        self._save()


def state_dir_from_env(env: dict[str, str]) -> Path | None:
    raw = env.get("STATE_DIRECTORY") or env.get("OSM_ALERT_STATE_DIR")
    path = Path(raw.split(":")[0]) if raw else Path.home() / ".local" / "state" / "osm-alert"
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        logger.error("osm-alert: state directory %s unusable: %s", path, exc)
        return None
    return path


def _float_env(env: dict[str, str], name: str, default: float) -> float:
    try:
        value = float(env.get(name) or default)
    except ValueError:
        return default
    return value if value >= 0 else default


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "unknown"
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_message(
    failure: UnitFailure,
    output: UnitOutput,
    attention: list[str],
    recipients: Recipients,
    *,
    host: str,
    now: float,
    repeat_hours: float,
    held: list[dict] | None = None,
) -> EmailMessage:
    if failure.is_test:
        subject = f"[OSM ALERT] {host} {failure.unit} test alert"
        intro = (
            f"Test alert from {host}: the unit {failure.unit} does not exist, so this "
            "mail only proves that osm-alert@.service can reach you."
        )
    else:
        subject = f"[OSM ALERT] {host} {failure.unit} {failure.status_label()}"
        intro = f"{failure.unit} failed on {host}."
    meaning = failure.meaning()
    status = failure.status_label() + (f" - {meaning}" if meaning else "")
    lines = [
        intro,
        "",
        f"Unit:        {failure.unit}",
        f"Host:        {host}",
        f"Result:      {failure.result}",
        f"Status:      {status}",
        f"Run started: {_fmt_ts(failure.started_at)}",
        f"Alert time:  {_fmt_ts(now)}",
        f"Sent to:     {recipients.source}",
    ]
    if attention:
        lines += ["", *attention]
    if held:
        lines += ["", f"Also failed since the last mail, held by the hourly cap ({len(held)}):"]
        lines += [
            f"  {_fmt_ts(_as_float(h.get('at')))}  {h.get('unit', '?')}  {h.get('status', '?')}"
            for h in held
        ]
    tail = output.lines[-TAIL_LINES:]
    header = f"Last {len(tail)} lines of output ({output.source})"
    if output.note:
        header += f" - {output.note}"
    lines += ["", header + ":"]
    # Redact the full lines first (a PEM block spans several), then truncate.
    shown = redact("\n".join(tail)).split("\n") if tail else []
    lines += [ln[:_MAX_LINE_CHARS] for ln in shown] or ["(no output found)"]
    lines += [
        "",
        RUNBOOK,
        f"The same alert repeats at most once every {repeat_hours:g}h; "
        "see journalctl -u 'osm-alert@*' for suppressed ones.",
    ]
    body = redact("\n".join(lines))
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "\n[truncated]"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mailer.from_address()
    # Bcc, not To: the admins must not learn each other's addresses.
    msg["To"] = UNDISCLOSED_RECIPIENTS
    if recipients.addresses:
        msg["Bcc"] = ", ".join(recipients.addresses)
    msg.set_content(body)
    return msg


def _deliver(msg: EmailMessage) -> bool:
    return mailer.send(msg, timeout=SMTP_TIMEOUT_S)


def _deliver_within_deadline(msg: EmailMessage, deadline_s: float | None = None) -> bool:
    """:func:`_deliver` with a hard deadline on the WHOLE send: the per-socket
    timeout alone multiplies over connect, STARTTLS, login and data and could
    outlast the unit's ``TimeoutStartSec=`` (systemd would then kill the hook
    and the alert would be lost with no DELIVERY_FAILED line). Raises
    TimeoutError past the deadline; the daemon thread dies with the process."""
    deadline_s = SEND_DEADLINE_S if deadline_s is None else deadline_s
    box: dict = {}

    def run() -> None:
        try:
            box["sent"] = _deliver(msg)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    worker = threading.Thread(target=run, name="osm-alert-smtp", daemon=True)
    worker.start()
    worker.join(deadline_s)
    if worker.is_alive():
        raise TimeoutError(f"SMTP send did not finish within {deadline_s:g}s")
    if "error" in box:
        raise box["error"]
    return bool(box.get("sent"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def notify(unit: str, *, env: dict[str, str] | None = None, dry_run: bool = False,
           force: bool = False, now: float | None = None) -> str:
    """Handle one failure of *unit*; returns the outcome word
    (sent / suppressed / dry-run / no-recipient / no-smtp / delivery-failed)."""
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    host = socket.gethostname()
    failure = read_unit_failure(unit, env)
    logger.warning(
        "osm-alert: unit=%s state=failed host=%s result=%s status=%s",
        failure.unit.removesuffix(".service"), host, failure.result, failure.status_label(),
    )
    output = read_unit_output(failure)
    attention = extract_attention_block(output.lines) if output.this_run else []
    signature = "" if attention else failure_signature(output.lines[-TAIL_LINES:])
    state_dir = state_dir_from_env(env)
    recipients = resolve_recipients(state_dir, env, write_cache=not dry_run, now=now)
    repeat_hours = _float_env(env, "OSM_ALERT_REPEAT_HOURS", DEFAULT_REPEAT_HOURS)
    max_per_hour = int(_float_env(env, "OSM_ALERT_MAX_PER_HOUR", DEFAULT_MAX_PER_HOUR))

    def message(held: list[dict] | None = None) -> EmailMessage:
        return build_message(failure, output, attention, recipients, host=host, now=now,
                             repeat_hours=repeat_hours, held=held)

    if dry_run:
        msg = message()
        print(f"Subject: {msg['Subject']}\nTo: {msg['To']}\nBcc: {msg['Bcc'] or ''}"
              f"\n\n{msg.get_content()}")
        return "dry-run"
    if not recipients.addresses:
        logger.error(
            "osm-alert: NO RECIPIENT for the %s alert: no active admin email in the "
            "database, no cached list and OSM_ALERT_FALLBACK_TO unset (%s)",
            failure.unit, recipients.source,
        )
        return "no-recipient"
    if not mailer.smtp_host():
        logger.error("osm-alert: SMTP_HOST unset: the %s alert was not mailed (journal only)",
                     failure.unit)
        return "no-smtp"
    key = dedupe_key(failure, attention, signature)
    store = AlertState(state_dir) if state_dir is not None else None
    reservation: Reservation | None = None
    # Decide and reserve under the lock; send outside it, so a slow relay
    # never makes a concurrent alert wait.
    if store is not None:
        with store.locked():
            verdict = None if force else store.decide(
                key, now, repeat_s=repeat_hours * 3600, max_per_hour=max_per_hour,
                dedupe=not failure.is_test)
            if verdict is not None:
                kind, why = verdict
                if kind == "cap":
                    try:
                        store.hold(failure, now)
                    except OSError as exc:
                        logger.error("osm-alert: cannot record the held alert: %s", exc)
                logger.warning("osm-alert: %s alert suppressed: %s", failure.unit, why)
                return "suppressed"
            try:
                reservation = store.reserve(key, now)
            except OSError as exc:
                logger.error("osm-alert: cannot record the alert before sending: %s", exc)
    msg = message(reservation.held if reservation is not None else None)
    error: Exception | None = None
    try:
        sent = _deliver_within_deadline(msg)
    except Exception as exc:  # noqa: BLE001 - an alert must never fail its unit
        sent, error = False, exc
    if not sent and store is not None and reservation is not None:
        with store.locked():
            try:
                store.undo(reservation)
            except OSError as exc:
                logger.error("osm-alert: cannot undo the alert record: %s", exc)
    if error is not None:
        logger.error("osm-alert: DELIVERY_FAILED unit=%s to=%s err=%s: %s",
                     failure.unit, ", ".join(recipients.addresses),
                     type(error).__name__, error)
        return "delivery-failed"
    if not sent:
        logger.error("osm-alert: SMTP_HOST unset: the %s alert was not mailed", failure.unit)
        return "no-smtp"
    logger.warning("osm-alert: %s alert mailed to %d recipient(s) (%s)",
                   failure.unit, len(recipients.addresses), recipients.source)
    return "sent"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.ops.alert_notify",
        description="Mail the OSM admins about a failed systemd unit (osm-alert@.service).",
    )
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--unit", help="the failed unit (%%i of osm-alert@)")
    what.add_argument("--refresh-admins", action="store_true",
                      help="only refresh the cached admin list (admins.json) from the "
                           "database; sends nothing (run nightly by the reindex unit)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the mail instead of sending it; no state change")
    parser.add_argument("--force", action="store_true",
                        help="send even when the dedupe window or the hourly cap would hold it")
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0  # a bad call must not fail the alert unit either
    try:
        from src import config  # noqa: PLC0415

        config.init_dotenv()
        if args.refresh_admins:
            if refresh_admin_cache(state_dir_from_env(dict(os.environ))):
                logger.info("osm-alert: admin list refreshed")
        else:
            notify(args.unit, dry_run=args.dry_run, force=args.force)
    except Exception:  # noqa: BLE001 - never fail the alert unit
        logger.exception("osm-alert: unexpected error (%s)", args.unit or "--refresh-admins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
