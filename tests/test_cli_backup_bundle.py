# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_cli_backup_bundle.py
"""Unit tests for the extended _cmd_backup — tar.gz bundle (M9 W-BK).

Neo4j backup now uses _export_neo4j_online() (Bolt driver, online, no APOC)
instead of the old stop-dump-start docker-compose flow.  The old
`_backup_neo4j_via_compose` / `_wait_neo4j_healthy` helpers are removed.
"""
import hashlib
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.cli import (
    _DEFAULT_KEEP_BUNDLES,
    _build_parser,
    _cmd_backup,
    _encrypt_with_passphrase,
    _get_latest_migration_version,
    _props_to_cypher,
    _prune_old_bundles,
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
    neo4j_password: str = "",
):
    """Run _cmd_backup with mocked subprocess and advisory lock, return (rc, tar_path)."""
    backup_dir = _make_backup_dir(tmp_path)
    monkeypatch.setenv("PG_DSN", pg_dsn)
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("FERNET_KEY", fernet_key)
    if neo4j_password:
        monkeypatch.setenv("NEO4J_PASSWORD", neo4j_password)

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

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.return_value = (True,)  # advisory lock acquired
    mock_conn.cursor.return_value = mock_cursor

    def _fake_run(cmd, **kwargs):
        if cmd and "pg_dump" in cmd[0]:
            return _fake_pg_dump(cmd, **kwargs)
        return MagicMock(returncode=0, stderr=b"")

    # _export_neo4j_online returns (True, msg) on success, (False, reason) on skip
    if neo4j_success:
        def _fake_neo4j_export(out_path):
            out_path.write_text("// neo4j.cypher stub\nCREATE (n:Test {x: 1});", encoding="utf-8")
            return True, "Exported 1 nodes, 0 relationships"
    else:
        def _fake_neo4j_export(out_path):
            return False, "NEO4J_PASSWORD not set — skipping Neo4j export"

    with patch("psycopg2.connect", return_value=mock_conn):
        with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
            with patch("subprocess.run", side_effect=_fake_run):
                with patch("src.cli._export_neo4j_online", side_effect=_fake_neo4j_export):
                    rc = _cmd_backup(args)

    return rc, output_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBackupWritesTarGzWithComponents:
    def test_tar_gz_contains_postgres_dump_and_manifest(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch)
        assert rc == 0, f"Expected rc=0, got {rc}"
        assert out.exists(), "Output tar.gz not created"

        with tarfile.open(str(out), "r:gz") as tar:
            names = tar.getnames()
        assert "postgres.dump" in names
        assert "postgres.sql" not in names, "New bundles must use postgres.dump, not postgres.sql"
        assert "manifest.json" in names

    def test_tar_gz_contains_neo4j_cypher_when_available(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch, neo4j_success=True)
        assert rc == 0
        with tarfile.open(str(out), "r:gz") as tar:
            names = tar.getnames()
        assert "neo4j.cypher" in names
        # Old dump format must not appear
        assert "neo4j.dump" not in names

    def test_tar_gz_no_neo4j_when_unavailable(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch, neo4j_success=False)
        assert rc == 0
        with tarfile.open(str(out), "r:gz") as tar:
            names = tar.getnames()
        assert "neo4j.cypher" not in names
        assert "neo4j.dump" not in names
        assert "postgres.dump" in names
        assert "postgres.sql" not in names, "New bundles must use postgres.dump, not postgres.sql"


