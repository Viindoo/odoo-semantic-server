"""Integration tests for ``osm.server.handlers.resolve_view``.

Boots a throwaway tenant schema, runs the indexer over the shared fixture
corpus, then asserts handler behaviour against the golden file
``tests/fixtures/golden/resolve_view.json``. DB-gated via ``DATABASE_URL``.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; skipping resolve_view integration tests",
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
CE_SUBSET = FIXTURES / "odoo_ce_subset"
CUSTOM_ADDONS = FIXTURES / "custom_addons"
GOLDEN = FIXTURES / "golden"


# ---------------------------------------------------------------------------
# Shared fixtures: indexed tenant schema
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    assert url is not None
    return url


@pytest.fixture(scope="module")
def indexed_tenant(
    database_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    import psycopg

    from osm.indexer.driver import index
    from scripts.create_tenant import main as create_tenant_main
    from scripts.migrate import main as migrate_main

    tmp = tmp_path_factory.mktemp("wp16_corpus")
    shutil.copytree(CE_SUBSET, tmp / "odoo_ce_subset")
    shutil.copytree(CUSTOM_ADDONS, tmp / "custom_addons")

    assert migrate_main(["--schema", "public", "--database-url", database_url]) == 0

    name = f"osm_wp16_{uuid.uuid4().hex[:8]}"
    assert create_tenant_main([name, "--database-url", database_url]) == 0
    try:
        with psycopg.connect(database_url) as conn:
            index(
                addon_roots=[tmp / "odoo_ce_subset", tmp / "custom_addons"],
                conn=conn,
                tenant=name,
                git_sha="wp16-fixture",
            )
            conn.commit()
        yield name
    finally:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            conn.commit()


@pytest.fixture()
def cursor(database_url: str) -> Iterator[Any]:
    import psycopg

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        yield cur


def _tenant_ctx(schema: str) -> Any:
    from osm.server.tenancy import context_from_tenant

    return context_from_tenant(schema)


def _labeled(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if "TODO" not in e and "skip_handler" not in e]


# ---------------------------------------------------------------------------
# Golden parity
# ---------------------------------------------------------------------------


def test_resolve_view_matches_golden(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.errors import NotFoundError
    from osm.server.handlers.resolve_view import resolve_view

    ctx = _tenant_ctx(indexed_tenant)
    golden = json.loads((GOLDEN / "resolve_view.json").read_text())

    for entry in _labeled(golden):
        xmlid = entry["xmlid"]
        kwargs = {
            "include_final_xml": entry.get("include_final_xml", True),
            "include_patch_log": entry.get("include_patch_log", True),
        }
        if entry.get("expect_404"):
            with pytest.raises(NotFoundError):
                resolve_view(cursor, ctx, xmlid, **kwargs)
            continue
        env = resolve_view(cursor, ctx, xmlid, **kwargs)
        # Byte-equality modulo indexed_at_sha.
        assert env["result"] == entry["result"], (
            f"result mismatch for {entry.get('label') or xmlid}"
        )
        assert env["warnings"] == entry["warnings"], (
            f"warnings mismatch for {entry.get('label') or xmlid}"
        )


# ---------------------------------------------------------------------------
# 404 / 409 / tenant isolation
# ---------------------------------------------------------------------------


def test_404_on_unknown_xmlid(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.errors import NotFoundError
    from osm.server.handlers.resolve_view import resolve_view

    ctx = _tenant_ctx(indexed_tenant)
    with pytest.raises(NotFoundError):
        resolve_view(cursor, ctx, "cv_basic_form.does_not_exist")


def test_404_on_missing_module_prefix(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.errors import NotFoundError
    from osm.server.handlers.resolve_view import resolve_view

    ctx = _tenant_ctx(indexed_tenant)
    with pytest.raises(NotFoundError):
        resolve_view(cursor, ctx, "no_such_module.no_such_view")


def test_400_on_empty_xmlid(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.errors import InvalidInputError
    from osm.server.handlers.resolve_view import resolve_view

    ctx = _tenant_ctx(indexed_tenant)
    with pytest.raises(InvalidInputError):
        resolve_view(cursor, ctx, "")


def test_409_on_stale_cross_schema_sha(
    cursor: Any, indexed_tenant: str, database_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Simulate public re-index divergence mid-session: extension view rows
    in public end up with a different indexed_at_sha than the tenant chain
    rows. Handler must raise StaleIndexError (409).
    """
    import psycopg

    from osm.indexer.driver import index
    from osm.server.errors import StaleIndexError
    from osm.server.handlers.resolve_view import resolve_view
    from scripts.migrate import main as migrate_main

    # Index the custom fixtures into public under a different SHA than the
    # tenant carries. The tenant was indexed with 'wp16-fixture'; use a
    # distinct SHA so cross-schema rows disagree on indexed_at_sha. Include
    # base + CE subset so cv_* / viin_* dependency resolution succeeds.
    tmp = tmp_path_factory.mktemp("wp16_public_stale")
    shutil.copytree(CE_SUBSET, tmp / "odoo_ce_subset")
    shutil.copytree(CUSTOM_ADDONS, tmp / "custom_addons")
    assert migrate_main(["--schema", "public", "--database-url", database_url]) == 0
    with psycopg.connect(database_url) as conn:
        index(
            addon_roots=[tmp / "odoo_ce_subset", tmp / "custom_addons"],
            conn=conn,
            tenant="public",
            git_sha="wp16-public-stale-sha",
        )
        conn.commit()

    ctx = _tenant_ctx(indexed_tenant)
    # cv_basic_form.cv_basic_partner_form now has rows in both schemas with
    # different indexed_at_sha values; handler should 409.
    with pytest.raises(StaleIndexError):
        resolve_view(cursor, ctx, "cv_basic_form.cv_basic_partner_form")

    # Cleanup: wipe every module row we inserted into public so adjacent
    # tests using the same DB start from a clean shared-schema state.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM public.modules")
        conn.commit()


