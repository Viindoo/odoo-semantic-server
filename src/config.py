# SPDX-License-Identifier: AGPL-3.0-or-later
# src/config.py
"""INI config reader for odoo-semantic-mcp.

Search order for config FILE (low priority — env vars override):
  1. $ODOO_SEMANTIC_CONF (explicit override)
  2. ~/.odoo-semantic/odoo-semantic.conf (system-wide user config)
  3. ./odoo-semantic.conf (repo-local, dev convenience)

Resolution order for INDIVIDUAL VALUE (per `from_env_or_ini`):
  1. Environment variable (e.g. NEO4J_PASSWORD, PG_DSN) — production override
  2. INI file [section]/key
  3. Caller-provided fallback (or None)

`.env` auto-load (issue #141, ADR-0031): CLI entry points call
`init_dotenv()` from inside their `main()` so that interactive
invocations pick up `.env` automatically. We do NOT call `load_dotenv`
at module import — pytest imports `src.config` to test config helpers,
and an import-time `.env` load would inject stale or template values
(e.g. `.env.example`'s `PG_DSN=postgresql://...:<PASSWORD>@...`) into
the test environment before fixtures get a chance to set theirs.
`override=False` still guarantees that env vars injected by systemd
(`EnvironmentFile=`) or the operator's shell always win.
"""
import configparser
import os
import pathlib
import re

from dotenv import load_dotenv

_conf: configparser.ConfigParser | None = None
_dotenv_initialized: bool = False


def init_dotenv() -> None:
    """Load `.env` from CWD walking up, idempotently.

    Call this from CLI `main()` entry points. Do NOT call from module
    scope or from test code — pytest fixtures inject env explicitly and
    should not see whatever `.env` happens to be in CWD.

    `override=False` ensures values already present in `os.environ`
    (systemd `EnvironmentFile=`, operator's `export`, pytest fixtures)
    are never clobbered; `.env` fills only missing slots.
    """
    global _dotenv_initialized
    if _dotenv_initialized:
        return
    load_dotenv(override=False)
    _dotenv_initialized = True

# Match the password segment in a postgres-style DSN: scheme://user:PASSWORD@host
# Example: postgresql://odoo_semantic:supersecret@localhost:5432/db
_DSN_PASSWORD_RE = re.compile(r"(://[^:/@]+):([^@]+)@")


def _load() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    env_override = os.getenv("ODOO_SEMANTIC_CONF")
    if env_override:
        path = pathlib.Path(env_override)
        if path.is_file():
            parser.read(path)
        return parser  # honor the override; don't fall through to home/cwd
    for path in [
        pathlib.Path.home() / ".odoo-semantic" / "odoo-semantic.conf",
        pathlib.Path.cwd() / "odoo-semantic.conf",
    ]:
        if path.is_file():
            parser.read(path)
            break
    return parser


def get(section: str, key: str, fallback: str | None = None) -> str | None:
    """Return string value for [section]/key, or fallback if missing."""
    global _conf
    if _conf is None:
        _conf = _load()
    return _conf.get(section, key, fallback=fallback)


def from_env_or_ini(
    env_var: str,
    section: str,
    key: str,
    fallback: str | None = None,
) -> str | None:
    """Read a config value with consistent precedence: env var → INI → fallback.

    Empty-string env values fall through to INI (so `unset X` and `export X=`
    behave the same — both mean "no env override").

    Use this instead of bare `config.get()` for any value that should be
    overridable from `.env` / docker-compose / systemd environment.
    """
    val = os.getenv(env_var)
    if val:
        return val
    return get(section, key, fallback=fallback)


def mask_dsn(dsn: str) -> str:
    """Replace the password in a DSN with `***` for safe logging.

    >>> mask_dsn("postgresql://user:secret@host:5432/db")
    'postgresql://user:***@host:5432/db'
    """
    if not dsn:
        return dsn
    return _DSN_PASSWORD_RE.sub(r"\1:***@", dsn)


def dsn_missing_hint(env_var: str = "PG_DSN") -> str:
    """Multi-line error message for missing DSN, surfacing the 3 fix options.

    Use this in every CLI entry point that fails when PG_DSN is absent,
    instead of inlining a short one-liner. Issue #141 — operators kept
    rediscovering the `set -a; . .env; set +a` workaround on every fresh
    deploy.
    """
    return (
        f"✗ PostgreSQL DSN missing.\n"
        f"  Option 1 (dev):  set -a; . .env; set +a  # source .env then retry\n"
        f"  Option 2 (dev):  export {env_var}=postgresql://user:pass@localhost:5432/db\n"
        f"  Option 3 (prod): add `pg_dsn = ...` to [database] section of odoo-semantic.conf\n"
        f"  See docs/deploy.md §3 for full setup."
    )
