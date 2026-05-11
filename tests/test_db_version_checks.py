"""Unit tests for src.db.migrate version checks using mocks (no PostgreSQL required)."""
from unittest.mock import MagicMock, patch

import pytest

from src.db.migrate import _ensure_extension, run_migrations


class TestPostgreSQLVersionCheck:
    """Unit tests for PostgreSQL version validation in run_migrations()."""

    def test_pg_16_passes(self):
        """PostgreSQL 16+ passes the version check."""
        # Mock connection and cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)
        mock_conn.autocommit = False

        # Mock version check returns PG 16.5
        mock_cursor.fetchone.return_value = (160005,)

        # Mock the rest of the function to prevent further execution
        # (we only want to verify it doesn't raise on version check)
        with patch("src.db.migrate._ensure_extension") as mock_ensure, \
             patch("src.db.migrate._run_yoyo"), \
             patch("src.db.migrate._conn_to_uri", return_value="postgresql://mock/db"), \
             patch("builtins.print"):
            mock_ensure.return_value = False
            # Should not raise RuntimeError about PostgreSQL version
            try:
                run_migrations(mock_conn)
            except RuntimeError as e:
                # Only fail if it's the PostgreSQL version error
                if "PostgreSQL 16+ required" in str(e):
                    pytest.fail(f"Should accept PostgreSQL 16.5, got: {e}")
                # Other errors are acceptable (we mocked just enough)

    def test_pg_15_raises(self):
        """PostgreSQL 15 raises RuntimeError."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)

        # Mock version check returns PG 15.4
        mock_cursor.fetchone.return_value = (150004,)

        # Should raise RuntimeError with specific message
        with pytest.raises(RuntimeError, match="PostgreSQL 16\\+ required"):
            run_migrations(mock_conn)

    def test_pg_17_passes(self):
        """PostgreSQL 17+ passes the version check."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)
        mock_conn.autocommit = False

        # Mock version check returns PG 17.0
        mock_cursor.fetchone.return_value = (170000,)

        with patch("src.db.migrate._ensure_extension") as mock_ensure, \
             patch("src.db.migrate._run_yoyo"), \
             patch("src.db.migrate._conn_to_uri", return_value="postgresql://mock/db"), \
             patch("builtins.print"):
            mock_ensure.return_value = False
            try:
                run_migrations(mock_conn)
            except RuntimeError as e:
                if "PostgreSQL 16+ required" in str(e):
                    pytest.fail(f"Should accept PostgreSQL 17.0, got: {e}")


class TestPgvectorVersionCheck:
    """Unit tests for pgvector version validation in _ensure_extension()."""

    def test_pgvector_0_8_passes(self):
        """pgvector 0.8+ passes the version check."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)

        # First call: _vector_extension_available checks if extension exists
        # Second call: retrieve extversion
        mock_cursor.fetchone.side_effect = [
            (1,),  # Extension exists
            ("0.8.2",),  # Version is 0.8.2
        ]

        result = _ensure_extension(mock_conn)
        assert result is True

    def test_pgvector_0_9_passes(self):
        """pgvector 0.9+ passes the version check."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)

        # First call: _vector_extension_available
        # Second call: retrieve extversion
        mock_cursor.fetchone.side_effect = [
            (1,),  # Extension exists
            ("0.9.1",),  # Version is 0.9.1
        ]

        result = _ensure_extension(mock_conn)
        assert result is True

    def test_pgvector_0_7_raises(self):
        """pgvector 0.7 raises RuntimeError."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)

        # First call: _vector_extension_available
        # Second call: retrieve extversion
        mock_cursor.fetchone.side_effect = [
            (1,),  # Extension exists
            ("0.7.5",),  # Version is 0.7.5
        ]

        with pytest.raises(RuntimeError, match="pgvector 0\\.8\\+ required"):
            _ensure_extension(mock_conn)

    def test_pgvector_0_6_raises(self):
        """pgvector 0.6 raises RuntimeError."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)

        mock_cursor.fetchone.side_effect = [
            (1,),  # Extension exists
            ("0.6.0",),  # Version is 0.6.0
        ]

        with pytest.raises(RuntimeError, match="pgvector 0\\.8\\+ required"):
            _ensure_extension(mock_conn)

    def test_pgvector_installed_after_create_extension(self):
        """pgvector installed via CREATE EXTENSION is validated."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)
        mock_conn.autocommit = False

        # _vector_extension_available returns False initially
        # After CREATE EXTENSION, it exists with version 0.8.0
        mock_cursor.fetchone.side_effect = [
            None,  # Extension not found initially
            ("0.8.0",),  # After CREATE EXTENSION, version is 0.8.0
        ]

        result = _ensure_extension(mock_conn)
        assert result is True

    def test_pgvector_installed_but_old_after_create_extension(self):
        """pgvector installed via CREATE EXTENSION but version < 0.8 raises."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)
        mock_conn.autocommit = False

        # _vector_extension_available returns False initially
        # After CREATE EXTENSION, it exists but with old version 0.5.0
        mock_cursor.fetchone.side_effect = [
            None,  # Extension not found initially
            ("0.5.0",),  # After CREATE EXTENSION, version is 0.5.0
        ]

        with pytest.raises(RuntimeError, match="pgvector 0\\.8\\+ required"):
            _ensure_extension(mock_conn)

    def test_pgvector_insufficient_privilege_returns_false(self):
        """CREATE EXTENSION fails with InsufficientPrivilege — returns False."""
        import psycopg2

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)
        mock_conn.autocommit = False

        # _vector_extension_available returns False (extension not found)
        # CREATE EXTENSION raises InsufficientPrivilege
        # We need to handle multiple calls to execute():
        # 1. First execute() in _vector_extension_available → returns False (no fetchone match)
        # 2. Second execute() for CREATE EXTENSION → raises InsufficientPrivilege
        def execute_side_effect(sql):
            if "CREATE EXTENSION" in sql:
                raise psycopg2.errors.InsufficientPrivilege("test error")

        mock_cursor.fetchone.return_value = None
        mock_cursor.execute.side_effect = execute_side_effect

        result = _ensure_extension(mock_conn)
        assert result is False
        mock_conn.rollback.assert_called_once()

    def test_pgvector_version_with_single_digit(self):
        """pgvector version '0' (malformed) should be handled gracefully."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)

        # Version string is just "0" (should be parsed as major=0, minor=0)
        mock_cursor.fetchone.side_effect = [
            (1,),  # Extension exists
            ("0",),  # Malformed version
        ]

        with pytest.raises(RuntimeError, match="pgvector 0\\.8\\+ required"):
            _ensure_extension(mock_conn)

    def test_pgvector_version_1_0_passes(self):
        """pgvector 1.0+ passes the version check."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=None)

        mock_cursor.fetchone.side_effect = [
            (1,),  # Extension exists
            ("1.0.0",),  # Version is 1.0.0
        ]

        result = _ensure_extension(mock_conn)
        assert result is True