class TestPgDumpUsesCustomFormat:
    """Assert that pg_dump is invoked with -F custom -Z 6 (not -F plain)."""

    def test_pg_dump_invocation_uses_custom_format_and_compression(
        self, tmp_path, monkeypatch
    ):
        """pg_dump must be called with -F custom -Z 6, NOT -F plain."""
        backup_dir = _make_backup_dir(tmp_path)
        monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
        monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
        monkeypatch.setenv("FERNET_KEY", "fk")

        out = backup_dir / "out.tar.gz"
        args = _build_parser().parse_args(["backup", "--output", str(out)])

        captured_cmds: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            if cmd and "pg_dump" in cmd[0]:
                stdout = kwargs.get("stdout")
                if stdout and hasattr(stdout, "write"):
                    stdout.write(b"-- pg_dump stub\n")
            return MagicMock(returncode=0, stderr=b"")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (True,)
        mock_conn.cursor.return_value = mock_cursor

        def _fake_neo4j_export(out_path):
            return False, "NEO4J_PASSWORD not set — skipping Neo4j export"

        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
                with patch("subprocess.run", side_effect=_fake_run):
                    with patch("src.cli._export_neo4j_online", side_effect=_fake_neo4j_export):
                        rc = _cmd_backup(args)

        assert rc == 0
        pg_dump_cmds = [c for c in captured_cmds if "pg_dump" in c[0]]
        assert pg_dump_cmds, "pg_dump must have been called"
        pg_cmd = pg_dump_cmds[0]
        assert "-F" in pg_cmd and "custom" in pg_cmd, (
            f"pg_dump must use -F custom, got: {pg_cmd}"
        )
        assert "-Z" in pg_cmd and "6" in pg_cmd, (
            f"pg_dump must use -Z 6 compression, got: {pg_cmd}"
        )
        assert "plain" not in pg_cmd, (
            f"pg_dump must NOT use -F plain (new format is custom), got: {pg_cmd}"
        )

    def test_bundle_tar_contains_postgres_dump_not_sql(self, tmp_path, monkeypatch):
        """Bundle tar must contain postgres.dump, NOT postgres.sql."""
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch)
        assert rc == 0
        with tarfile.open(str(out), "r:gz") as tar:
            names = tar.getnames()
        assert "postgres.dump" in names, f"Expected postgres.dump in bundle, got: {names}"
        assert "postgres.sql" not in names, (
            f"postgres.sql must NOT appear in new-format bundles, got: {names}"
        )


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
        assert any(c["file"] == "postgres.dump" for c in manifest["components"])

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

    def test_manifest_lists_neo4j_cypher_when_exported(self, tmp_path, monkeypatch):
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch, neo4j_success=True)
        assert rc == 0

        with tarfile.open(str(out), "r:gz") as tar:
            f = tar.extractfile(tar.getmember("manifest.json"))
            manifest = json.loads(f.read())

        files = [c["file"] for c in manifest["components"]]
        assert "neo4j.cypher" in files
        assert "neo4j.dump" not in files


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


class TestNeo4jOnlineExportSkipIsNonFatal:
    def test_missing_neo4j_password_is_non_fatal(self, tmp_path, monkeypatch, caplog):
        """When NEO4J_PASSWORD is absent, backup still succeeds with a warning."""
        rc, out = _run_backup(tmp_path, monkeypatch=monkeypatch, neo4j_success=False)
        assert rc == 0, "Backup should succeed even when Neo4j export is skipped"
        assert out.exists()

    def test_neo4j_connection_failure_is_non_fatal(self, tmp_path, monkeypatch):
        """When Neo4j is unreachable, backup still completes with postgres.dump."""
        backup_dir = _make_backup_dir(tmp_path)
        monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
        monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
        monkeypatch.setenv("FERNET_KEY", "fk")
        monkeypatch.setenv("NEO4J_PASSWORD", "pw")

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
            return MagicMock(returncode=0, stderr=b"")

        def _failing_export(out_path):
            return False, "Neo4j connection failed: Connection refused"

        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
                with patch("subprocess.run", side_effect=_fake_run):
                    with patch("src.cli._export_neo4j_online", side_effect=_failing_export):
                        rc = _cmd_backup(args)

        assert rc == 0
        assert out.exists()
        with tarfile.open(str(out), "r:gz") as tar:
            assert "postgres.dump" in tar.getnames()
            assert "neo4j.cypher" not in tar.getnames()


class TestPropsToCypher:
    """Unit tests for _props_to_cypher serializer."""

    def test_string_value_quoted(self):
        result = _props_to_cypher({"name": "sale.order"})
        assert result == 'name: "sale.order"'

    def test_int_value_literal(self):
        result = _props_to_cypher({"count": 42})
        assert result == "count: 42"

    def test_bool_value_lowercase(self):
        result = _props_to_cypher({"active": True, "archived": False})
        assert "active: true" in result
        assert "archived: false" in result

    def test_none_value_skipped(self):
        result = _props_to_cypher({"x": None, "y": 1})
        assert "x" not in result
        assert "y: 1" in result

    def test_list_value_bracket(self):
        result = _props_to_cypher({"tags": ["a", "b"]})
        assert result == 'tags: ["a", "b"]'

    def test_special_key_backtick_escaped(self):
        result = _props_to_cypher({"my-key": "val"})
        assert "`my-key`" in result

    def test_empty_props(self):
        assert _props_to_cypher({}) == ""

    def test_multiple_props(self):
        result = _props_to_cypher({"a": "x", "b": 2})
        # Order is dict-insertion order (Python 3.7+)
        assert 'a: "x"' in result
        assert "b: 2" in result


