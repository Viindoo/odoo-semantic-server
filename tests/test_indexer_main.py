"""Unit tests for src/indexer/__main__.py — no DB or Ollama required."""
from unittest.mock import MagicMock, patch

import src.indexer.__main__ as main_mod


def test_no_embed_flag_skips_embedder(monkeypatch, tmp_path):
    """--no-embed passes embedder=None regardless of config."""
    import src.config as config_mod

    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[embedder]\nurl = http://localhost:11434\n")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None

    with (
        patch("src.indexer.__main__.open_production_pg") as mock_pg,
        patch("src.indexer.__main__.index_profile") as mock_ip,
    ):
        mock_pg.return_value.close = MagicMock()
        mock_ip.return_value = {"modules": 0, "views": 0, "qweb": 0, "embeddings": 0}
        main_mod.main(["--profile", "test_prof", "--no-embed"])

    mock_ip.assert_called_once()
    _, kwargs = mock_ip.call_args
    assert kwargs.get("embedder") is None


def test_embedder_built_when_config_has_url(monkeypatch, tmp_path):
    """embedder is built and passed when [embedder] url is configured."""
    import src.config as config_mod

    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[embedder]\nurl = http://localhost:11434\nmodel = qwen3-embedding-q5km\ndim = 1024\n"
    )
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None

    fake_embedder = object()

    with (
        patch("src.indexer.__main__.open_production_pg") as mock_pg,
        patch("src.indexer.__main__.index_profile") as mock_ip,
        patch("src.indexer.embedder.Qwen3Embedder", return_value=fake_embedder),
    ):
        mock_pg.return_value.close = MagicMock()
        mock_ip.return_value = {"modules": 0, "views": 0, "qweb": 0, "embeddings": 0}
        main_mod.main(["--profile", "test_prof"])

    mock_ip.assert_called_once()
    _, kwargs = mock_ip.call_args
    assert kwargs.get("embedder") is fake_embedder


def test_embedder_none_when_config_missing_url(monkeypatch, tmp_path):
    """embedder=None (with warning) when [embedder] url not in config."""
    import src.config as config_mod

    cfg = tmp_path / "empty.conf"
    cfg.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None

    with (
        patch("src.indexer.__main__.open_production_pg") as mock_pg,
        patch("src.indexer.__main__.index_profile") as mock_ip,
    ):
        mock_pg.return_value.close = MagicMock()
        mock_ip.return_value = {"modules": 0, "views": 0, "qweb": 0, "embeddings": 0}
        main_mod.main(["--profile", "test_prof"])

    mock_ip.assert_called_once()
    _, kwargs = mock_ip.call_args
    assert kwargs.get("embedder") is None


def test_all_flag_passes_embedder_to_index_all(monkeypatch, tmp_path):
    """--all uses index_all and passes embedder=None when --no-embed given."""
    import src.config as config_mod

    cfg = tmp_path / "empty.conf"
    cfg.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None

    with (
        patch("src.indexer.__main__.open_production_pg") as mock_pg,
        patch("src.indexer.__main__.index_all") as mock_ia,
    ):
        mock_pg.return_value.close = MagicMock()
        mock_ia.return_value = {
            "profiles_ok": 0, "profiles_failed": [],
            "modules": 0, "views": 0, "qweb": 0, "embeddings": 0,
        }
        main_mod.main(["--all", "--no-embed"])

    mock_ia.assert_called_once()
    _, kwargs = mock_ia.call_args
    assert kwargs.get("embedder") is None
