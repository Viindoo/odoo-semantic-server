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
