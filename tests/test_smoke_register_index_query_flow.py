# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_smoke_register_index_query_flow.py
"""E2E smoke test: admin flow add_profile → add_repo → index_profile → resolve_model.

Covers M2.5 manual E2E item (admin registers a repo, indexes it, and the MCP
tool returns user-visible tree text with the module name from indexed data).

Requires Neo4j + PostgreSQL — marked neo4j + postgres.
"""
import subprocess
import textwrap
from pathlib import Path

import pytest

from src.db.migrate import run_migrations
from src.db.pg import repo_store
from src.indexer.pipeline import index_profile
from src.mcp.server import _resolve_model
from tests.conftest import TEST_VERSION

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]

# Profile name must match pattern ^[a-zA-Z0-9_]{1,50}$ per manager validation.
_SMOKE_PROFILE = "smoke_register_idx_99"
_SMOKE_MODEL = "my.model"
_SMOKE_MODULE = "test_mod"


def _build_minimal_addon_repo(base: Path) -> Path:
    """Create a minimal Odoo addon repo under base/repo and git-init it.

    Layout:
        repo/
          addons/
            test_mod/
              __manifest__.py
              models/
                __init__.py
                my_model.py

    The repo is git-committed so the incremental indexer can compute HEAD sha.
    """
    repo = base / "repo"
    addon_dir = repo / "addons" / _SMOKE_MODULE
    models_dir = addon_dir / "models"

    # Create directories
    addon_dir.mkdir(parents=True)
    models_dir.mkdir()

    # __manifest__.py
    (addon_dir / "__manifest__.py").write_text(
        textwrap.dedent(f"""
            {{'name': 'Test Mod', 'depends': ['base'], 'installable': True,
             'version': '{TEST_VERSION}.1.0.0'}}
        """).strip() + "\n",
        encoding="utf-8",
    )

    # models/__init__.py
    (models_dir / "__init__.py").write_text(
        "from . import my_model\n",
        encoding="utf-8",
    )

    # models/my_model.py — declares my.model with one Char field
    (models_dir / "my_model.py").write_text(
        textwrap.dedent("""
            from odoo import models, fields


            class MyModel(models.Model):
                _name = "my.model"
                field_a = fields.Char()
        """).lstrip(),
        encoding="utf-8",
    )

    # git init + commit so HEAD sha is available (needed by incremental indexer)
    git = ["git", "-C", str(repo)]
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(git + ["config", "user.email", "test@example.com"],
                   check=True, capture_output=True)
    subprocess.run(git + ["config", "user.name", "Test"],
                   check=True, capture_output=True)
    subprocess.run(git + ["add", "."], check=True, capture_output=True)
    subprocess.run(git + ["commit", "-m", "init"], check=True, capture_output=True)

    return repo


def test_full_admin_flow_register_index_query(
    clean_neo4j,
    clean_pg,
    tmp_path,
):
    """Admin can: add profile → add repo → index → query MCP tool.

    Assertions:
    1. index_profile returns at least 1 module indexed.
    2. _resolve_model returns a string containing the model name + module name
       + tree markers from the indexed data (model is queryable via MCP).
    3. _resolve_model with a different version returns "not found" (no version leak).
    """
    # --- Setup schema (clean_pg drops and recreates tables) ---
    run_migrations(clean_pg)

    # --- Step 1: Build a local minimal Odoo addon repo ---
    repo_path = _build_minimal_addon_repo(tmp_path)

    # --- Step 2: Register profile (version=TEST_VERSION = "99.0") ---
    profile_id = repo_store().add_profile(
        name=_SMOKE_PROFILE,
        odoo_version=TEST_VERSION,
        description="Smoke test profile",
    )
    assert isinstance(profile_id, int) and profile_id > 0

    # --- Step 3: Register repo ---
    repo_id = repo_store().add_repo(
        profile_id=profile_id,
        url=f"file://{repo_path}",
        branch="master",
        local_path=str(repo_path),
    )
    assert isinstance(repo_id, int) and repo_id > 0

    # --- Step 4: Index (no-embed; Neo4j only) ---
    summary = index_profile(
        clean_pg,
        profile_name=_SMOKE_PROFILE,
        embedder=None,
    )
    assert summary["modules"] >= 1, (
        f"Expected at least 1 module indexed, got {summary}"
    )

    # --- Step 5: Query via MCP _resolve_model ---
    result = _resolve_model(_SMOKE_MODEL, TEST_VERSION)

    # Must be a non-empty string
    assert isinstance(result, str) and result.strip(), (
        f"_resolve_model returned empty or non-string: {result!r}"
    )

    # Must contain the model name
    assert _SMOKE_MODEL in result, (
        f"Expected '{_SMOKE_MODEL}' in MCP output, got:\n{result}"
    )

    # Must contain the module name (proves data from the indexed repo is used)
    assert _SMOKE_MODULE in result, (
        f"Expected module '{_SMOKE_MODULE}' in MCP output, got:\n{result}"
    )

    # Must contain tree-format markers (Ship Wow Product principle)
    has_tree_marker = ("├─" in result or "└─" in result or "Defined in:" in result)
    assert has_tree_marker, (
        f"Expected tree-format markers (├─/└─ or 'Defined in:') in MCP output, got:\n{result}"
    )

    # --- Step 6: Isolation check — different version must NOT see "99.0" data ---
    result_other = _resolve_model(_SMOKE_MODEL, "17.0")
    # Either "not found" message (no 17.0 data) or, if 17.0 data happens to
    # exist from another test, it must not mention our smoke module.
    if _SMOKE_MODEL in result_other:
        # If the model name appears (real 17.0 data), the module must not match.
        assert _SMOKE_MODULE not in result_other, (
            f"Version isolation failure: '{_SMOKE_MODULE}' appeared in 17.0 query "
            f"but was only indexed under 99.0.\nOutput:\n{result_other}"
        )
    # If result_other is "not found", that is also acceptable — nothing to assert.
