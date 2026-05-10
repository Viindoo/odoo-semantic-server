"""Verify skip-message printed when embedder=None."""
from unittest.mock import MagicMock


def test_skip_msg_printed_when_no_embed(monkeypatch, capsys):
    from src.indexer import __main__ as indexer_main

    fake_pg = MagicMock()
    monkeypatch.setattr(
        "src.indexer.__main__.open_production_pg", lambda: fake_pg
    )
    monkeypatch.setattr(
        "src.indexer.__main__.index_profile",
        lambda pg, **kw: {"modules": 1, "fields": 0, "methods": 0},
    )

    rc = indexer_main.main(["index-repo", "--profile", "p1", "--no-embed"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Embeddings skipped" in out
    assert "EMBEDDER_URL" in out
