# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify skip-message printed when embedder=None.

Two cases, two messages:
  - `--no-embed` explicit  → short positive note, no "EMBEDDER_URL" reference
  - URL missing in config  → actionable "EMBEDDER_URL not configured" notice
"""
from unittest.mock import MagicMock


def _setup_common(monkeypatch):
    fake_pg = MagicMock()
    monkeypatch.setattr(
        "src.indexer.__main__.open_production_pg", lambda: fake_pg
    )
    monkeypatch.setattr(
        "src.indexer.__main__.index_profile",
        lambda pg, **kw: {"modules": 1, "fields": 0, "methods": 0},
    )


def test_skip_msg_explicit_no_embed(monkeypatch, capsys):
    from src.indexer import __main__ as indexer_main

    _setup_common(monkeypatch)
    rc = indexer_main.main(["index-repo", "--profile", "p1", "--no-embed"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Embeddings skipped (--no-embed)" in out
    assert "EMBEDDER_URL" not in out


def test_skip_msg_when_url_missing(monkeypatch, capsys):
    from src.indexer import __main__ as indexer_main

    _setup_common(monkeypatch)
    monkeypatch.setattr(
        "src.indexer.__main__._build_embedder", lambda: None
    )
    rc = indexer_main.main(["index-repo", "--profile", "p1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "EMBEDDER_URL not configured" in out
