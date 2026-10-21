# SPDX-License-Identifier: AGPL-3.0-or-later
"""The indexer CLI honours the admin-tuned embedder read timeout (#383 item 3) - no DB.

``Qwen3Embedder`` resolves ``embedding.timeout_read_seconds`` from the
app_settings overlay ONCE, when it is built. ``main()`` used to build it before
``open_production_pg()`` created the pool, so every CLI run logged
``overlay-only resolve failed: PostgreSQL pool is not initialized`` and used
the constant default instead of the value an admin set. The pool must exist
before the embedder for every subcommand that builds one.

The pool is faked: ``open_production_pg`` installs a stub pool whose
app_settings row holds ``{"v": 777}``.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

import src.db.pg as pg_mod
import src.indexer.__main__ as main_mod
from src.constants import TIMEOUT_EMBEDDER_READ
from src.db.exceptions import PoolNotInitializedError

TUNED = 777
assert TUNED != TIMEOUT_EMBEDDER_READ


class _Cursor:
    def __init__(self):
        self.key = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.key = params[0] if params else None

    def fetchone(self):
        return ({"v": TUNED},) if self.key == "embedding.timeout_read_seconds" else None


class _Conn:
    def cursor(self):
        return _Cursor()


class _Pool:
    @contextmanager
    def checkout(self):
        yield _Conn()


@pytest.fixture
def no_pool_until_open(monkeypatch, tmp_path):
    """No pool exists until open_production_pg() runs, like a fresh CLI process."""
    import src.config as config_mod
    import src.indexer.embedder as embedder_mod

    state = {"pool": None}

    def get_pool():
        if state["pool"] is None:
            raise PoolNotInitializedError(
                "PostgreSQL pool is not initialized. Call init_pool(dsn) at startup."
            )
        return state["pool"]

    def open_production_pg():
        state["pool"] = _Pool()
        return MagicMock()

    monkeypatch.setattr(pg_mod, "get_pool", get_pool)
    monkeypatch.setattr(main_mod, "open_production_pg", open_production_pg)
    monkeypatch.setattr(embedder_mod, "EMBEDDER_BACKEND", "ollama")
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[embedder]\nurl = http://127.0.0.1:9\nmodel = m\ndim = 1024\n")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.setattr(config_mod, "_conf", None)
    return state


def _overlay_warnings(caplog):
    return [r for r in caplog.records if "overlay-only resolve failed" in r.getMessage()]


def test_index_repo_builds_the_embedder_after_the_pool(no_pool_until_open, monkeypatch, caplog):
    seen = {}

    def index_profile(pg, **kw):
        seen["embedder"] = kw["embedder"]
        return {"modules": 0}

    monkeypatch.setattr(main_mod, "index_profile", index_profile)
    with caplog.at_level(logging.WARNING):
        assert main_mod.main(["index-repo", "--profile", "p"]) == 0
    assert _overlay_warnings(caplog) == []
    assert seen["embedder"]._http.timeout.read == TUNED


def test_reembed_stubs_builds_the_embedder_after_the_pool(no_pool_until_open, monkeypatch, caplog):
    seen = {}

    def reembed(pg, **kw):
        seen["embedder"] = kw["embedder"]
        return {"modules_checked": 0, "modules_reembedded": 0, "total_embed_calls": 0}

    monkeypatch.setattr(main_mod, "reembed_stubs_for_profile", reembed)
    with caplog.at_level(logging.WARNING):
        assert main_mod.main(["reembed-stubs", "--profile", "p"]) == 0
    assert _overlay_warnings(caplog) == []
    assert seen["embedder"]._http.timeout.read == TUNED


def test_reembed_stubs_closes_the_connection_when_no_embedder_is_configured(
    no_pool_until_open, monkeypatch, tmp_path,
):
    import src.config as config_mod

    conn = MagicMock()
    monkeypatch.setattr(main_mod, "open_production_pg", lambda: conn)
    empty = tmp_path / "empty.conf"
    empty.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(empty))
    monkeypatch.setattr(config_mod, "_conf", None)
    assert main_mod.main(["reembed-stubs", "--profile", "p"]) == 1
    conn.close.assert_called_once()


# GUARD against a vacuous pass: without a pool the overlay IS missed (the bug).
def test_without_a_pool_the_embedder_misses_the_overlay(no_pool_until_open, caplog):
    with caplog.at_level(logging.WARNING):
        embedder = main_mod._build_embedder()
    assert _overlay_warnings(caplog), "the fake pool no longer reproduces the bug"
    assert embedder._http.timeout.read == TIMEOUT_EMBEDDER_READ
    embedder.close()
