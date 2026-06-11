# SPDX-License-Identifier: AGPL-3.0-or-later
"""WI-1 (#238): writability signals (related= / readonly) in field render + writer.

Business rule under protection: an AI client must be able to tell, from
`model_inspect(method='fields')` (list) and `_resolve_field` (detail), whether
a field is effectively read-only — i.e. a stored-related or computed-without-
inverse field the ORM silently ignores on create()/write(). Surfacing this
prevents the false-green / false-red consumer trap described in #238.

Covers:
  - Writer round-trip: f.readonly / f.inverse / f.effective_readonly persisted.
  - Detail render (_resolve_field): "Readonly: Yes" for stored-related,
    "Readonly: No" for a plain writable field.
  - List render (_list_fields): stored-related row carries `related=` + `readonly`.
  - Graceful degradation: a Field node missing the new properties renders
    without crashing and without a misleading "Readonly: No" / "readonly" marker.

DB version: TEST_VERSION = "98.0" (isolated namespace).
"""
import importlib
import os

import pytest

pytestmark = pytest.mark.neo4j

TEST_VERSION = "98.0"


@pytest.fixture(scope="module")
def ro_db(neo4j_driver, monkeypatch_module):
    """Seed a model with stored-related + writable + computed-with-inverse fields."""
    from src.indexer.models import FieldInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)

    mod = ModuleInfo(
        name="ro_mod", odoo_version=TEST_VERSION, repo="odoo_test",
        path="/tmp/ro_mod", depends=["base"], edition="community",
    )
    model = ModelInfo(
        name="ro.model",
        module="ro_mod",
        odoo_version=TEST_VERSION,
        fields=[
            # stored-related, no compute, no inverse -> effective_readonly True
            FieldInfo(
                "res_model", "char",
                related="workflow_id.model_name", stored=True,
                readonly=None, inverse=None, effective_readonly=True,
            ),
            # plain writable field
            FieldInfo(
                "name", "char",
                readonly=None, inverse=None, effective_readonly=False,
            ),
            # computed WITH inverse setter -> writable
            FieldInfo(
                "alias", "char",
                compute="_compute_alias", inverse="_set_alias",
                stored=True, readonly=None, effective_readonly=False,
            ),
        ],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    )
    monkeypatch_module.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password")
    )

    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


def test_writer_persists_readonly_properties(ro_db, neo4j_driver):
    """Writer round-trip: the new Field properties land in Neo4j."""
    with neo4j_driver.session() as s:
        rec = s.run(
            """
            MATCH (f:Field {name: 'res_model', model: 'ro.model', odoo_version: $v})
            RETURN f.readonly AS ro, f.inverse AS inv,
                   f.effective_readonly AS eff, f.related AS rel
            """,
            v=TEST_VERSION,
        ).single()
    assert rec is not None
    assert rec["eff"] is True
    assert rec["rel"] == "workflow_id.model_name"
    assert rec["inv"] is None
    # readonly kwarg was absent (None) on the source field
    assert rec["ro"] is None


def test_detail_render_readonly_flag(ro_db):
    """_resolve_field detail: stored-related = Readonly Yes; writable = Readonly No."""
    server = importlib.import_module("src.mcp.server")

    ro_out = server._resolve_field("ro.model", "res_model", TEST_VERSION)
    assert "Readonly: Yes" in ro_out, ro_out

    rw_out = server._resolve_field("ro.model", "name", TEST_VERSION)
    assert "Readonly: No" in rw_out, rw_out

    # computed-with-inverse is writable
    alias_out = server._resolve_field("ro.model", "alias", TEST_VERSION)
    assert "Readonly: No" in alias_out, alias_out


def test_list_render_readonly_and_related(ro_db):
    """_list_fields list: stored-related row carries `related=` + `readonly`."""
    server = importlib.import_module("src.mcp.server")

    out = server._list_fields("ro.model", TEST_VERSION)
    # Find the res_model row.
    ro_lines = [ln for ln in out.splitlines() if "res_model" in ln]
    assert ro_lines, f"res_model row missing in list output:\n{out}"
    row = ro_lines[0]
    assert "related=workflow_id.model_name" in row, row
    assert "readonly" in row, row

    # writable field must NOT carry a readonly marker.
    name_lines = [ln for ln in out.splitlines() if "name :" in ln or " name " in ln]
    # The plain `name` row should not contain the bare `readonly` token.
    assert all("readonly" not in ln for ln in name_lines if "res_model" not in ln), out


def test_graceful_degradation_missing_properties(ro_db, neo4j_driver):
    """Pre-reindex Field nodes lack the new props — render must not crash and must
    NOT print a misleading 'Readonly: No' (detail) or 'readonly' marker (list)."""
    server = importlib.import_module("src.mcp.server")

    # Simulate a legacy node: remove the new properties from `name`.
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (f:Field {name: 'name', model: 'ro.model', odoo_version: $v})
            REMOVE f.readonly, f.inverse, f.effective_readonly
            """,
            v=TEST_VERSION,
        )

    # Detail: no Readonly line at all (omitted, not 'No').
    detail = server._resolve_field("ro.model", "name", TEST_VERSION)
    assert "Readonly:" not in detail, detail
    # Field still resolves normally.
    assert "ro.model.name" in detail, detail

    # List: the legacy row must not carry a `readonly` marker.
    out = server._list_fields("ro.model", TEST_VERSION)
    legacy_rows = [
        ln for ln in out.splitlines()
        if ln.lstrip().startswith(("name :", "[ref")) and "name :" in ln
    ]
    for ln in legacy_rows:
        assert "readonly" not in ln, f"legacy row should omit readonly marker: {ln}"

    # Restore for any later test ordering safety.
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (f:Field {name: 'name', model: 'ro.model', odoo_version: $v})
            SET f.effective_readonly = false
            """,
            v=TEST_VERSION,
        )


# NOTE: the effective-readonly business invariant (stored-related field surfaced
# as read-only) is fully covered by test_detail_render_readonly_flag (via the LIVE
# _resolve_field text path) and test_list_render_readonly_and_related (via
# _list_fields). The former test_structured_readonly_passthrough probed the
# now-removed _resolve_field_structured helper and was redundant — it was dropped
# with the structured subsystem (ADR-0028); the invariant stays protected by the
# live-path tests above.
