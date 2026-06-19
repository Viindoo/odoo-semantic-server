# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_setup_pattern_indexes.py
"""No-DB unit tests for Neo4jWriter.setup_pattern_indexes (fix 2a).

A patterns-only reseed (``seed_patterns._write_neo4j``) must issue ONLY the 3
``PatternExample`` index statements, not the full ~33-statement schema setup of
``setup_indexes()``. These tests capture the Cypher run() calls via a fake
session — no live Neo4j required (runs in the ``not neo4j and not postgres``
lane).
"""

from __future__ import annotations

import pytest

import src.indexer.writer_neo4j as writer_mod
from src.indexer.writer_neo4j import Neo4jWriter


class _FakeSession:
    def __init__(self, sink: list[str]):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, stmt, *args, **kwargs):
        self._sink.append(stmt)


class _FakeDriver:
    def __init__(self):
        self.statements: list[str] = []

    def session(self, *args, **kwargs):
        return _FakeSession(self.statements)

    def close(self):
        pass


@pytest.fixture
def fake_writer(monkeypatch):
    """Neo4jWriter whose driver is a fake capturing every run() statement."""
    fake = _FakeDriver()
    monkeypatch.setattr(
        writer_mod.GraphDatabase, "driver", lambda *a, **k: fake
    )
    w = Neo4jWriter(uri="bolt://x", user="u", password="p")
    return w, fake


# Exactly the 3 PatternExample indexes — verbatim from setup_indexes().
_EXPECTED_PATTERN_INDEXES = [
    "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample) ON (n.pattern_id)",
    "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample)"
    " ON (n.language, n.odoo_version_min)",
    "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample) ON (n.category)",
]


def test_setup_pattern_indexes_issues_only_three(fake_writer):
    w, fake = fake_writer
    w.setup_pattern_indexes()

    assert len(fake.statements) == 3, (
        f"expected exactly 3 PatternExample index statements, "
        f"got {len(fake.statements)}: {fake.statements}"
    )
    assert fake.statements == _EXPECTED_PATTERN_INDEXES


def test_setup_pattern_indexes_only_touches_patternexample(fake_writer):
    w, fake = fake_writer
    w.setup_pattern_indexes()

    # Every statement targets the PatternExample label and nothing else.
    for stmt in fake.statements:
        assert "PatternExample" in stmt
    joined = " ".join(fake.statements)
    for other_label in ("Module", "Model", "Field", "Method", "TestClass",
                         "Stylesheet", "CoreSymbol", "LintRule"):
        assert other_label not in joined, (
            f"setup_pattern_indexes leaked a {other_label} index"
        )


def test_setup_indexes_still_full_schema(fake_writer):
    """Regression guard: the FULL setup_indexes still covers every label."""
    w, fake = fake_writer
    w.setup_indexes()

    joined = " ".join(fake.statements)
    # Full setup is unchanged — it must still create non-pattern indexes.
    for label in ("Module", "Model", "Field", "Method", "PatternExample",
                  "TestClass", "Stylesheet"):
        assert label in joined
    # And it issues far more than the 3 pattern indexes.
    assert len(fake.statements) > 3
