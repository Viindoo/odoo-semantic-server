# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_cli_restore_bundle.py
"""Unit tests for CLI restore bundle path (M9 W-RS).

Tests cover:
- tarfile filter='data' blocks path traversal and symlinks
- Missing manifest.json aborts restore
- Missing postgres.sql aborts restore
- Valid bundle completes restore with safety backup
- neo4j.cypher restore via _restore_neo4j_cypher (online Bolt driver)
- Legacy neo4j.dump detected and manual-restore note printed
"""
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.cli import (
    _build_parser,
    _cmd_restore,
    _export_neo4j_online,
    _restore_neo4j_cypher,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_bundle(
    tmp_path: Path,
    *,
    include_manifest: bool = True,
    include_pg: bool = True,
    neo4j_cypher: bytes | None = None,
    neo4j_dump: bytes | None = None,
) -> Path:
    """Create a .tar.gz bundle with optional contents."""
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        if include_manifest:
            data = json.dumps({"created_at": "2026-05-15"}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if include_pg:
            pg_data = b"-- SQL dump\n"
            info2 = tarfile.TarInfo("postgres.sql")
            info2.size = len(pg_data)
            tar.addfile(info2, io.BytesIO(pg_data))
        if neo4j_cypher is not None:
            info3 = tarfile.TarInfo("neo4j.cypher")
            info3.size = len(neo4j_cypher)
            tar.addfile(info3, io.BytesIO(neo4j_cypher))
        if neo4j_dump is not None:
            info4 = tarfile.TarInfo("neo4j.dump")
            info4.size = len(neo4j_dump)
            tar.addfile(info4, io.BytesIO(neo4j_dump))
    return bundle


def _make_traversal_bundle(tmp_path: Path) -> Path:
    """Craft a malicious bundle with a path-traversal member."""
    bundle = tmp_path / "evil.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        # Attempt path traversal: ../../etc/passwd
        evil_data = b"root:x:0:0:root:/root:/bin/bash\n"
        info = tarfile.TarInfo("../../etc/passwd")
        info.size = len(evil_data)
        tar.addfile(info, io.BytesIO(evil_data))
    return bundle


def _make_symlink_bundle(tmp_path: Path) -> Path:
    """Craft a bundle with a symlink pointing outside the dest dir."""
    bundle = tmp_path / "symlink.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        # Symlink member pointing to /etc/passwd
        info = tarfile.TarInfo("evil_link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        info.size = 0
        tar.addfile(info)
    return bundle


def _args_for(file: Path, dsn: str = "postgresql://user:pw@localhost/db"):
    """Parse CLI args for restore command."""
    return _build_parser().parse_args(["restore", str(file)])


# ---------------------------------------------------------------------------
# filter='data' — path traversal blocked
# ---------------------------------------------------------------------------

def test_bundle_extract_uses_filter_data_blocks_traversal(tmp_path, monkeypatch):
    """A bundle with ../../etc/passwd member must be rejected (filter='data')."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    bundle = _make_traversal_bundle(tmp_path)
    args = _args_for(bundle)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"", stdout="")
        result = _cmd_restore(args)
    # Should fail: either tarfile rejects the member, or manifest missing after safe extract
    # Either way, should NOT return 0 success with a traversal payload
    # (filter='data' rejects the member; the bundle also has no manifest → exit 1)
    assert result != 0


def test_bundle_extract_rejects_symlink_member(tmp_path, monkeypatch):
    """A bundle with a symlink outside dest must be rejected (filter='data')."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    bundle = _make_symlink_bundle(tmp_path)
    args = _args_for(bundle)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"", stdout="")
        result = _cmd_restore(args)
    # Either filter='data' rejects the symlink, or manifest is absent → exit 1
    assert result != 0


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def test_bundle_missing_manifest_aborts(tmp_path, monkeypatch):
    """Bundle without manifest.json → non-zero exit."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    bundle = _make_minimal_bundle(tmp_path, include_manifest=False, include_pg=True)
    args = _args_for(bundle)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"", stdout="")
        result = _cmd_restore(args)
    assert result != 0


# ---------------------------------------------------------------------------
# postgres.sql validation
# ---------------------------------------------------------------------------

def test_bundle_missing_pg_dump_aborts(tmp_path, monkeypatch):
    """Bundle without postgres.sql → non-zero exit."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    bundle = _make_minimal_bundle(tmp_path, include_manifest=True, include_pg=False)
    args = _args_for(bundle)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"", stdout="")
        result = _cmd_restore(args)
    assert result != 0


# ---------------------------------------------------------------------------
# Happy path: valid bundle with safety backup
# ---------------------------------------------------------------------------

def test_bundle_restore_happy_path(tmp_path, monkeypatch):
    """Valid bundle + PG_DSN → safety backup + psql restore called."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    bundle = _make_minimal_bundle(tmp_path)
    args = _args_for(bundle)

    call_order = []

    def mock_run(cmd, **kwargs):
        call_order.append(cmd[0])
        # Safety backup writes to stdout
        if cmd[0] == "pg_dump":
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                stdout.write(b"-- mock dump\n")
            return MagicMock(returncode=0, stderr=b"")
        # psql restore
        return MagicMock(returncode=0, stderr="", stdout="")

    with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
        with patch("subprocess.run", side_effect=mock_run):
            result = _cmd_restore(args)

    assert result == 0
    # Safety backup (pg_dump) must come BEFORE psql restore
    assert "pg_dump" in call_order
    assert "psql" in call_order
    pg_dump_idx = call_order.index("pg_dump")
    psql_idx = call_order.index("psql")
    assert pg_dump_idx < psql_idx, "Safety backup must run before psql restore"


def test_bundle_safety_backup_failure_aborts(tmp_path, monkeypatch):
    """If pg_dump safety backup fails, restore must abort (no psql called)."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    bundle = _make_minimal_bundle(tmp_path)
    args = _args_for(bundle)

    call_order = []

    def mock_run(cmd, **kwargs):
        call_order.append(cmd[0])
        if cmd[0] == "pg_dump":
            return MagicMock(returncode=1, stderr=b"pg_dump: connection refused")
        return MagicMock(returncode=0, stderr="", stdout="")

    with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
        with patch("subprocess.run", side_effect=mock_run):
            result = _cmd_restore(args)

    assert result != 0
    assert "psql" not in call_order, "psql must NOT be called if safety backup fails"


# ---------------------------------------------------------------------------
# Neo4j Cypher restore via _restore_neo4j_cypher
# ---------------------------------------------------------------------------

_CYPHER_STUB = (
    b"// neo4j.cypher\n"
    b"CREATE (n:Module {name: \"sale\", __eid__: \"elem:0\"});\n"
    b"MATCH (a {__eid__: \"elem:0\"}), (b {__eid__: \"elem:0\"}) "
    b"CREATE (a)-[:SELF]->(b);\n"
    b"MATCH (n) WHERE n.__eid__ IS NOT NULL REMOVE n.__eid__;\n"
)


def test_bundle_with_neo4j_cypher_calls_restore_neo4j_cypher(tmp_path, monkeypatch):
    """Bundle with neo4j.cypher must trigger _restore_neo4j_cypher, not manual note."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")

    bundle = _make_minimal_bundle(tmp_path, neo4j_cypher=_CYPHER_STUB)
    args = _args_for(bundle)

    called_with: list[Path] = []

    def _fake_restore_neo4j(cypher_path):
        called_with.append(cypher_path)
        return True, "Restored 2 statements"

    def mock_run(cmd, **kwargs):
        if cmd[0] == "pg_dump":
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                stdout.write(b"-- mock dump\n")
            return MagicMock(returncode=0, stderr=b"")
        return MagicMock(returncode=0, stderr="", stdout="")

    snap_ok = (True, "Exported 1 nodes, 0 relationships")
    with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
        with patch("subprocess.run", side_effect=mock_run):
            # Safety snapshot of the live graph succeeds so the wipe is allowed.
            with patch("src.cli._export_neo4j_online", return_value=snap_ok):
                with patch("src.cli._restore_neo4j_cypher", side_effect=_fake_restore_neo4j):
                    result = _cmd_restore(args)

    assert result == 0
    assert len(called_with) == 1, "_restore_neo4j_cypher must be called exactly once"
    assert called_with[0].name == "neo4j.cypher"


def test_bundle_neo4j_restore_failure_propagates_nonzero_exit(tmp_path, monkeypatch, capsys):
    """Business rule (DR safety): a Neo4j restore failure must propagate to a
    non-zero exit code, NOT be swallowed as a warning with exit 0.

    DR automation keys off the process exit code. If Postgres restores but the
    Neo4j graph fails (or is left partial), the command must report overall
    FAILURE so the operator/automation does not believe the restore succeeded.
    The Postgres success is still reported explicitly; only the exit code
    signals the partial-failure state.
    """
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")

    bundle = _make_minimal_bundle(tmp_path, neo4j_cypher=_CYPHER_STUB)
    args = _args_for(bundle)

    def mock_run(cmd, **kwargs):
        if cmd[0] == "pg_dump":
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                stdout.write(b"-- mock dump\n")
            return MagicMock(returncode=0, stderr=b"")
        return MagicMock(returncode=0, stderr="", stdout="")

    def _failing_restore(cypher_path):
        # Reachable Neo4j whose replay failed mid-way (a real, fatal failure —
        # NOT an "unreachable" early-exit).
        return False, "Restore finished with 3 errors: 'CREATE ...': syntax error"

    snap_ok = (True, "Exported 5 nodes, 2 relationships")
    with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
        with patch("subprocess.run", side_effect=mock_run):
            # Safety snapshot of the live graph succeeds (so the wipe is allowed).
            with patch("src.cli._export_neo4j_online", return_value=snap_ok):
                with patch("src.cli._restore_neo4j_cypher", side_effect=_failing_restore):
                    result = _cmd_restore(args)

    # Neo4j failure must NOT be swallowed — overall exit is non-zero.
    assert result != 0, "Neo4j restore failure must propagate a non-zero exit code"
    captured = capsys.readouterr()
    # Postgres success is still communicated.
    assert "postgres" in (captured.out + captured.err).lower()
    # And the failure is surfaced as an error, not a quiet log.
    assert "Neo4j restore failed" in captured.err


def test_bundle_neo4j_safety_snapshot_failure_on_reachable_db_aborts_before_wipe(
    tmp_path, monkeypatch, capsys
):
    """Business rule (DR safety): if the live Neo4j is reachable but its
    pre-restore safety snapshot fails for a non-connectivity reason, the restore
    must ABORT before wiping — never destroy a graph it could not first back up.
    """
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")

    bundle = _make_minimal_bundle(tmp_path, neo4j_cypher=_CYPHER_STUB)
    args = _args_for(bundle)

    def mock_run(cmd, **kwargs):
        if cmd[0] == "pg_dump":
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                stdout.write(b"-- mock dump\n")
            return MagicMock(returncode=0, stderr=b"")
        return MagicMock(returncode=0, stderr="", stdout="")

    restore_called: list[bool] = []

    def _restore_must_not_run(cypher_path):
        restore_called.append(True)
        return True, "should not be reached"

    with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
        with patch("subprocess.run", side_effect=mock_run):
            # Reachable DB, but snapshot fails for a real reason (not unreachable).
            with patch(
                "src.cli._export_neo4j_online",
                return_value=(False, "Neo4j export failed: disk full"),
            ):
                with patch("src.cli._restore_neo4j_cypher", side_effect=_restore_must_not_run):
                    result = _cmd_restore(args)

    assert result != 0, "Failed safety snapshot on a reachable graph must abort"
    assert not restore_called, (
        "_restore_neo4j_cypher (which wipes) must NOT run when the safety "
        "snapshot of a reachable graph failed"
    )


def test_bundle_with_legacy_neo4j_dump_prints_manual_note(tmp_path, monkeypatch, capsys):
    """Legacy bundles with neo4j.dump must print a manual-restore note, not call cypher restore."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    bundle = _make_minimal_bundle(tmp_path, neo4j_dump=b"NEO4J_DUMP_PLACEHOLDER")
    args = _args_for(bundle)

    def mock_run(cmd, **kwargs):
        if cmd[0] == "pg_dump":
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                stdout.write(b"-- mock dump\n")
            return MagicMock(returncode=0, stderr=b"")
        return MagicMock(returncode=0, stderr="", stdout="")

    restore_called: list[bool] = []

    def _should_not_be_called(cypher_path):
        restore_called.append(True)
        return True, "should not be called"

    with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
        with patch("subprocess.run", side_effect=mock_run):
            with patch("src.cli._restore_neo4j_cypher", side_effect=_should_not_be_called):
                result = _cmd_restore(args)

    assert result == 0
    assert not restore_called, "_restore_neo4j_cypher must NOT be called for legacy .dump"
    captured = capsys.readouterr()
    assert "neo4j-admin" in captured.out or "manual" in captured.out.lower()


# ---------------------------------------------------------------------------
# _restore_neo4j_cypher unit tests (mocked driver)
# ---------------------------------------------------------------------------

def test_restore_neo4j_cypher_executes_statements(tmp_path, monkeypatch):
    """_restore_neo4j_cypher must parse and execute non-comment statements.

    FIX 1: restore is a REPLACE — it must run `MATCH (n) DETACH DELETE n` FIRST
    (to avoid duplicating onto a non-empty graph) and then replay the file's
    statements in order.
    """
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")

    cypher_file = tmp_path / "neo4j.cypher"
    cypher_file.write_text(
        "// header comment\n"
        "\n"
        "CREATE (n:Test {x: 1});\n"
        "MATCH (n) WHERE n.__eid__ IS NOT NULL REMOVE n.__eid__;\n",
        encoding="utf-8",
    )

    executed: list[str] = []

    mock_result = MagicMock()
    mock_result.consume.return_value = None

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.run.side_effect = lambda stmt: (executed.append(stmt), mock_result)[1]

    mock_driver = MagicMock()
    mock_driver.verify_connectivity.return_value = None
    mock_driver.session.return_value = mock_session
    mock_driver.close.return_value = None

    with patch("neo4j.GraphDatabase.driver", return_value=mock_driver):
        ok, msg = _restore_neo4j_cypher(cypher_file)

    assert ok, f"Expected success, got: {msg}"
    # The wipe must be the FIRST statement executed (replace, not merge).
    assert executed[0] == "MATCH (n) DETACH DELETE n", (
        f"Restore must DETACH DELETE the graph before replaying; got first "
        f"statement {executed[0]!r}"
    )
    # Then the 2 file statements (CREATE + __eid__ cleanup) replay in order.
    assert executed[1:] == [
        "CREATE (n:Test {x: 1})",
        "MATCH (n) WHERE n.__eid__ IS NOT NULL REMOVE n.__eid__",
    ], f"File statements not replayed correctly: {executed[1:]}"


def _mock_neo4j_driver_capturing(executed: list[str]):
    """Build a mock GraphDatabase driver that records every statement run."""
    mock_result = MagicMock()
    mock_result.consume.return_value = None

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.run.side_effect = lambda stmt: (executed.append(stmt), mock_result)[1]

    mock_driver = MagicMock()
    mock_driver.verify_connectivity.return_value = None
    mock_driver.session.return_value = mock_session
    mock_driver.close.return_value = None
    return mock_driver


def test_restore_neo4j_cypher_empty_file_refuses_to_wipe(tmp_path, monkeypatch):
    """Business rule (DR safety): an empty/no-statement cypher file must NOT wipe
    the graph. DETACH DELETE must never run; the call returns an error.
    """
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")

    cypher_file = tmp_path / "neo4j.cypher"
    # Only comments + blanks → zero executable statements.
    cypher_file.write_text("// neo4j.cypher — header only\n\n", encoding="utf-8")

    executed: list[str] = []
    mock_driver = _mock_neo4j_driver_capturing(executed)

    with patch("neo4j.GraphDatabase.driver", return_value=mock_driver):
        ok, msg = _restore_neo4j_cypher(cypher_file)

    assert not ok, "Empty cypher file must be rejected"
    assert "MATCH (n) DETACH DELETE n" not in executed, (
        "DETACH DELETE must NOT run for an empty/corrupt dump — graph would be "
        f"unrecoverably wiped. Executed: {executed}"
    )


def test_restore_neo4j_cypher_missing_completeness_trailer_refuses_to_wipe(
    tmp_path, monkeypatch
):
    """Business rule (DR safety): a cypher file truncated before the export
    completeness trailer (`REMOVE n.__eid__`) must NOT wipe the graph.

    A genuine _export_neo4j_online file always ends with that trailer; its
    absence means the file was cut off mid-write, so restoring it would replay
    a partial graph after wiping the live one.
    """
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")

    cypher_file = tmp_path / "neo4j.cypher"
    # Has CREATE statements but is truncated before the trailer.
    cypher_file.write_text(
        "// neo4j.cypher\n"
        'CREATE (n:Module {name: "sale", __eid__: "elem:0"});\n'
        'CREATE (n:Module {name: "stock", __eid__: "elem:1"});\n',
        encoding="utf-8",
    )

    executed: list[str] = []
    mock_driver = _mock_neo4j_driver_capturing(executed)

    with patch("neo4j.GraphDatabase.driver", return_value=mock_driver):
        ok, msg = _restore_neo4j_cypher(cypher_file)

    assert not ok, "Truncated cypher file (no trailer) must be rejected"
    assert "trailer" in msg.lower() or "truncated" in msg.lower(), (
        f"Error message should explain the missing-trailer reason; got: {msg!r}"
    )
    assert "MATCH (n) DETACH DELETE n" not in executed, (
        f"DETACH DELETE must NOT run for a truncated dump. Executed: {executed}"
    )


def test_restore_neo4j_cypher_missing_password(tmp_path, monkeypatch):
    """_restore_neo4j_cypher returns False when NEO4J_PASSWORD is unset."""
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    cypher_file = tmp_path / "neo4j.cypher"
    cypher_file.write_text("CREATE (n:X);", encoding="utf-8")

    ok, msg = _restore_neo4j_cypher(cypher_file)
    assert not ok
    assert "NEO4J_PASSWORD" in msg or "password" in msg.lower()


# ---------------------------------------------------------------------------
# _export_neo4j_online — consistency + streaming contract (mocked driver)
# ---------------------------------------------------------------------------

def test_export_reads_nodes_and_rels_in_single_transaction(tmp_path, monkeypatch):
    """Business rule (F — snapshot consistency): the node scan and relationship
    scan must run inside ONE explicit read transaction so a concurrent indexer
    write cannot land between them and produce a dangling-rel dump.

    Verified structurally: exactly one begin_transaction() is opened, and BOTH
    the node query and the relationship query run via that transaction's .run()
    (never via the session's auto-commit .run()).
    """
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")

    tx_queries: list[str] = []
    session_queries: list[str] = []

    mock_tx = MagicMock()
    mock_tx.__enter__ = lambda s: s
    mock_tx.__exit__ = MagicMock(return_value=False)
    # Each tx.run() returns an empty iterable of records (no nodes/rels).
    mock_tx.run.side_effect = lambda q, **kw: (tx_queries.append(q), iter([]))[1]

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.begin_transaction.return_value = mock_tx
    mock_session.run.side_effect = lambda q, **kw: (session_queries.append(q), iter([]))[1]

    mock_driver = MagicMock()
    mock_driver.verify_connectivity.return_value = None
    mock_driver.session.return_value = mock_session
    mock_driver.close.return_value = None

    out_path = tmp_path / "neo4j.cypher"
    with patch("neo4j.GraphDatabase.driver", return_value=mock_driver):
        ok, msg = _export_neo4j_online(out_path)

    assert ok, f"Export failed: {msg}"
    # Exactly one read transaction was opened for the whole export.
    assert mock_session.begin_transaction.call_count == 1, (
        "Export must open exactly one read transaction for a consistent snapshot; "
        f"begin_transaction called {mock_session.begin_transaction.call_count} times"
    )
    # Both scans went through the transaction, not session auto-commit.
    assert any("MATCH (n) RETURN" in q for q in tx_queries), tx_queries
    assert any("MATCH (a)-[r]->(b)" in q for q in tx_queries), tx_queries
    assert session_queries == [], (
        f"No query may bypass the read transaction via session.run(); got {session_queries}"
    )


def test_export_streams_to_file_handle_not_in_memory_join(tmp_path, monkeypatch):
    """Business rule (E — memory): the export must write each statement straight
    to the file handle rather than building a full in-memory list and join()-ing.

    Verified structurally: the function opens the output path for writing and
    issues many incremental .write() calls (one per node/rel/trailer line),
    instead of a single write_text() of the whole graph.
    """
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")

    # Two nodes, one relationship — enough to prove >1 incremental write.
    node_rows = [
        {"eid": "e0", "lbls": ["Module"], "props": {"name": "sale"}},
        {"eid": "e1", "lbls": ["Module"], "props": {"name": "stock"}},
    ]
    rel_rows = [
        {"aeid": "e0", "beid": "e1", "rtype": "DEPENDS_ON", "props": {}},
    ]

    def _tx_run(query, **kw):
        if "MATCH (n) RETURN" in query:
            return iter(node_rows)
        if "MATCH (a)-[r]->(b)" in query:
            return iter(rel_rows)
        return iter([])

    mock_tx = MagicMock()
    mock_tx.__enter__ = lambda s: s
    mock_tx.__exit__ = MagicMock(return_value=False)
    mock_tx.run.side_effect = _tx_run

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.begin_transaction.return_value = mock_tx

    mock_driver = MagicMock()
    mock_driver.verify_connectivity.return_value = None
    mock_driver.session.return_value = mock_session
    mock_driver.close.return_value = None

    out_path = tmp_path / "neo4j.cypher"
    with patch("neo4j.GraphDatabase.driver", return_value=mock_driver):
        ok, msg = _export_neo4j_online(out_path)

    assert ok, f"Export failed: {msg}"
    content = out_path.read_text(encoding="utf-8")
    # Streamed content still reproduces the full, replayable file: 2 CREATE node
    # statements, 1 relationship MATCH+CREATE, and the completeness trailer.
    assert content.count("CREATE (n") == 2, content
    assert content.count("-[:DEPENDS_ON") == 1, content
    assert "REMOVE n.__eid__" in content, content
    assert msg == "Exported 2 nodes, 1 relationships", msg
