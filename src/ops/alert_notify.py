# SPDX-License-Identifier: AGPL-3.0-or-later
# src/ops/alert_notify.py
"""Mail the OSM admins when a systemd unit fails (``osm-alert@.service``, #383).

Every OSM unit has ``OnFailure=osm-alert@%n``. systemd then starts
``osm-alert@<unit>.service``, which runs::

    python -m src.ops.alert_notify --unit %i

What deserves an alert: a failure that stops OSM from reflecting the code and
that the system cannot heal by itself - a unit crash or non-zero exit, the
database or Neo4j down, a DNS / network outage, a repo that cannot be cloned,
the embedder down for a whole run, and exit 3 of the nightly reindex (indexed,
but a lifecycle hold needs an operator). Precedent: from 2026-08-25 to
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
   Each successful read refreshes ``admins.json`` in the state directory; when
   the database is unreachable (exactly when alerts matter) the alert goes to
   that last known list plus ``OSM_ALERT_FALLBACK_TO``.
5. Dedupe: one mail per (unit, exit status, attention block) every
   ``OSM_ALERT_REPEAT_HOURS`` (24); at most ``OSM_ALERT_MAX_PER_HOUR`` (6)
   mails per hour over all units. A test alert skips the dedupe, not the cap.
6. Sends through :mod:`src.mailer` (the SES SMTP settings of the Web UI).
   Secrets in the log excerpt are redacted first.

State lives in ``$STATE_DIRECTORY`` (``StateDirectory=osm-alert``), else
``OSM_ALERT_STATE_DIR``, else ``~/.local/state/osm-alert``.

Manual use::

    python -m src.ops.alert_notify --unit odoo-semantic-reindex --dry-run
    python -m src.ops.alert_notify --unit odoo-semantic-reindex --force
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
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from src import mailer

logger = logging.getLogger("osm-alert")

TAIL_LINES = 60
DEFAULT_REPEAT_HOURS = 24.0
DEFAULT_MAX_PER_HOUR = 6
SMTP_TIMEOUT_S = 20.0
DB_CONNECT_TIMEOUT_S = 5
CMD_TIMEOUT_S = 15
# Bytes read from the end of a unit log file: the nightly log is rotated daily
# and one run writes a few KB, so this always covers the current run.
_LOG_READ_BYTES = 512 * 1024
_MAX_LINE_CHARS = 500
_MAX_BODY_CHARS = 20_000
_HISTORY_KEEP_S = 7 * 24 * 3600

RUN_MARKER = "osm-run-start"
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
    "3": "indexed, but the module lifecycle needs an operator",
    "4": "the lifecycle audit found drift",
}
_INDEXER_UNITS = ("odoo-semantic-reindex", "odoo-semantic-lifecycle-audit")


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

    @property
    def is_test(self) -> bool:
        """A unit systemd does not know: the documented smoke test."""
        return not self.known

    def status_label(self) -> str:
        if self.exit_code in ("killed", "dumped"):
            return f"signal {self.exit_status}"
        return f"exit {self.exit_status}"

    def meaning(self) -> str | None:
        if self.exit_status in _SYSTEMD_STATUS:
            return _SYSTEMD_STATUS[self.exit_status]
        base = self.unit.removesuffix(".service")
        if base in _INDEXER_UNITS and self.exit_code in ("", "exited"):
            return _INDEXER_STATUS.get(self.exit_status)
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


def read_unit_failure(unit: str, env: dict[str, str]) -> UnitFailure:
    """The failure of *unit*: ``$MONITOR_*`` first, ``systemctl show`` for the rest."""
    unit = normalize_unit(env.get("MONITOR_UNIT") or unit)
    failure = UnitFailure(unit=unit)
    if env.get("MONITOR_SERVICE_RESULT") or env.get("MONITOR_EXIT_STATUS"):
        failure.from_monitor = True
        failure.result = env.get("MONITOR_SERVICE_RESULT") or failure.result
        failure.exit_code = env.get("MONITOR_EXIT_CODE", "")
        failure.exit_status = env.get("MONITOR_EXIT_STATUS") or failure.exit_status
        failure.invocation_id = env.get("MONITOR_INVOCATION_ID", "")
    fields = "LoadState,Result,ExecMainCode,ExecMainStatus,InvocationID,ExecMainStartTimestamp"
    text = _run_cmd(["systemctl", "show", "--timestamp=unix", "-p", fields, unit])
    if text is None:  # systemd < 248 has no --timestamp=; the start time stays unknown
        text = _run_cmd(["systemctl", "show", "-p", fields, unit])
    props = _parse_show(text)
    if props.get("LoadState") == "not-found":
        failure.known = False
    if not failure.from_monitor:
        failure.result = props.get("Result") or failure.result
        failure.exit_code = _EXEC_MAIN_CODE.get(props.get("ExecMainCode", ""), "")
        status = props.get("ExecMainStatus")
        if status:
            failure.exit_status = status
        failure.invocation_id = props.get("InvocationID", "")
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

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S,
    ), "[private key redacted]"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/@\s]+):([^@\s]+)@", re.I), r"\1:***@"),
    (re.compile(
        r"(\b[A-Za-z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY|FERNET_KEY)"
        r"[A-Za-z0-9_]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)",
        re.I,
    ), r"\1\2***"),
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I), r"\1 ***"),
    (re.compile(r"\bosm_[A-Za-z0-9_-]{16,}"), "osm_***"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), r"\1***"),
)


def redact(text: str) -> str:
    """Mask credentials a log excerpt may carry before it leaves the host."""
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
        dsn, connect_timeout=DB_CONNECT_TIMEOUT_S, options="-c statement_timeout=5000",
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


def _read_cache(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _clean_addresses(data.get("emails") or []) if isinstance(data, dict) else []


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def resolve_recipients(state_dir: Path | None, env: dict[str, str], *,
                       write_cache: bool = True) -> Recipients:
    """DB admins (cached on success unless *write_cache* is False); when the DB
    cannot give one, the cached list plus ``OSM_ALERT_FALLBACK_TO``."""
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
                try:
                    _write_json_atomic(cache, {"emails": admins, "updated_at": time.time()})
                except OSError as exc:
                    logger.warning("osm-alert: cannot write %s: %s", cache, exc)
            return Recipients(admins, "active Web UI admins (database)")
        reason = "no active Web UI admin has an email (webui_users)"
    logger.error("osm-alert: %s; using the fallback recipients", reason)
    cached = _read_cache(cache) if cache is not None else []
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


def dedupe_key(failure: UnitFailure, attention: list[str]) -> str:
    material = "\n".join([failure.unit, failure.exit_status, *(ln.strip() for ln in attention)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AlertState:
    """Sent-alert history in ``state.json``, serialized by an flock."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "state.json"
        self._lock_path = state_dir / "state.lock"
        self.data: dict = {"sent": {}, "history": []}

    @contextlib.contextmanager
    def locked(self):
        """Hold the state lock; an unusable state directory degrades to "no
        dedupe" (the alert still goes out) rather than losing the alert."""
        try:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            logger.error("osm-alert: state lock %s unusable, sending without dedupe: %s",
                         self._lock_path, exc)
            yield self
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = {
                        "sent": dict(raw.get("sent") or {}),
                        "history": list(raw.get("history") or []),
                    }
            except (OSError, ValueError):
                pass
            yield self
        finally:
            os.close(fd)

    def decide(self, key: str, now: float, *, repeat_s: float, max_per_hour: int,
               dedupe: bool = True) -> str | None:
        """None to send, else why the alert is suppressed."""
        last = self.data["sent"].get(key)
        if dedupe and last is not None and now - float(last) < repeat_s:
            hours = (now - float(last)) / 3600
            return f"same alert sent {hours:.1f}h ago (repeat after {repeat_s / 3600:g}h)"
        recent = [t for t in self.data["history"] if now - float(t) < 3600]
        if len(recent) >= max_per_hour:
            return f"rate cap reached ({len(recent)} mails in the last hour, max {max_per_hour})"
        return None

    def record(self, key: str, now: float) -> None:
        self.data["sent"] = {
            k: t for k, t in self.data["sent"].items() if now - float(t) < _HISTORY_KEEP_S
        }
        self.data["sent"][key] = now
        self.data["history"] = [
            t for t in self.data["history"] if now - float(t) < 3600
        ] + [now]
        _write_json_atomic(self.path, self.data)


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
    tail = output.lines[-TAIL_LINES:]
    header = f"Last {len(tail)} lines of output ({output.source})"
    if output.note:
        header += f" - {output.note}"
    lines += ["", header + ":"]
    lines += [ln[:_MAX_LINE_CHARS] for ln in tail] or ["(no output found)"]
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
    msg["To"] = ", ".join(recipients.addresses)
    msg.set_content(body)
    return msg


