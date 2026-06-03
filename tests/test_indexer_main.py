# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src/indexer/__main__.py — no DB or Ollama required.

WI-F1: updated to use the new subcommand structure
       (`index-repo --profile`, `index-repo --all`, `index-core --source`).
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

import src.indexer.__main__ as main_mod


def test_no_embed_flag_skips_embedder(monkeypatch, tmp_path):
    """index-repo --no-embed passes embedder=None regardless of config."""
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
        main_mod.main(["index-repo", "--profile", "test_prof", "--no-embed"])

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
        main_mod.main(["index-repo", "--profile", "test_prof"])

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
        main_mod.main(["index-repo", "--profile", "test_prof"])

    mock_ip.assert_called_once()
    _, kwargs = mock_ip.call_args
    assert kwargs.get("embedder") is None


def test_all_flag_passes_embedder_to_index_all(monkeypatch, tmp_path):
    """index-repo --all uses index_all and passes embedder=None when --no-embed given."""
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
        main_mod.main(["index-repo", "--all", "--no-embed"])

    mock_ia.assert_called_once()
    _, kwargs = mock_ia.call_args
    assert kwargs.get("embedder") is None


@pytest.mark.parametrize("log_format", ["text", "json"])
@pytest.mark.parametrize(
    "argv_extra, expected_level",
    [
        (["--verbose"], logging.INFO),
        ([], logging.WARNING),
    ],
)
def test_verbose_flag_controls_root_log_level(
    monkeypatch, tmp_path, argv_extra, expected_level, log_format
):
    """--verbose configures the ROOT logger to INFO; its absence leaves WARNING.

    Asserts the observable logging configuration (the real root level after
    main() runs configure_logging), not that a particular level value was passed
    to a mocked helper. Parametrized to prove verbose is the cause: with the
    flag the root level is INFO, without it WARNING. configure_logging is NOT
    mocked here so the real effect is exercised.

    Parametrized over BOTH LOG_FORMAT modes: the json branch uses an explicit
    `root.setLevel(level)`, while the text branch delegates to logging.basicConfig
    (a no-op once the test runner has installed root handlers). The text case is
    the regression guard for the configure_logging fix that adds an unconditional
    `setLevel` to the text branch — without that fix this case goes RED because
    basicConfig silently ignores the requested level.
    """
    import src.config as config_mod

    cfg = tmp_path / "empty.conf"
    cfg.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.setenv("LOG_FORMAT", log_format)
    config_mod._conf = None

    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = root.handlers[:]
    try:
        with (
            patch("src.indexer.__main__.open_production_pg") as mock_pg,
            patch("src.indexer.__main__.index_profile") as mock_ip,
        ):
            mock_pg.return_value.close = MagicMock()
            mock_ip.return_value = {"modules": 0, "views": 0, "qweb": 0, "embeddings": 0}
            main_mod.main(["index-repo", "--profile", "test_prof", *argv_extra])

        assert root.level == expected_level, (
            f"root log level should be {logging.getLevelName(expected_level)} "
            f"for argv_extra={argv_extra}, got {logging.getLevelName(root.level)}"
        )
    finally:
        root.setLevel(saved_level)
        root.handlers = saved_handlers


@pytest.mark.parametrize(
    "argv_extra, expected_progress",
    [
        (["--verbose"], True),
        ([], False),
    ],
)
def test_verbose_flag_controls_progress(
    monkeypatch, tmp_path, argv_extra, expected_progress
):
    """--verbose drives the progress indicator on the indexing run.

    The observable contract is that the indexing run shows a progress bar only
    in verbose mode. We verify the progress mode actually applied to the run via
    the index_profile boundary (the seam where progress is consumed),
    parametrized so absence of the flag proves the default is no-progress.
    """
    import src.config as config_mod

    cfg = tmp_path / "empty.conf"
    cfg.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None

    with (
        patch("src.indexer.__main__.open_production_pg") as mock_pg,
        patch("src.indexer.__main__.index_profile") as mock_ip,
        patch("src.logging_config.configure_logging"),
    ):
        mock_pg.return_value.close = MagicMock()
        mock_ip.return_value = {"modules": 0, "views": 0, "qweb": 0, "embeddings": 0}
        main_mod.main(["index-repo", "--profile", "test_prof", *argv_extra])

    mock_ip.assert_called_once()
    _, kwargs = mock_ip.call_args
    assert kwargs.get("progress") is expected_progress, (
        f"progress should be {expected_progress} for argv_extra={argv_extra}, "
        f"got {kwargs.get('progress')!r}"
    )


def test_index_core_subcommand_calls_index_core(monkeypatch, tmp_path):
    """index-core subcommand dispatches to _run_index_core with correct args."""
    import src.config as config_mod

    cfg = tmp_path / "empty.conf"
    cfg.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None

    odoo_source = tmp_path / "odoo_source"
    odoo_source.mkdir()

    with patch("src.indexer.__main__._run_index_core") as mock_run:
        main_mod.main(["index-core", "--source", str(odoo_source), "--version", "17.0"])

    mock_run.assert_called_once_with(
        source=str(odoo_source),
        version="17.0",
        static_data_dir=None,
    )
