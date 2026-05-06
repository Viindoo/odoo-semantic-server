"""Unit test: server.py reads host/port from src.config (no MCP/Neo4j needed)."""
from src import config as config_mod


def test_server_module_uses_config_for_host_port(tmp_path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[server]\nhost = 192.168.42.7\nport = 8888\n"
        "[database]\nneo4j_uri = bolt://localhost:7687\n"
        "neo4j_user = neo4j\nneo4j_password = pw\n"
    )
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None  # invalidate cache

    from src.mcp import server
    assert server._mcp_host() == "192.168.42.7"
    assert server._mcp_port() == 8888


def test_server_falls_back_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(tmp_path / "nope.conf"))
    config_mod._conf = None
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)

    from src.mcp import server
    assert server._mcp_host() == "127.0.0.1"
    assert server._mcp_port() == 8002


def test_get_driver_reads_neo4j_uri_from_config(tmp_path, monkeypatch):
    """_get_driver() reads neo4j_uri from [database] section, not env var."""
    import importlib

    import neo4j

    import src.config as config_mod
    import src.mcp.server as server_mod

    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\nneo4j_uri = bolt://cfg.example.com:7687\n"
        "neo4j_user = cfguser\nneo4j_password = cfgpass\n"
    )
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_USER", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    config_mod._conf = None

    captured: dict = {}
    monkeypatch.setattr(
        neo4j.GraphDatabase, "driver",
        lambda uri, *, auth: captured.update({"uri": uri, "auth": auth}) or object(),
    )

    importlib.reload(server_mod)
    server_mod._driver = None

    server_mod._get_driver()

    assert captured["uri"] == "bolt://cfg.example.com:7687"
    assert captured["auth"] == ("cfguser", "cfgpass")


def test_get_driver_env_overrides_config(tmp_path, monkeypatch):
    """NEO4J_URI env var takes priority over config file in _get_driver()."""
    import importlib

    import neo4j

    import src.config as config_mod
    import src.mcp.server as server_mod

    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\nneo4j_uri = bolt://cfg.example.com:7687\n"
        "neo4j_user = cfguser\nneo4j_password = cfgpass\n"
    )
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.setenv("NEO4J_URI", "bolt://env.example.com:7687")
    monkeypatch.setenv("NEO4J_USER", "envuser")
    monkeypatch.setenv("NEO4J_PASSWORD", "envpass")
    config_mod._conf = None

    captured: dict = {}
    monkeypatch.setattr(
        neo4j.GraphDatabase, "driver",
        lambda uri, *, auth: captured.update({"uri": uri, "auth": auth}) or object(),
    )

    importlib.reload(server_mod)
    server_mod._driver = None

    server_mod._get_driver()

    assert captured["uri"] == "bolt://env.example.com:7687"
    assert captured["auth"] == ("envuser", "envpass")
