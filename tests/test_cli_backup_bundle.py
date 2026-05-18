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
        # New flow: cmd is `docker compose run --rm -T -v <host_tmpdir>:/backups
        # neo4j neo4j-admin database dump --to-path=/backups neo4j`. We resolve
        # the host_tmpdir from the bind-mount arg and write the dump file there.
        if not neo4j_success:
            return MagicMock(returncode=1, stderr=b"neo4j-admin: database in use")
        # Find the -v "<host>:/backups" arg (compose run preserves -v).
        for i, arg in enumerate(cmd):
            if arg == "-v" and i + 1 < len(cmd) and cmd[i + 1].endswith(":/backups"):
                host_dir = cmd[i + 1].split(":", 1)[0]
                (Path(host_dir) / "neo4j.dump").write_bytes(b"neo4j-dump-stub")
                break
        return MagicMock(returncode=0, stderr=b"")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.return_value = (True,)  # advisory lock acquired
    mock_conn.cursor.return_value = mock_cursor

    def _fake_run(cmd, **kwargs):
        if cmd and "pg_dump" in cmd[0]:
            return _fake_pg_dump(cmd, **kwargs)
        # New Neo4j flow: docker compose stop/run/start + docker inspect.
        if cmd[:3] == ["docker", "compose", "stop"]:
            return MagicMock(returncode=0, stderr=b"")
        if cmd[:3] == ["docker", "compose", "start"]:
            return MagicMock(returncode=0, stderr=b"")
        if cmd[:3] == ["docker", "compose", "run"] and "neo4j-admin" in cmd:
            return _fake_neo4j_dump(cmd, **kwargs)
        if cmd[:2] == ["docker", "inspect"]:
            # _wait_neo4j_healthy polls this — return healthy immediately so
            # the test does not sleep.
            return MagicMock(returncode=0, stdout="healthy\n", stderr="")
        return MagicMock(returncode=0, stderr=b"")

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
    def test_docker_not_found_is_non_fatal(self, tmp_path, monkeypatch, caplog):
        """When docker is missing (compose backup unreachable) backup still
        succeeds — postgres.sql + manifest land in the bundle, neo4j.dump is
        skipped with a logged warning."""
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
            if cmd and "pg_dump" in cmd[0]:
                stdout = kwargs.get("stdout")
                if stdout and hasattr(stdout, "write"):
                    stdout.write(b"-- pg_dump stub\n")
                return MagicMock(returncode=0, stderr=b"")
            if cmd and cmd[0] == "docker":
                raise FileNotFoundError("docker not found")
            return MagicMock(returncode=0)

        with caplog.at_level(logging.WARNING, logger="src.cli"):
            with patch("psycopg2.connect", return_value=mock_conn):
                with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
                    with patch("subprocess.run", side_effect=_fake_run):
                        rc = _cmd_backup(args)

        assert rc == 0, "Backup should succeed even when docker is missing"
        assert out.exists()
        assert any("docker" in r.message.lower() for r in caplog.records)


class TestNeo4jStopDumpStartFlow:
    def test_dump_failure_still_restarts_neo4j(self, tmp_path, monkeypatch):
        """If neo4j-admin dump exits non-zero, the finally clause must still
        invoke `docker compose start neo4j` — preventing a stuck-stopped DB."""
        backup_dir = _make_backup_dir(tmp_path)
        monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
        monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
        monkeypatch.setenv("FERNET_KEY", "fk")

        calls: list[list[str]] = []
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (True,)
        mock_conn.cursor.return_value = mock_cursor

        def _fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd and "pg_dump" in cmd[0]:
                stdout = kwargs.get("stdout")
                if stdout and hasattr(stdout, "write"):
                    stdout.write(b"-- pg_dump stub\n")
                return MagicMock(returncode=0, stderr=b"")
            if cmd[:3] == ["docker", "compose", "run"]:
                return MagicMock(returncode=1, stderr=b"database in use")
            if cmd[:2] == ["docker", "inspect"]:
                return MagicMock(returncode=0, stdout="healthy\n", stderr="")
            return MagicMock(returncode=0, stderr=b"")

        out = backup_dir / "out.tar.gz"
        args = _build_parser().parse_args(["backup", "--output", str(out)])
        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
                with patch("subprocess.run", side_effect=_fake_run):
                    rc = _cmd_backup(args)

        assert rc == 0, (
            "Backup must succeed even if dump step failed "
            "(postgres+manifest still archived)"
        )
        stop_seen = any(c[:3] == ["docker", "compose", "stop"] for c in calls)
        start_seen = any(c[:3] == ["docker", "compose", "start"] for c in calls)
        assert stop_seen, "expected `docker compose stop neo4j` to be invoked"
        assert start_seen, "expected `docker compose start neo4j` after dump failure (finally)"

    def test_dump_command_uses_bind_mount_for_to_path(self):
        """The dump container must bind-mount the host tmpdir to /backups so
        the resulting neo4j.dump lands on the host filesystem (the original
        bug: --to-path used a HOST path that didn't exist in the container)."""
        from src.cli import _backup_neo4j_via_compose

        captured: list[list[str]] = []

        def _capturing_fake(cmd, **kwargs):
            captured.append(list(cmd))
            if cmd[:2] == ["docker", "inspect"]:
                return MagicMock(returncode=0, stdout="healthy\n", stderr="")
            return MagicMock(returncode=1, stderr=b"short-circuit")

        with patch("subprocess.run", side_effect=_capturing_fake):
            _backup_neo4j_via_compose(Path("/host/tmp/abc"))

        run_cmds = [c for c in captured if c[:3] == ["docker", "compose", "run"]]
        assert run_cmds, f"no docker compose run command issued; captured={captured}"
        run_cmd = run_cmds[0]
        assert "-v" in run_cmd, run_cmd
        bind_idx = run_cmd.index("-v") + 1
        assert run_cmd[bind_idx] == "/host/tmp/abc:/backups", run_cmd[bind_idx]
        assert "--to-path=/backups" in run_cmd, run_cmd
        assert "neo4j-admin" in run_cmd, run_cmd
        # --verbose pinned so future failures surface the real error instead
        # of the generic "Dump failed for databases" one-liner.
        assert "--verbose" in run_cmd, run_cmd

    def test_tmpdir_is_chmod_world_writable_before_dump(self, tmp_path):
        """The bind-mounted tmpdir must be chmod 0o777 before the container
        starts so neo4j-admin (running as container's default user, UID 7474
        for the official image) can write neo4j.dump back through the bind
        mount. Without this the dump exits 1 with a generic 'Dump failed'
        message and the bundle silently ships incomplete."""
        from src.cli import _backup_neo4j_via_compose

        host_tmpdir = tmp_path / "bm"
        host_tmpdir.mkdir(mode=0o700)
        assert (host_tmpdir.stat().st_mode & 0o777) == 0o700

        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr=b"")):
            _backup_neo4j_via_compose(host_tmpdir)

        assert (host_tmpdir.stat().st_mode & 0o777) == 0o777, (
            "host_tmpdir must be chmod 0o777 before bind-mount to /backups"
        )


class TestGetLatestMigrationVersion:
    def test_returns_string(self):
        v = _get_latest_migration_version()
        assert isinstance(v, str)
        assert v != ""