class TestGetLatestMigrationVersion:
    def test_returns_string(self):
        v = _get_latest_migration_version()
        assert isinstance(v, str)
        assert v != ""


class TestRetentionPruning:
    """Tests for _prune_old_bundles and --keep-bundles / OSM_BACKUP_KEEP integration."""

    def _make_bundles(self, directory: Path, count: int) -> list[Path]:
        """Create *count* fake .tar.gz files with monotonically-increasing mtimes."""
        import os

        bundles = []
        base_mtime = 1_700_000_000  # fixed epoch, avoids wall-clock sensitivity
        for i in range(count):
            p = directory / f"osm-backup-{i:04d}.tar.gz"
            p.write_bytes(b"stub")
            os.utime(p, (base_mtime + i, base_mtime + i))
            bundles.append(p)
        return bundles  # index 0 = oldest, index -1 = newest

    def test_retention_prunes_to_keep_n(self, tmp_path):
        """16 bundles, keep_n=5 → exactly 5 remain (the 5 newest)."""
        bundles = self._make_bundles(tmp_path, 16)
        newest = bundles[-1]  # highest mtime = newest

        deleted, reclaimed = _prune_old_bundles(tmp_path, keep_n=5, current_bundle=newest)

        remaining = sorted(tmp_path.glob("*.tar.gz"))
        assert len(remaining) == 5, f"Expected 5 remaining, got {len(remaining)}: {remaining}"
        # The 5 survivors must be the 5 newest (bundles[11..15])
        expected_survivors = {b.resolve() for b in bundles[11:]}
        assert {b.resolve() for b in remaining} == expected_survivors

        # Return value must list the 11 pruned paths
        assert len(deleted) == 11
        assert set(deleted) == {b.resolve() for b in bundles[:11]}
        assert reclaimed == 11 * len(b"stub")

    def test_retention_default_is_14(self, monkeypatch):
        """Without --keep-bundles AND without OSM_BACKUP_KEEP, effective keep_n is 14."""
        monkeypatch.delenv("OSM_BACKUP_KEEP", raising=False)
        args = _build_parser().parse_args(["backup", "--output", "/tmp/x.tar.gz"])
        assert args.keep_bundles is None, "argparse default must be None (not 14)"
        # Simulate the resolution logic in _cmd_backup
        env_keep = None  # OSM_BACKUP_KEEP not set
        if args.keep_bundles is not None:
            resolved = args.keep_bundles
        elif env_keep is not None:
            resolved = int(env_keep)
        else:
            resolved = _DEFAULT_KEEP_BUNDLES
        assert resolved == 14

    def test_retention_never_deletes_current(self, tmp_path):
        """current_bundle must survive even if its mtime would rank it outside keep window."""
        import os

        # Create 10 newer bundles and 1 current_bundle with the oldest mtime.
        self._make_bundles(tmp_path, 10)
        # current_bundle has the oldest mtime — would normally be pruned at keep_n=5
        current = tmp_path / "osm-backup-current.tar.gz"
        current.write_bytes(b"current")
        os.utime(current, (1_699_000_000, 1_699_000_000))  # older than all others

        deleted, _reclaimed = _prune_old_bundles(tmp_path, keep_n=5, current_bundle=current)

        assert current.exists(), "current_bundle must never be deleted"
        assert current.resolve() not in deleted

    def test_retention_keep_zero_disables_pruning(self, tmp_path):
        """--keep-bundles 0 (or OSM_BACKUP_KEEP=0) honours the documented contract:
        pruning is disabled entirely — no bundles deleted, returns (empty, 0)."""
        import os as _os

        # Create 5 bundles plus a current bundle — all must survive.
        for i in range(5):
            f = tmp_path / f"bundle-{i:02d}.tar.gz"
            f.write_bytes(b"x" * (1024 * (i + 1)))
            _os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
        current = tmp_path / "current.tar.gz"
        current.write_bytes(b"current")

        deleted, reclaimed = _prune_old_bundles(tmp_path, keep_n=0, current_bundle=current)

        assert deleted == []
        assert reclaimed == 0
        # All 5 pre-existing bundles still on disk.
        for i in range(5):
            assert (tmp_path / f"bundle-{i:02d}.tar.gz").exists()
        assert current.exists()