def test_tenant_private_view_does_not_bleed_to_public(
    database_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A view only defined in a tenant schema must resolve there; a public
    tenant context (``schemas=('public',)`` only) must not see it.
    """
    import psycopg

    from osm.indexer.driver import index
    from osm.server.errors import NotFoundError
    from osm.server.handlers.resolve_view import resolve_view
    from osm.server.tenancy import context_from_tenant
    from scripts.create_tenant import main as create_tenant_main
    from scripts.migrate import main as migrate_main

    tmp = tmp_path_factory.mktemp("wp16_private")
    # Include base so cv_basic_form's depends=['base'] resolves — otherwise
    # load_order drops the fixture module.
    shutil.copytree(CE_SUBSET / "base", tmp / "base")
    shutil.copytree(CUSTOM_ADDONS / "cv_basic_form", tmp / "cv_basic_form")

    assert migrate_main(["--schema", "public", "--database-url", database_url]) == 0
    name = f"osm_wp16_p_{uuid.uuid4().hex[:8]}"
    assert create_tenant_main([name, "--database-url", database_url]) == 0
    try:
        with psycopg.connect(database_url) as conn:
            index(
                addon_roots=[tmp],
                conn=conn,
                tenant=name,
                git_sha="wp16-private",
            )
            conn.commit()

        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # Tenant context sees the view.
            ctx_tenant = context_from_tenant(name)
            env = resolve_view(cur, ctx_tenant, "cv_basic_form.cv_basic_partner_form")
            assert env["result"]["xmlid"] == "cv_basic_form.cv_basic_partner_form"

            # Public context does not (row lives only in tenant schema).
            ctx_public = context_from_tenant("public")
            with pytest.raises(NotFoundError):
                resolve_view(cur, ctx_public, "cv_basic_form.cv_basic_partner_form")
    finally:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            conn.commit()
