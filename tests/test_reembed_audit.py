# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for M10 WI-3: reembed_stubs_for_profile + audit_repo_for_profile.

Integration tests require both Neo4j and PostgreSQL (pgvector).
Unit tests for the CLI dispatch (subcommand wiring) run without DB.
"""
import json
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TEST_VERSION = "99.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "checkout", "-b", TEST_VERSION)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / ".gitkeep").write_text("")
    _git(path, "add", ".gitkeep")
    _git(path, "commit", "-m", "init")
    return path


def _seed_module(repo: Path, name: str) -> None:
    """Create a minimal Odoo module under repo/<name> with 1 model + 1 field."""
    module = repo / name
    module.mkdir(parents=True, exist_ok=True)
    (module / "__manifest__.py").write_text(
        f"{{'name': {name!r}, 'version': '{TEST_VERSION}.1.0.0', "
        f"'depends': [], 'installable': True}}\n"
    )
    models_dir = module / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "__init__.py").write_text("")
    (models_dir / f"{name}.py").write_text(textwrap.dedent(f"""
        from odoo import models, fields

        class {name.capitalize()}Model(models.Model):
            _name = '{name}.stub'
            x = fields.Char()
    """).strip())
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add module {name}")


# ---------------------------------------------------------------------------
# Unit tests (no DB) — CLI subcommand wiring
# ---------------------------------------------------------------------------

def test_reembed_stubs_subcommand_registered():
    """reembed-stubs subcommand is registered in the CLI parser."""
    import src.indexer.__main__ as main_mod
    parser = main_mod._build_parser()
    # Parse a minimal valid invocation — must not raise
    args = parser.parse_args(["reembed-stubs", "--profile", "some_prof"])
    assert args.subcommand == "reembed-stubs"
    assert args.profile == "some_prof"


def test_audit_repo_subcommand_registered(tmp_path):
    """audit-repo subcommand is registered in the CLI parser."""
    import src.indexer.__main__ as main_mod
    parser = main_mod._build_parser()
    out_path = str(tmp_path / "out.json")
    args = parser.parse_args(["audit-repo", "--profile", "some_prof", "--output", out_path])
    assert args.subcommand == "audit-repo"
    assert args.profile == "some_prof"
    assert args.output == out_path


def test_reembed_stubs_returns_error_when_embedder_missing(monkeypatch, tmp_path):
    """reembed-stubs exits with code 1 when embedder URL is not configured."""
    import src.config as config_mod
    import src.indexer.__main__ as main_mod

    cfg = tmp_path / "empty.conf"
    cfg.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None

    with patch("src.indexer.__main__.open_production_pg") as mock_pg:
        mock_pg.return_value.close = MagicMock()
        rc = main_mod.main(["reembed-stubs", "--profile", "x"])
    assert rc == 1, "Should return exit code 1 when embedder is not configured"


def test_audit_repo_writes_json(monkeypatch, tmp_path):
    """audit-repo subcommand writes a valid JSON array to --output path."""
    import src.config as config_mod
    import src.indexer.__main__ as main_mod

    cfg = tmp_path / "empty.conf"
    cfg.write_text("")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None

    out_path = tmp_path / "audit.json"
    fake_rows = [
        {"module": "sale", "odoo_version": "17.0",
         "model_count": 2, "field_count": 10, "method_count": 5,
         "view_count": 3, "embedding_count": 20},
    ]

    with (
        patch("src.indexer.__main__.open_production_pg") as mock_pg,
        patch("src.indexer.__main__.audit_repo_for_profile", return_value=fake_rows),
    ):
        mock_pg.return_value.close = MagicMock()
        rc = main_mod.main(["audit-repo", "--profile", "my_prof",
                             "--output", str(out_path)])

    assert rc == 0
    assert out_path.exists(), "JSON output file must be created"
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data == fake_rows


# ---------------------------------------------------------------------------
# Integration tests — require Neo4j + PostgreSQL + pgvector
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
@pytest.mark.postgres
def test_reembed_stubs_idempotent(clean_neo4j, clean_pg, tmp_path):
    """reembed_stubs_for_profile is idempotent.

    First call: module with zero embeddings -> embedder is called.
    Second call: module already has embeddings -> no new embed calls (no-op).
    """
    from src.db.migrate import _vector_extension_available, run_migrations
    from src.db.pg import repo_store
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.pipeline import index_profile, reembed_stubs_for_profile

    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed")

    # Index a module WITHOUT embedder first (no embeddings in pgvector).
    repo = _make_git_repo(tmp_path / "repo_stub")
    _seed_module(repo, "stub_mod")
    pid = repo_store().add_profile("stub_prof", TEST_VERSION)
    repo_store().add_repo(pid, "file://local/stub", TEST_VERSION, str(repo))

    # Index without embedder -> Neo4j nodes created, 0 pgvector rows.
    index_profile(clean_pg, profile_name="stub_prof", embedder=None)

    # Verify Neo4j has the module but pgvector has zero rows.
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM embeddings WHERE module = 'stub_mod' AND odoo_version = %s",
            (TEST_VERSION,),
        )
        initial_count = cur.fetchone()[0]
    assert initial_count == 0, (
        f"Expected 0 embeddings after --no-embed index, got {initial_count}"
    )

    # First reembed run — should embed the stub module.
    embedder = FakeEmbedder(dim=1024)
    summary1 = reembed_stubs_for_profile(clean_pg, profile_name="stub_prof",
                                          embedder=embedder)
    assert summary1["modules_reembedded"] >= 1, (
        f"Expected >= 1 module reembedded, got {summary1}"
    )
    assert summary1["total_embed_calls"] >= 1, (
        f"Expected >= 1 embed call, got {summary1}"
    )

    # Verify pgvector now has rows.
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM embeddings WHERE module = 'stub_mod' AND odoo_version = %s",
            (TEST_VERSION,),
        )
        after_first = cur.fetchone()[0]
    assert after_first > 0, "Expected embeddings after reembed run"

    # Second reembed run — should be a no-op (all modules already embedded).
    call_count_before = embedder.call_count
    summary2 = reembed_stubs_for_profile(clean_pg, profile_name="stub_prof",
                                          embedder=embedder)
    call_count_after = embedder.call_count
    embed_delta = call_count_after - call_count_before

    assert summary2["modules_reembedded"] == 0, (
        f"Second run must not re-embed any module, got {summary2}"
    )
    assert embed_delta == 0, (
        f"Second run must not call embed(), got embed_delta={embed_delta}"
    )


@pytest.mark.neo4j
@pytest.mark.postgres
def test_audit_repo_returns_correct_schema(clean_neo4j, clean_pg, tmp_path):
    """audit_repo_for_profile returns correct per-module JSON schema.

    Verifies that:
    - Each row has the required keys.
    - model_count >= 1 (we seeded one model).
    - field_count >= 1 (we seeded one field).
    - embedding_count is an int (0 or more).
    """
    from src.db.migrate import run_migrations
    from src.db.pg import repo_store
    from src.indexer.pipeline import audit_repo_for_profile, index_profile

    run_migrations(clean_pg)

    repo = _make_git_repo(tmp_path / "repo_audit")
    _seed_module(repo, "audit_mod")
    pid = repo_store().add_profile("audit_prof", TEST_VERSION)
    repo_store().add_repo(pid, "file://local/audit", TEST_VERSION, str(repo))

    # Index with no embedder (embeddings = 0 is acceptable for schema check).
    index_profile(clean_pg, profile_name="audit_prof", embedder=None)

    rows = audit_repo_for_profile(clean_pg, profile_name="audit_prof")

    assert isinstance(rows, list), "audit_repo_for_profile must return a list"
    assert len(rows) >= 1, f"Expected at least 1 row, got {rows}"

    required_keys = {
        "module", "odoo_version", "model_count", "field_count",
        "method_count", "view_count", "embedding_count",
    }
    for row in rows:
        missing = required_keys - set(row.keys())
        assert not missing, f"Row missing keys {missing}: {row}"
        assert isinstance(row["embedding_count"], int), (
            f"embedding_count must be int, got {type(row['embedding_count'])}"
        )
        assert row["odoo_version"] == TEST_VERSION

    # Find our seeded module.
    audit_row = next((r for r in rows if r["module"] == "audit_mod"), None)
    assert audit_row is not None, (
        f"Expected 'audit_mod' in audit output, got modules: {[r['module'] for r in rows]}"
    )
    assert audit_row["model_count"] >= 1, (
        f"Expected model_count >= 1 for audit_mod, got {audit_row['model_count']}"
    )
    assert audit_row["field_count"] >= 1, (
        f"Expected field_count >= 1 for audit_mod, got {audit_row['field_count']}"
    )


@pytest.mark.neo4j
@pytest.mark.postgres
def test_audit_repo_empty_profile_returns_empty_list(clean_neo4j, clean_pg):
    """audit_repo_for_profile returns [] for a profile with no repos."""
    from src.db.migrate import run_migrations
    from src.db.pg import repo_store
    from src.indexer.pipeline import audit_repo_for_profile

    run_migrations(clean_pg)
    repo_store().add_profile("empty_audit_prof", TEST_VERSION)

    rows = audit_repo_for_profile(clean_pg, profile_name="empty_audit_prof")
    assert rows == [], f"Expected empty list for profile with no repos, got {rows}"


@pytest.mark.neo4j
@pytest.mark.postgres
def test_audit_repo_json_serialisable(clean_neo4j, clean_pg, tmp_path):
    """audit_repo_for_profile output is JSON-serialisable with correct types."""
    from src.db.migrate import run_migrations
    from src.db.pg import repo_store
    from src.indexer.pipeline import audit_repo_for_profile, index_profile

    run_migrations(clean_pg)

    repo = _make_git_repo(tmp_path / "repo_audit_json")
    _seed_module(repo, "json_mod")
    pid = repo_store().add_profile("audit_json_prof", TEST_VERSION)
    repo_store().add_repo(pid, "file://local/json", TEST_VERSION, str(repo))

    index_profile(clean_pg, profile_name="audit_json_prof", embedder=None)

    rows = audit_repo_for_profile(clean_pg, profile_name="audit_json_prof")

    # Must be JSON-serialisable without error.
    serialised = json.dumps(rows)
    assert isinstance(serialised, str)

    # Round-trip: deserialized data must equal original.
    decoded = json.loads(serialised)
    assert decoded == rows
