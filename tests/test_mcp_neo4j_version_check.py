"""Unit tests for Neo4j version check in MCP server startup."""
from unittest.mock import MagicMock

import pytest


class TestNeo4jVersionCheck:
    """Test that _get_driver() fails fast on Neo4j < 5.x."""

    def test_neo4j_4x_raises(self, monkeypatch, tmp_path):
        """Neo4j 4.x should raise RuntimeError with clear message."""
        import neo4j

        import src.config as config_mod
        import src.mcp.server as server_mod

        # Setup config
        cfg = tmp_path / "odoo-semantic.conf"
        cfg.write_text(
            "[database]\nneo4j_uri = bolt://localhost:7687\n"
            "neo4j_user = neo4j\nneo4j_password = pw\n"
        )
        monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
        monkeypatch.delenv("CI", raising=False)
        config_mod._conf = None
        server_mod._driver = None
        server_mod._version_checked = False

        # Mock GraphDatabase.driver and session to return Neo4j 4.4.0
        mock_session = MagicMock()
        mock_row = {"v": "4.4.0"}
        mock_session.run.return_value.single.return_value = mock_row
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=None)

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session

        monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **kw: mock_driver)

        # Should raise RuntimeError
        with pytest.raises(RuntimeError) as exc_info:
            server_mod._get_driver()

        assert "5.x+ required" in str(exc_info.value)
        assert "4.4.0" in str(exc_info.value)

    def test_neo4j_5x_passes(self, monkeypatch, tmp_path):
        """Neo4j 5.x should pass without raising."""
        import neo4j

        import src.config as config_mod
        import src.mcp.server as server_mod

        # Setup config
        cfg = tmp_path / "odoo-semantic.conf"
        cfg.write_text(
            "[database]\nneo4j_uri = bolt://localhost:7687\n"
            "neo4j_user = neo4j\nneo4j_password = pw\n"
        )
        monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
        monkeypatch.delenv("CI", raising=False)
        config_mod._conf = None
        server_mod._driver = None
        server_mod._version_checked = False

        # Mock GraphDatabase.driver and session to return Neo4j 5.26.25
        mock_session = MagicMock()
        mock_row = {"v": "5.26.25"}
        mock_session.run.return_value.single.return_value = mock_row
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=None)

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session

        monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **kw: mock_driver)

        # Should not raise
        driver = server_mod._get_driver()
        assert driver is not None

    def test_no_components_row_does_not_raise(self, monkeypatch, tmp_path):
        """If dbms.components() returns None (edge case), should not raise."""
        import neo4j

        import src.config as config_mod
        import src.mcp.server as server_mod

        # Setup config
        cfg = tmp_path / "odoo-semantic.conf"
        cfg.write_text(
            "[database]\nneo4j_uri = bolt://localhost:7687\n"
            "neo4j_user = neo4j\nneo4j_password = pw\n"
        )
        monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
        monkeypatch.delenv("CI", raising=False)
        config_mod._conf = None
        server_mod._driver = None
        server_mod._version_checked = False

        # Mock GraphDatabase.driver and session to return None (no row)
        mock_session = MagicMock()
        mock_session.run.return_value.single.return_value = None
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=None)

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session

        monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **kw: mock_driver)

        # Should not raise (defensive fallback for edge case)
        driver = server_mod._get_driver()
        assert driver is not None

    def test_version_check_skipped_in_ci(self, monkeypatch, tmp_path):
        """When CI=true, version check should be skipped (CI image is pinned)."""
        import neo4j

        import src.config as config_mod
        import src.mcp.server as server_mod

        # Setup config
        cfg = tmp_path / "odoo-semantic.conf"
        cfg.write_text(
            "[database]\nneo4j_uri = bolt://localhost:7687\n"
            "neo4j_user = neo4j\nneo4j_password = pw\n"
        )
        monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
        monkeypatch.setenv("CI", "true")
        config_mod._conf = None
        server_mod._driver = None
        server_mod._version_checked = False

        # Mock driver that would fail if version check runs
        mock_driver = MagicMock()
        mock_driver.session.side_effect = RuntimeError("Should not reach version check in CI")

        monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **kw: mock_driver)

        # Should not raise (version check skipped)
        driver = server_mod._get_driver()
        assert driver is not None

    def test_version_checked_flag_persists(self, monkeypatch, tmp_path):
        """Once _version_checked is True, subsequent calls should skip check."""
        import neo4j

        import src.config as config_mod
        import src.mcp.server as server_mod

        # Setup config
        cfg = tmp_path / "odoo-semantic.conf"
        cfg.write_text(
            "[database]\nneo4j_uri = bolt://localhost:7687\n"
            "neo4j_user = neo4j\nneo4j_password = pw\n"
        )
        monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
        monkeypatch.delenv("CI", raising=False)
        config_mod._conf = None
        server_mod._driver = None
        server_mod._version_checked = False

        call_count = [0]

        def mock_driver_factory(*args, **kwargs):
            mock_session = MagicMock()
            mock_row = {"v": "5.26.25"}
            mock_session.run.return_value.single.return_value = mock_row
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=None)

            mock_driver = MagicMock()
            mock_driver.session.return_value = mock_session

            # Track how many times session() is called
            original_session = mock_driver.session

            def tracked_session(*a, **kw):
                call_count[0] += 1
                return original_session(*a, **kw)

            mock_driver.session = tracked_session
            return mock_driver

        monkeypatch.setattr(neo4j.GraphDatabase, "driver", mock_driver_factory)

        # First call should check version
        server_mod._get_driver()
        assert call_count[0] == 1

        # Second call should reuse _driver without creating new session
        # (since _version_checked=True, it skips the check)
        server_mod._get_driver()
        assert call_count[0] == 1  # No new session created
