# src/config.py
"""INI config reader for odoo-semantic-mcp.

Search order:
  1. $ODOO_SEMANTIC_CONF (explicit override)
  2. ~/.odoo-semantic/odoo-semantic.conf (system-wide user config)
  3. ./odoo-semantic.conf (repo-local, dev convenience)

Returns fallback if nothing matches. No env-var fallback at lookup time —
callers pass `fallback=...` explicitly per key.
"""
import configparser
import os
import pathlib

_conf: configparser.ConfigParser | None = None


def _load() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    candidates: list[pathlib.Path] = []
    env_override = os.getenv("ODOO_SEMANTIC_CONF")
    if env_override:
        candidates.append(pathlib.Path(env_override))
    candidates.append(pathlib.Path.home() / ".odoo-semantic" / "odoo-semantic.conf")
    candidates.append(pathlib.Path.cwd() / "odoo-semantic.conf")
    for path in candidates:
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
