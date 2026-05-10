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

This keeps the `.env` file (read by docker-compose + shells) and
`odoo-semantic.conf` (read by Python app) consistent: env vars always win,
INI file is canonical default. See README §Configuration.
"""
import configparser
import os
import pathlib
import re

_conf: configparser.ConfigParser | None = None

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
