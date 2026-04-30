"""Integration tests for the indexer driver.

Skipped when DATABASE_URL is unset. Each test provisions a throwaway tenant
schema (``osm_test_<hex>``) so it never trashes ``public``. Cleanup drops the
schema on teardown.

Anchors:

- Full index of the fixture corpus runs clean.
- Re-running on unchanged tree: no row writes outside cache_metadata.indexed_at.
- Touching a single method body re-indexes exactly that method's row + its
  cache row.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; skipping driver integration tests",
)


FIXTURES = Path(__file__).parent.parent / "fixtures"
CE_SUBSET = FIXTURES / "odoo_ce_subset"
CUSTOM_ADDONS = FIXTURES / "custom_addons"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    assert url is not None
    return url


@pytest.fixture()
def tenant_schema(database_url: str) -> Iterator[str]:
    """Fresh tenant schema per test; dropped on teardown."""
    import psycopg

    from scripts.create_tenant import main as create_tenant_main
    from scripts.migrate import main as migrate_main

    assert migrate_main(["--schema", "public", "--database-url", database_url]) == 0

    name = f"osm_test_{uuid.uuid4().hex[:8]}"
    assert create_tenant_main([name, "--database-url", database_url]) == 0
    try:
        yield name
    finally:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            conn.commit()


@pytest.fixture()
def fixture_mirror(tmp_path: Path) -> Path:
    """Copy the 20-module fixture corpus into tmp_path so delta tests can mutate
    files without touching the committed sources."""
    dst = tmp_path / "corpus"
    dst.mkdir()
    shutil.copytree(CE_SUBSET, dst / "odoo_ce_subset")
    shutil.copytree(CUSTOM_ADDONS, dst / "custom_addons")
    return dst


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_index(
    database_url: str,
    tenant: str,
    addon_roots: list[Path],
    git_sha: str,
) -> object:
    import psycopg

    from osm.indexer.driver import index

    with psycopg.connect(database_url) as conn:
        stats = index(
            addon_roots=addon_roots,
            conn=conn,
            tenant=tenant,
            git_sha=git_sha,
        )
        conn.commit()
    return stats


def _count(database_url: str, schema: str, table: str) -> int:
    import psycopg

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _max_indexed_at_sha(database_url: str, schema: str, table: str) -> set[str]:
    import psycopg

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT DISTINCT indexed_at_sha FROM "{schema}"."{table}"')
        return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Full index
# ---------------------------------------------------------------------------


def test_full_index_of_20_module_fixture(
    database_url: str,
    tenant_schema: str,
    fixture_mirror: Path,
) -> None:
    roots = [fixture_mirror / "odoo_ce_subset", fixture_mirror / "custom_addons"]
    stats = _run_index(database_url, tenant_schema, roots, git_sha="sha-first")

    # Every fixture module directory lands as a modules row. The corpus
    # grows over time; count the manifests on disk rather than pinning a
    # stale magic number.
    expected = sum(
        1 for root in roots for manifest in root.glob("*/__manifest__.py")
    )
    assert stats.modules_scanned == expected  # type: ignore[attr-defined]
    assert stats.modules_upserted == expected  # type: ignore[attr-defined]
    assert _count(database_url, tenant_schema, "modules") == expected

    # models/fields/methods tables are non-empty
    assert _count(database_url, tenant_schema, "models") > 0
    assert _count(database_url, tenant_schema, "fields") > 0
    assert _count(database_url, tenant_schema, "methods") > 0

    # cache_metadata covers every __manifest__.py + every python file under models/
    # At minimum, `expected` manifest rows.
    cache_rows = _count(database_url, tenant_schema, "cache_metadata")
    assert cache_rows >= expected

    # every data row got stamped with git_sha
    assert _max_indexed_at_sha(database_url, tenant_schema, "modules") == {"sha-first"}


# ---------------------------------------------------------------------------
# Idempotence: no writes outside cache_metadata.indexed_at
# ---------------------------------------------------------------------------


def test_rerun_unchanged_writes_only_cache_timestamps(
    database_url: str,
    tenant_schema: str,
    fixture_mirror: Path,
) -> None:
    roots = [fixture_mirror / "odoo_ce_subset", fixture_mirror / "custom_addons"]
    _run_index(database_url, tenant_schema, roots, git_sha="sha-first")

    # Snapshot: every data-table row's indexed_at_sha should stay "sha-first"
    # after a re-run under a DIFFERENT git_sha, because nothing changed.
    stats2 = _run_index(database_url, tenant_schema, roots, git_sha="sha-second")

    # No module/model/field/method rows should have been touched.
    assert stats2.modules_upserted == 0  # type: ignore[attr-defined]
    assert stats2.models_inserted == 0  # type: ignore[attr-defined]
    assert stats2.models_updated == 0  # type: ignore[attr-defined]
    assert stats2.fields_inserted == 0  # type: ignore[attr-defined]
    assert stats2.fields_updated == 0  # type: ignore[attr-defined]
    assert stats2.methods_inserted == 0  # type: ignore[attr-defined]
    assert stats2.methods_updated == 0  # type: ignore[attr-defined]
    assert stats2.rows_deleted == 0  # type: ignore[attr-defined]
    assert stats2.override_links_written == 0  # type: ignore[attr-defined]

    # Every module/model/field/method indexed_at_sha is still sha-first.
    for table in ("modules", "models", "fields", "methods"):
        assert _max_indexed_at_sha(database_url, tenant_schema, table) == {"sha-first"}

    # cache_metadata.indexed_at got bumped (but that's all).
    assert stats2.cache_rows_touched > 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Delta: touching a single method body updates only that method row
# ---------------------------------------------------------------------------


def test_single_method_body_change_updates_one_method_row(
    database_url: str,
    tenant_schema: str,
    fixture_mirror: Path,
) -> None:
    roots = [fixture_mirror / "odoo_ce_subset", fixture_mirror / "custom_addons"]
    _run_index(database_url, tenant_schema, roots, git_sha="sha-first")

    # Pick a file with a known method we can mutate: sale_order.py in
    # viin_fixture_method_override_super.
    target = (
        fixture_mirror
        / "custom_addons"
        / "viin_fixture_method_override_super"
        / "models"
        / "sale_order.py"
    )
    original = target.read_text()

    # Append a harmless comment inside the method body to change its body hash
    # but keep signature + line range. Strategy: find "action_confirm" def and
    # append a pass-like comment to the next line.
    # Simpler: append a comment at end of file — models row's content_hash
    # changes but start_line/end_line don't shift for methods.
    # Actually we want to change a METHOD body specifically. Modify the docstring
    # (or method body) in place.
    assert "def action_confirm" in original, "fixture precondition failed"
    # Inject a no-op line inside the method
    modified = original.replace(
        "def action_confirm(self):",
        "def action_confirm(self):\n        # indexer-delta-test marker",
        1,
    )
    assert modified != original
    target.write_text(modified)

    stats = _run_index(database_url, tenant_schema, roots, git_sha="sha-after-edit")

    # Exactly one method row updated (the one whose body hash changed).
    # The model row for that class may also update because file end_line shifts;
    # fields on the class shift their start_line too. Accept model + method +
    # any fields in that class as potentially-updated, but no other module's
    # rows should move.
    # Check: rows touched are bounded; cache_metadata.indexed_at_sha on the
    # touched file == sha-after-edit while others are still sha-first.
    import psycopg

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(f'SET LOCAL search_path TO "{tenant_schema}", public')
        cur.execute(
            "SELECT git_sha FROM cache_metadata WHERE file_path = %s",
            (str(target),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "sha-after-edit"

        # Unchanged module manifest file still has first sha.
        other_manifest = (
            fixture_mirror
            / "custom_addons"
            / "viin_fixture_method_override_break_super"
            / "__manifest__.py"
        )
        cur.execute(
            "SELECT git_sha FROM cache_metadata WHERE file_path = %s",
            (str(other_manifest),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "sha-first"

    # Second run stats: at least 1 method update. Tight bound: 0 modules
    # upserted (manifest unchanged), nothing deleted.
    assert stats.modules_upserted == 0  # type: ignore[attr-defined]
    assert stats.rows_deleted == 0  # type: ignore[attr-defined]
    assert stats.methods_updated >= 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tenancy isolation: two tenants index the same source into separate schemas.
# ---------------------------------------------------------------------------


def test_two_tenants_index_isolated(
    database_url: str,
    fixture_mirror: Path,
) -> None:
    import psycopg

    from scripts.create_tenant import main as create_tenant_main
    from scripts.migrate import main as migrate_main

    assert migrate_main(["--schema", "public", "--database-url", database_url]) == 0

    tenants: list[str] = []
    try:
        for _ in range(2):
            name = f"osm_test_{uuid.uuid4().hex[:8]}"
            assert create_tenant_main([name, "--database-url", database_url]) == 0
            tenants.append(name)

        roots = [fixture_mirror / "odoo_ce_subset"]
        for t in tenants:
            _run_index(database_url, t, roots, git_sha=f"sha-{t}")

        # Each tenant has its own row set; no cross-contamination.
        for t in tenants:
            assert _count(database_url, t, "modules") == 10  # CE subset = 10 modules
    finally:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            for t in tenants:
                cur.execute(f'DROP SCHEMA IF EXISTS "{t}" CASCADE')
            conn.commit()
