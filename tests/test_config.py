# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src.config — INI reader, no DB needed."""
import textwrap
from pathlib import Path

import pytest

from src import config as config_mod


@pytest.fixture(autouse=True)
def reset_config_cache():
    """src.config caches the parser at module level — reset before/after each test."""
    config_mod._conf = None
    yield
    config_mod._conf = None


def test_reads_from_explicit_path(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(textwrap.dedent("""
        [database]
        neo4j_uri = bolt://1.2.3.4:7687
        neo4j_user = neo
        neo4j_password = secret

        [server]
        host = 127.0.0.1
        port = 8002
    """).strip())
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))

    assert config_mod.get("database", "neo4j_uri") == "bolt://1.2.3.4:7687"
    assert config_mod.get("server", "port") == "8002"


def test_fallback_when_key_missing(tmp_path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[server]\nhost = 127.0.0.1\n")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    assert config_mod.get("server", "port", fallback="8002") == "8002"


def test_fallback_when_section_missing(tmp_path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[other]\nkey = val\n")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    assert config_mod.get("server", "port", fallback="8002") == "8002"


def test_missing_file_returns_fallback(tmp_path, monkeypatch):
    """When ODOO_SEMANTIC_CONF points at non-existent file, return empty (don't fall through)."""
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(tmp_path / "nope.conf"))
    # Even if cwd has a config, it must NOT be read — env override takes priority
    (tmp_path / "odoo-semantic.conf").write_text("[server]\nhost = should-not-see\n")
    monkeypatch.chdir(tmp_path)
    assert config_mod.get("server", "host", fallback="0.0.0.0") == "0.0.0.0"


def test_searches_repo_local_when_no_env(tmp_path, monkeypatch):
    """Without ODOO_SEMANTIC_CONF, falls back to ./odoo-semantic.conf in cwd."""
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[server]\nhost = repo-local\n")
    monkeypatch.delenv("ODOO_SEMANTIC_CONF", raising=False)
    monkeypatch.chdir(tmp_path)
    # Override HOME so home-dir lookup misses
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    assert config_mod.get("server", "host", fallback="X") == "repo-local"


# --- from_env_or_ini ---------------------------------------------------------

def test_from_env_or_ini_env_wins_over_ini(tmp_path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[database]\nneo4j_password = from-ini\n")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.setenv("NEO4J_PASSWORD", "from-env")
    assert config_mod.from_env_or_ini(
        "NEO4J_PASSWORD", "database", "neo4j_password"
    ) == "from-env"


def test_from_env_or_ini_falls_through_when_env_unset(tmp_path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[database]\nneo4j_password = from-ini\n")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    assert config_mod.from_env_or_ini(
        "NEO4J_PASSWORD", "database", "neo4j_password"
    ) == "from-ini"


def test_from_env_or_ini_empty_env_falls_through(tmp_path, monkeypatch):
    """Empty-string env var is treated same as unset (falls through to INI)."""
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[database]\nneo4j_password = from-ini\n")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.setenv("NEO4J_PASSWORD", "")
    assert config_mod.from_env_or_ini(
        "NEO4J_PASSWORD", "database", "neo4j_password"
    ) == "from-ini"


def test_from_env_or_ini_uses_fallback_when_both_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(tmp_path / "nope.conf"))
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    assert config_mod.from_env_or_ini(
        "NEO4J_PASSWORD", "database", "neo4j_password", fallback="default"
    ) == "default"


def test_from_env_or_ini_returns_none_when_no_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(tmp_path / "nope.conf"))
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    assert config_mod.from_env_or_ini(
        "NEO4J_PASSWORD", "database", "neo4j_password"
    ) is None


# --- mask_dsn ----------------------------------------------------------------

def test_mask_dsn_postgres():
    dsn = "postgresql://odoo_semantic:supersecret@localhost:5432/odoo_semantic"
    masked = config_mod.mask_dsn(dsn)
    assert "supersecret" not in masked
    assert masked == "postgresql://odoo_semantic:***@localhost:5432/odoo_semantic"


def test_mask_dsn_special_chars_in_password():
    dsn = "postgresql://user:p@ss!w0rd@host:5432/db"  # @ in password
    masked = config_mod.mask_dsn(dsn)
    assert "p@ss!w0rd" not in masked or masked.count("@") == 1


def test_mask_dsn_no_password_segment_unchanged():
    dsn = "postgresql://localhost:5432/db"
    assert config_mod.mask_dsn(dsn) == dsn


def test_mask_dsn_empty_or_none():
    assert config_mod.mask_dsn("") == ""
    assert config_mod.mask_dsn(None) is None