def _deliver(msg: EmailMessage) -> bool:
    return mailer.send(msg, timeout=SMTP_TIMEOUT_S)


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
    state_dir = state_dir_from_env(env)
    recipients = resolve_recipients(state_dir, env, write_cache=not dry_run)
    repeat_hours = _float_env(env, "OSM_ALERT_REPEAT_HOURS", DEFAULT_REPEAT_HOURS)
    max_per_hour = int(_float_env(env, "OSM_ALERT_MAX_PER_HOUR", DEFAULT_MAX_PER_HOUR))
    msg = build_message(failure, output, attention, recipients, host=host, now=now,
                        repeat_hours=repeat_hours)
    if dry_run:
        print(f"Subject: {msg['Subject']}\nTo: {msg['To']}\n\n{msg.get_content()}")
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
    key = dedupe_key(failure, attention)
    store = AlertState(state_dir) if state_dir is not None else None
    with store.locked() if store is not None else contextlib.nullcontext():
        if store is not None and not force:
            why = store.decide(key, now, repeat_s=repeat_hours * 3600,
                               max_per_hour=max_per_hour, dedupe=not failure.is_test)
            if why:
                logger.warning("osm-alert: %s alert suppressed: %s", failure.unit, why)
                return "suppressed"
        try:
            sent = _deliver(msg)
        except Exception as exc:  # noqa: BLE001 - an alert must never fail its unit
            logger.error("osm-alert: DELIVERY_FAILED unit=%s to=%s err=%s: %s",
                         failure.unit, msg["To"], type(exc).__name__, exc)
            return "delivery-failed"
        if not sent:
            logger.error("osm-alert: SMTP_HOST unset: the %s alert was not mailed", failure.unit)
            return "no-smtp"
        if store is not None:
            try:
                store.record(key, now)
            except OSError as exc:
                logger.error("osm-alert: cannot record the sent alert: %s", exc)
    logger.warning("osm-alert: %s alert mailed to %d recipient(s) (%s)",
                   failure.unit, len(recipients.addresses), recipients.source)
    return "sent"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.ops.alert_notify",
        description="Mail the OSM admins about a failed systemd unit (osm-alert@.service).",
    )
    parser.add_argument("--unit", required=True, help="the failed unit (%%i of osm-alert@)")
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
        notify(args.unit, dry_run=args.dry_run, force=args.force)
    except Exception:  # noqa: BLE001 - never fail the alert unit
        logger.exception("osm-alert: unexpected error while alerting on %s", args.unit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
