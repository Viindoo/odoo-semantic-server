# tests/test_cli_backup_bundle.py
"""Unit tests for the extended _cmd_backup — tar.gz bundle (M9 W-BK)."""
import hashlib
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.cli import (
    _build_parser,
    _cmd_backup,
    _encrypt_with_passphrase,
    _get_latest_migration_version,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_backup_dir(tmp_path: Path) -> Path:
    bd = tmp_path / "backup"
    bd.mkdir()
    return bd


def _run_backup(
    tmp_path: Path,
    output_rel: str = "backup/out.tar.gz",
    *,
    monkeypatch,
    pg_dsn: str = "postgresql://user:pw@localhost/db",
    bundle_passphrase_env: str = "",
    passphrase: str = "",
    fernet_key: str = "test-fernet-key",
    neo4j_success: bool = False,
):
    """Run _cmd_backup with mocked subprocess and advisory lock, return (rc, tar_path)."""
    backup_dir = _make_backup_dir(tmp_path)
    monkeypatch.setenv("PG_DSN", pg_dsn)
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("FERNET_KEY", fernet_key)

    if bundle_passphrase_env and passphrase:
        monkeypatch.setenv(bundle_passphrase_env, passphrase)

    output_path = tmp_path / output_rel

    args_list = ["backup", "--output", str(output_path)]
    if bundle_passphrase_env:
        args_list += ["--bundle-passphrase-env", bundle_passphrase_env]
    args = _build_parser().parse_args(args_list)

    def _fake_pg_dump(cmd, **kwargs):
        # pg_dump writes to stdout now (no -f); write stub bytes to the stdout handle
        stdout = kwargs.get("stdout")
        if stdout and hasattr(stdout, "write"):
            stdout.write(b"-- pg_dump stub\n")
        return MagicMock(returncode=0, stderr=b"")

    def _fake_neo4j_dump(cmd, **kwargs):
        if neo4j_success:
            # neo4j-admin writes to --to-path <dir>/neo4j.dump
            to_path_idx = cmd.index("--to-path") + 1
            (Path(cmd[to_path_idx]) / "neo4j.dump").write_bytes(b"neo4j-dump-stub")
            return MagicMock(returncode=0, stderr="")
        return MagicMock(returncode=1, stderr="neo4j not running")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.return_value = (True,)  # advisory lock acquired
    mock_conn.cursor.return_value = mock_cursor

    def _fake_run(cmd, **kwargs):
        if "pg_dump" in cmd[0]:
            return _fake_pg_dump(cmd, **kwargs)
        if "neo4j-admin" in cmd[0]:
            return _fake_neo4j_dump(cmd, **kwargs)
        return MagicMock(returncode=0, stderr="")

    with patch("psycopg2.connect", return_value=mock_conn):
        with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
            with patch("subprocess.run", side_effect=_fake_run):
                rc = _cmd_backup(args)

    return rc, output_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBackupWritesTarGzWithComponents:
    def test_tar_gz_contains_postgres_sql_and_manifest(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch)
        assert rc == 0, f"Expected rc=0, got {rc}"
        assert out.exists(), "Output tar.gz not created"

        with tarfile.open(str(out), "r:gz") as tar:
            names = tar.getnames()
        assert "postgres.sql" in names
        assert "manifest.json" in names

    def test_tar_gz_contains_neo4j_dump_when_available(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch, neo4j_success=True)
        assert rc == 0
        with tarfile.open(str(out), "r:gz") as tar:
            names = tar.getnames()
        assert "neo4j.dump" in names

    def test_tar_gz_no_neo4j_when_unavailable(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch, neo4j_success=False)
        assert rc == 0
        with tarfile.open(str(out), "r:gz") as tar:
            names = tar.getnames()
        assert "neo4j.dump" not in names
        assert "postgres.sql" in names


class TestBackupRejectsOutputOutsideBackupDir:
    def test_rejects_outside_path(self, tmp_path, monkeypatch):
        backup_dir = _make_backup_dir(tmp_path)
        monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
        monkeypatch.setenv("BACKUP_DIR", str(backup_dir))

        # Output is in /tmp, not under backup_dir
        outside = tmp_path / "outside" / "dump.tar.gz"
        args = _build_parser().parse_args(["backup", "--output", str(outside)])

        rc = _cmd_backup(args)
        assert rc == 1, "Expected failure for path outside BACKUP_DIR"

    def test_rejects_non_tar_gz_extension(self, tmp_path, monkeypatch):
        backup_dir = _make_backup_dir(tmp_path)
        monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
        monkeypatch.setenv("BACKUP_DIR", str(backup_dir))

        bad = backup_dir / "dump.sql"
        args = _build_parser().parse_args(["backup", "--output", str(bad)])
        rc = _cmd_backup(args)
        assert rc == 1

    def test_accepts_path_inside_backup_dir(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch)
        assert rc == 0


class TestBackupAdvisoryLockPreventsConcurrent:
    def test_advisory_lock_not_acquired_returns_nonzero(self, tmp_path, monkeypatch):
        backup_dir = _make_backup_dir(tmp_path)
        monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
        monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
        monkeypatch.setenv("FERNET_KEY", "fk")

        out = backup_dir / "out.tar.gz"
        args = _build_parser().parse_args(["backup", "--output", str(out)])

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)
        # Advisory lock NOT acquired
        mock_cursor.fetchone.return_value = (False,)
        mock_conn.cursor.return_value = mock_cursor

        with patch("psycopg2.connect", return_value=mock_conn):
            rc = _cmd_backup(args)

        assert rc != 0, "Expected non-zero when advisory lock cannot be acquired"


