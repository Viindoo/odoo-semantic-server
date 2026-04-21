"""Golden-file tests for the 3 P1 handlers.

Boots a throwaway tenant schema, runs the WP-6 indexer over the shared
fixture corpus, then compares each handler's response to the labeled
entries in `tests/fixtures/golden/*.json`. TODO-only entries are skipped
so golden labelling can catch up without blocking this test.

Skipped when `DATABASE_URL` is unset.
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
    reason="DATABASE_URL not set; skipping handler golden tests",
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
CE_SUBSET = FIXTURES / "odoo_ce_subset"
CUSTOM_ADDONS = FIXTURES / "custom_addons"
GOLDEN = FIXTURES / "golden"


# ---------------------------------------------------------------------------
# Shared fixture: indexed tenant schema
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    assert url is not None
    return url


@pytest.fixture(scope="module")
def indexed_tenant(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    import psycopg

    from osm.indexer.driver import index
    from scripts.create_tenant import main as create_tenant_main
    from scripts.migrate import main as migrate_main

    tmp = tmp_path_factory.mktemp("wp8_corpus")
    shutil.copytree(CE_SUBSET, tmp / "odoo_ce_subset")
    shutil.copytree(CUSTOM_ADDONS, tmp / "custom_addons")

    assert migrate_main(["--schema", "public", "--database-url", database_url]) == 0

    name = f"osm_wp8_{uuid.uuid4().hex[:8]}"
    assert create_tenant_main([name, "--database-url", database_url]) == 0
    try:
        with psycopg.connect(database_url) as conn:
            index(
                addon_roots=[tmp / "odoo_ce_subset", tmp / "custom_addons"],
                conn=conn,
                tenant=name,
                git_sha="golden-fixture",
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx(schema: str) -> Any:
    from osm.server.tenancy import context_from_tenant

    return context_from_tenant(schema)


def _labeled(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Golden entries whose chain the handler is expected to reproduce.

    Drops TODO skeletons (labelling pending) and any entry that carries a
    `skip_handler` marker documenting a known P2+ feature gap.
    """
    return [e for e in entries if "TODO" not in e and "skip_handler" not in e]


def _normalise_file_path(path: str) -> str:
    """Golden file paths are relative to repo root; live query returns paths
    relative to the tmp-path corpus. Normalise by taking the tail after
    `odoo_ce_subset/` or `custom_addons/` so the comparison is stable.
    """
    for m in ("odoo_ce_subset/", "custom_addons/"):
        idx = path.find(m)
        if idx != -1:
            return "tests/fixtures/" + path[idx:]
    return path


def _normalise_entry(entry: dict[str, Any]) -> dict[str, Any]:
    entry = dict(entry)
    if "file" in entry and isinstance(entry["file"], str):
        entry["file"] = _normalise_file_path(entry["file"])
    return entry


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


def test_resolve_model_matches_golden(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.handlers.resolve_model import resolve_model

    ctx = _tenant_ctx(indexed_tenant)
    golden = json.loads((GOLDEN / "resolve_model.json").read_text())

    for entry in _labeled(golden):
        env = resolve_model(cursor, ctx, entry["model_name"])
        got = env["result"]
        got_chain = [_normalise_entry(e) for e in got["chain"]]
        # Drop `kind: primary` markers for byte-equality since golden uses
        # implicit kind via chain position, not an explicit marker.
        for e in got_chain:
            e.pop("kind", None)
        expected_chain = [_normalise_entry(e) for e in entry["chain"]]
        for e in expected_chain:
            e.pop("kind", None)
        assert got_chain == expected_chain, f"chain mismatch for {entry['model_name']}"
        assert got["abstract"] == entry["abstract"]
        assert got["transient"] == entry["transient"]
        assert got["inherits"] == entry["inherits"]


def test_resolve_model_404_on_unknown(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.errors import NotFoundError
    from osm.server.handlers.resolve_model import resolve_model

    ctx = _tenant_ctx(indexed_tenant)
    with pytest.raises(NotFoundError):
        resolve_model(cursor, ctx, "does.not.exist")


def test_resolve_model_400_on_empty(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.errors import InvalidInputError
    from osm.server.handlers.resolve_model import resolve_model

    ctx = _tenant_ctx(indexed_tenant)
    with pytest.raises(InvalidInputError):
        resolve_model(cursor, ctx, "")


# ---------------------------------------------------------------------------
# resolve_field
# ---------------------------------------------------------------------------


def test_resolve_field_matches_golden(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.handlers.resolve_field import resolve_field

    ctx = _tenant_ctx(indexed_tenant)
    golden = json.loads((GOLDEN / "resolve_field.json").read_text())

    for entry in _labeled(golden):
        env = resolve_field(cursor, ctx, entry["model_name"], entry["field_name"])
        got_chain = [_normalise_entry(e) for e in env["result"]["chain"]]
        # kind marker is informational; strip to match golden
        for e in got_chain:
            e.pop("kind", None)
        expected_chain = [_normalise_entry(e) for e in entry["chain"]]
        for e in expected_chain:
            e.pop("kind", None)
        assert got_chain == expected_chain, (
            f"field chain mismatch for {entry['model_name']}.{entry['field_name']}"
        )


def test_resolve_field_404(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.errors import NotFoundError
    from osm.server.handlers.resolve_field import resolve_field

    ctx = _tenant_ctx(indexed_tenant)
    with pytest.raises(NotFoundError):
        resolve_field(cursor, ctx, "sale.order", "no_such_field")


# ---------------------------------------------------------------------------
# resolve_method
# ---------------------------------------------------------------------------


def test_resolve_method_matches_golden(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.handlers.resolve_method import resolve_method

    ctx = _tenant_ctx(indexed_tenant)
    golden = json.loads((GOLDEN / "resolve_method.json").read_text())

    for entry in _labeled(golden):
        env = resolve_method(cursor, ctx, entry["model_name"], entry["method_name"])
        result = env["result"]
        got_chain = [_normalise_entry(e) for e in result["chain"]]
        expected_chain = [_normalise_entry(e) for e in entry["chain"]]
        assert got_chain == expected_chain, (
            f"method chain mismatch for {entry['model_name']}.{entry['method_name']}"
        )
        assert result["chain_is_broken"] == entry["chain_is_broken"], (
            f"chain_is_broken mismatch for {entry['model_name']}.{entry['method_name']}"
        )


def test_resolve_method_404(cursor: Any, indexed_tenant: str) -> None:
    from osm.server.errors import NotFoundError
    from osm.server.handlers.resolve_method import resolve_method

    ctx = _tenant_ctx(indexed_tenant)
    with pytest.raises(NotFoundError):
        resolve_method(cursor, ctx, "sale.order", "no_such_method")
