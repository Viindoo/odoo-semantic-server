# tests/test_pipeline_config.py
"""Unit tests for pipeline._neo4j_creds() — no Neo4j required."""
import importlib


def test_neo4j_creds_reads_from_config_file(tmp_path, monkeypatch):
    """pipeline._neo4j_creds() reads [database]/neo4j_* from odoo-semantic.conf."""
    import src.config as config_mod

    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\nneo4j_uri = bolt://db.example.com:7687\n"
        "neo4j_user = admin\nneo4j_password = secret\n"
    )
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    # Clear NEO4J_TEST_* and NEO4J_* env vars so config file path is exercised
    monkeypatch.delenv("NEO4J_TEST_URI", raising=False)
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_TEST_USER", raising=False)
    monkeypatch.delenv("NEO4J_USER", raising=False)
    monkeypatch.delenv("NEO4J_TEST_PASSWORD", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    config_mod._conf = None  # invalidate cache

    # Re-import to pick up the monkeypatched env
    import src.indexer.pipeline as pipeline_mod
    importlib.reload(pipeline_mod)

    uri, user, password = pipeline_mod._neo4j_creds()
    assert uri == "bolt://db.example.com:7687"
    assert user == "admin"
    assert password == "secret"


def test_neo4j_creds_env_overrides_config(tmp_path, monkeypatch):
    """NEO4J_* env vars take priority over config file."""
    import src.config as config_mod

    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\nneo4j_uri = bolt://config.example.com:7687\n"
        "neo4j_user = cfguser\nneo4j_password = cfgpass\n"
    )
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.delenv("NEO4J_TEST_URI", raising=False)
    monkeypatch.delenv("NEO4J_TEST_USER", raising=False)
    monkeypatch.delenv("NEO4J_TEST_PASSWORD", raising=False)
    monkeypatch.setenv("NEO4J_URI", "bolt://env.example.com:7687")
    monkeypatch.setenv("NEO4J_USER", "envuser")
    monkeypatch.setenv("NEO4J_PASSWORD", "envpass")
    config_mod._conf = None

    import src.indexer.pipeline as pipeline_mod
    importlib.reload(pipeline_mod)

    uri, user, password = pipeline_mod._neo4j_creds()
    assert uri == "bolt://env.example.com:7687"
    assert user == "envuser"
    assert password == "envpass"


def test_neo4j_creds_fallback_defaults(tmp_path, monkeypatch):
    """_neo4j_creds() raises when no env / no config supplies a password.

    Behavior changed (B2): hardcoded `"password"` fallback removed for security.
    Production must explicitly set NEO4J_PASSWORD or neo4j_password in config.
    """
    import pytest as _pytest  # noqa: I001 — keep grouped near use site

    import src.config as config_mod

    # Point ODOO_SEMANTIC_CONF at an empty file so config yields nothing
    cfg = tmp_path / "empty.conf"
    cfg.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    for key in ("NEO4J_TEST_URI", "NEO4J_URI", "NEO4J_TEST_USER", "NEO4J_USER",
                "NEO4J_TEST_PASSWORD", "NEO4J_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    config_mod._conf = None

    import src.indexer.pipeline as pipeline_mod
    importlib.reload(pipeline_mod)

    with _pytest.raises(RuntimeError, match="Neo4j password missing"):
        pipeline_mod._neo4j_creds()


def test_neo4j_creds_uses_env_when_no_config(tmp_path, monkeypatch):
    """When config file empty but env vars set, _neo4j_creds returns env values."""
    import src.config as config_mod

    cfg = tmp_path / "empty.conf"
    cfg.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    for key in ("NEO4J_TEST_URI", "NEO4J_TEST_USER", "NEO4J_TEST_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "envpw")
    config_mod._conf = None

    import src.indexer.pipeline as pipeline_mod
    importlib.reload(pipeline_mod)

    uri, user, password = pipeline_mod._neo4j_creds()
    assert uri == "bolt://localhost:7687"
    assert user == "neo4j"
    assert password == "envpw"