class TestManifestContainsSchemaVersion:
    def test_manifest_has_schema_version_and_created_at(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch)
        assert rc == 0

        with tarfile.open(str(out), "r:gz") as tar:
            member = tar.getmember("manifest.json")
            f = tar.extractfile(member)
            manifest = json.loads(f.read())

        assert "created_at" in manifest
        assert "schema_version" in manifest
        assert manifest["schema_version"] != ""
        assert "components" in manifest
        assert any(c["file"] == "postgres.sql" for c in manifest["components"])

    def test_manifest_components_have_sha256(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch)
        assert rc == 0

        with tarfile.open(str(out), "r:gz") as tar:
            f = tar.extractfile(tar.getmember("manifest.json"))
            manifest = json.loads(f.read())

        for comp in manifest["components"]:
            assert "file" in comp
            assert "sha256" in comp
            assert len(comp["sha256"]) == 64  # SHA-256 hex digest


class TestFernetEncryptionWithPassphrase:
    def test_fernet_enc_present_when_passphrase_provided(self, tmp_path, monkeypatch):
        rc, out = _run_backup(
            tmp_path,
            monkeypatch=monkeypatch,
            bundle_passphrase_env="MY_PASSPHRASE",
            passphrase="supersecret",
            fernet_key="my-fernet-key-value",
        )
        assert rc == 0

        with tarfile.open(str(out), "r:gz") as tar:
            names = tar.getnames()
        assert "fernet.enc" in names

    def test_fernet_enc_decrypts_to_original_key(self, tmp_path, monkeypatch):
        import base64

        from cryptography.fernet import Fernet

        fernet_key = "my-fernet-key-value"
        passphrase = "testpassphrase123"

        rc, out = _run_backup(
            tmp_path,
            monkeypatch=monkeypatch,
            bundle_passphrase_env="PP_ENV",
            passphrase=passphrase,
            fernet_key=fernet_key,
        )
        assert rc == 0

        # Extract and decrypt fernet.enc
        with tarfile.open(str(out), "r:gz") as tar:
            f = tar.extractfile(tar.getmember("fernet.enc"))
            enc_data = f.read()

        # Decrypt using same PBKDF2 derivation as _encrypt_with_passphrase
        import hashlib
        salt = enc_data[:16]
        token = enc_data[16:]
        key_bytes = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, 100_000, dklen=32)
        derived_key = base64.urlsafe_b64encode(key_bytes)
        decrypted = Fernet(derived_key).decrypt(token)
        assert decrypted.decode() == fernet_key

    def test_fernet_enc_absent_when_no_passphrase(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch)
        assert rc == 0
        with tarfile.open(str(out), "r:gz") as tar:
            names = tar.getnames()
        assert "fernet.enc" not in names

    def test_encrypt_with_passphrase_round_trip(self):
        """_encrypt_with_passphrase / decrypt round-trip (unit test, no subprocess)."""
        import base64

        from cryptography.fernet import Fernet

        plaintext = "my-secret-fernet-key"
        passphrase = "my-passphrase"

        encrypted = _encrypt_with_passphrase(plaintext, passphrase)
        assert len(encrypted) > 16  # salt + token

        salt = encrypted[:16]
        token = encrypted[16:]
        key_bytes = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, 100_000, dklen=32)
        derived_key = base64.urlsafe_b64encode(key_bytes)
        recovered = Fernet(derived_key).decrypt(token).decode()
        assert recovered == plaintext


class TestNeo4jMissingLogsWarningNotFatal:
    def test_neo4j_admin_not_found_is_non_fatal(self, tmp_path, monkeypatch, caplog):
        import logging
        backup_dir = _make_backup_dir(tmp_path)
        monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
        monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
        monkeypatch.setenv("FERNET_KEY", "fk")

        out = backup_dir / "out.tar.gz"
        args = _build_parser().parse_args(["backup", "--output", str(out)])

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (True,)
        mock_conn.cursor.return_value = mock_cursor

        def _fake_run(cmd, **kwargs):
            if "pg_dump" in cmd[0]:
                # pg_dump writes to stdout (no -f); write stub bytes to the stdout handle
                stdout = kwargs.get("stdout")
                if stdout and hasattr(stdout, "write"):
                    stdout.write(b"-- pg_dump stub\n")
                return MagicMock(returncode=0, stderr=b"")
            if "neo4j-admin" in cmd[0]:
                raise FileNotFoundError("neo4j-admin not found")
            return MagicMock(returncode=0)

        with caplog.at_level(logging.WARNING, logger="src.cli"):
            with patch("psycopg2.connect", return_value=mock_conn):
                with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
                    with patch("subprocess.run", side_effect=_fake_run):
                        rc = _cmd_backup(args)

        assert rc == 0, "Backup should succeed even when neo4j-admin is missing"
        assert out.exists()
        # Warning should mention neo4j-admin
        assert any("neo4j-admin" in r.message for r in caplog.records)


class TestGetLatestMigrationVersion:
    def test_returns_string(self):
        v = _get_latest_migration_version()
        assert isinstance(v, str)
        assert v != ""
