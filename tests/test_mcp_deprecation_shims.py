# SPDX-License-Identifier: AGPL-3.0-or-later
"""WI-D5 — shim contract tests for the 10 legacy tools deprecated in W-D4.

Three sections:

1. PARAMETRIZED EQUIVALENCE — 10 tests (one per legacy tool).
   For each tool: assert that
   - legacy_tool(...).content[0].text (or plain str) starts with "DEPRECATED:"
   - body after banner == superset_tool(...).content[0].text

2. CONTRACT TESTS (≥2):
   - test_banner_format_regex: regex match on exact banner phrase
   - test_all_10_legacy_tools_have_banner: enumerate all 10 by name, verify each emits banner

DB version: D5_99.0 — distinct from 99.0 / 94.0 / C4_99.0 / B4_94.0 used by other modules.

Runtime: ~12s (10 Neo4j round-trips for parametrized tests).
"""
import importlib
import os
import re

import pytest

from tests.conftest import seed_js_patches, seed_owl_components, seed_qweb_templates

pytestmark = pytest.mark.neo4j

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

D5_VERSION = "D5_99.0"
_MODULE_NAME = "d5_sale"
_MODEL_NAME = "d5.order"
_FIELD_NAME = "amount_total"
_METHOD_NAME = "action_confirm"
_VIEW_XMLID = "d5_sale.view_d5_order_form"
_OWL_COMP = "D5OrderKanban"
_QWEB_XMLID = "d5_sale.d5_order_tmpl"
_JS_TARGET = "D5Widget"
_JS_PATCH_NAME = "D5Widget.applyFilter"

# Banner pattern per ADR-0028 / WI-D4 implementation.
_BANNER_RE = re.compile(
    r"^DEPRECATED: use \w+\(.+\) instead\. Will be removed in v0\.6"
)

# ---------------------------------------------------------------------------
# Fixture — seed one module, one model, one of each UI-layer entity
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def d5_db(neo4j_driver, monkeypatch_module):
    """Seed minimal data covering all 10 legacy tool paths under D5_VERSION."""
    from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Wipe any leftover data from previous runs at this version.
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=D5_VERSION
        )

    mod = ModuleInfo(
        name=_MODULE_NAME,
        odoo_version=D5_VERSION,
        repo="odoo_test",
        path="/tmp/d5_sale",
        depends=["base"],
        edition="community",
    )
    model = ModelInfo(
        name=_MODEL_NAME,
        module=_MODULE_NAME,
        odoo_version=D5_VERSION,
        fields=[
            FieldInfo(_FIELD_NAME, "monetary", compute="_compute_total", stored=True),
            FieldInfo("name", "char", required=True),
            FieldInfo("state", "selection"),
        ],
        methods=[
            MethodInfo(_METHOD_NAME, has_super_call=True),
            MethodInfo("_compute_total"),
        ],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    # Seed View node (writer_neo4j does not expose view writing).
    with neo4j_driver.session() as session:
        session.run(
            """
            MERGE (v:View {xmlid: $xmlid, odoo_version: $ver})
            SET v.type = 'form', v.model = $model, v.module = $module,
                v.xpaths_exprs = [], v.xpaths_positions = [], v.profile = []
            """,
            xmlid=_VIEW_XMLID,
            ver=D5_VERSION,
            model=_MODEL_NAME,
            module=_MODULE_NAME,
        )

    # Seed OWL component.
    seed_owl_components(
        neo4j_driver,
        module=_MODULE_NAME,
        odoo_version=D5_VERSION,
        components=[{"name": _OWL_COMP, "bound_model": _MODEL_NAME, "template": None}],
    )

    # Seed QWeb template.
    seed_qweb_templates(
        neo4j_driver,
        module=_MODULE_NAME,
        odoo_version=D5_VERSION,
        templates=[{"xmlid": _QWEB_XMLID, "inherit_xmlid": None}],
    )

    # Seed JS patch.
    seed_js_patches(
        neo4j_driver,
        module=_MODULE_NAME,
        odoo_version=D5_VERSION,
        patches=[{"target": _JS_TARGET, "patch_name": _JS_PATCH_NAME, "era": "patch"}],
    )

    # Patch env vars so server.py connects to the test Neo4j instance.
    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    )
    monkeypatch_module.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password")
    )

    # Force server re-init against test instance (clears cached driver).
    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    # Teardown — delete seeded nodes.
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=D5_VERSION
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_text(result) -> str:
    """Extract the text string from a tool result (ToolResult or plain str)."""
    if isinstance(result, str):
        return result
    # ToolResult — .content[0].text
    return result.content[0].text


def _strip_banner(text: str) -> str:
    """Remove the DEPRECATED banner (header line + blank line separator)."""
    # Banner format: "DEPRECATED: ... ADR-0028.\n\n"
    # _deprecation_banner() always ends with "\n\n", so split on first double-newline.
    if "\n\n" in text:
        return text.split("\n\n", 1)[1]
    return text


# Pattern for opaque ref IDs minted by list_* tools — [ref=f12], [ref=m3], etc.
_REF_TOKEN_RE = re.compile(r"\[ref=[fmvxp]\d{1,5}\]")


def _mask_refs(text: str) -> str:
    """Replace all [ref=...] tokens with a stable placeholder.

    list_fields / list_methods / list_owl_components mint new ref IDs on every
    call via a thread-safe incrementing counter.  When the full test suite runs,
    other modules have already consumed earlier counter values, so the same tool
    called twice in sequence yields different ref numbers (f4 vs f8).

    Masking the tokens allows the body-equivalence assertion to remain
    meaningful (structure, entity names, types all match) without being brittle
    against the global counter state.
    """
    return _REF_TOKEN_RE.sub("[ref=<ID>]", text)


# ---------------------------------------------------------------------------
# Section 1 — PARAMETRIZED EQUIVALENCE (10 tests)
# ---------------------------------------------------------------------------

# Each entry: (legacy_tool_name, legacy_kwargs, superset_kwargs)
# superset_kwargs uses the *inner* _model_inspect / _module_inspect / _entity_lookup
# args so we call the @mcp.tool wrappers (model_inspect, module_inspect, entity_lookup).
_LEGACY_SUPERSET_PAIRS = [
    # --- resolve_model → model_inspect(method='summary') ---
    pytest.param(
        "resolve_model",
        {"target": _MODEL_NAME, "odoo_version": D5_VERSION},
        "model_inspect",
        {"model": _MODEL_NAME, "method": "summary", "odoo_version": D5_VERSION},
        id="resolve_model",
    ),
    # --- resolve_field → model_inspect(method='field', field=...) ---
    pytest.param(
        "resolve_field",
        {"target": f"{_MODEL_NAME}.{_FIELD_NAME}", "odoo_version": D5_VERSION},
        "model_inspect",
        {
            "model": _MODEL_NAME,
            "method": "field",
            "field": _FIELD_NAME,
            "odoo_version": D5_VERSION,
        },
        id="resolve_field",
    ),
    # --- resolve_method → model_inspect(method='method', method_name=...) ---
    pytest.param(
        "resolve_method",
        {"target": f"{_MODEL_NAME}.{_METHOD_NAME}", "odoo_version": D5_VERSION},
        "model_inspect",
        {
            "model": _MODEL_NAME,
            "method": "method",
            "method_name": _METHOD_NAME,
            "odoo_version": D5_VERSION,
        },
        id="resolve_method",
    ),
    # --- resolve_view → entity_lookup(kind='view', xmlid=...) ---
    pytest.param(
        "resolve_view",
        {"target": _VIEW_XMLID, "odoo_version": D5_VERSION},
        "entity_lookup",
        {"kind": "view", "xmlid": _VIEW_XMLID, "odoo_version": D5_VERSION},
        id="resolve_view",
    ),
    # --- list_fields → model_inspect(method='fields') ---
    pytest.param(
        "list_fields",
        {"model": _MODEL_NAME, "odoo_version": D5_VERSION},
        "model_inspect",
        {"model": _MODEL_NAME, "method": "fields", "odoo_version": D5_VERSION},
        id="list_fields",
    ),
    # --- list_methods → model_inspect(method='methods') ---
    pytest.param(
        "list_methods",
        {"model": _MODEL_NAME, "odoo_version": D5_VERSION},
        "model_inspect",
        {"model": _MODEL_NAME, "method": "methods", "odoo_version": D5_VERSION},
        id="list_methods",
    ),
    # --- list_views → model_inspect(method='views') ---
    pytest.param(
        "list_views",
        {"model": _MODEL_NAME, "odoo_version": D5_VERSION},
        "model_inspect",
        {"model": _MODEL_NAME, "method": "views", "odoo_version": D5_VERSION},
        id="list_views",
    ),
    # --- list_owl_components → module_inspect(method='owl') ---
    pytest.param(
        "list_owl_components",
        {"module": _MODULE_NAME, "odoo_version": D5_VERSION},
        "module_inspect",
        {"name": _MODULE_NAME, "method": "owl", "odoo_version": D5_VERSION},
        id="list_owl_components",
    ),
    # --- list_qweb_templates → module_inspect(method='qweb') ---
    pytest.param(
        "list_qweb_templates",
        {"module": _MODULE_NAME, "odoo_version": D5_VERSION},
        "module_inspect",
        {"name": _MODULE_NAME, "method": "qweb", "odoo_version": D5_VERSION},
        id="list_qweb_templates",
    ),
    # --- list_js_patches → module_inspect(method='js') ---
    pytest.param(
        "list_js_patches",
        {"odoo_version": D5_VERSION, "module": _MODULE_NAME},
        "module_inspect",
        {"name": _MODULE_NAME, "method": "js", "odoo_version": D5_VERSION},
        id="list_js_patches",
    ),
]


@pytest.mark.parametrize(
    "legacy_name,legacy_kw,superset_name,superset_kw",
    _LEGACY_SUPERSET_PAIRS,
)
def test_shim_emits_banner_and_body_matches_superset(
    d5_db,
    legacy_name,
    legacy_kw,
    superset_name,
    superset_kw,
):
    """(AC-D5-1, AC-D5-4) Legacy tool emits DEPRECATED banner and its body equals superset.

    Assertions:
    1. Legacy output text starts with "DEPRECATED:" (banner present).
    2. Legacy body after banner == superset tool output text (body equivalence).
    """
    server = importlib.import_module("src.mcp.server")

    legacy_fn = getattr(server, legacy_name).fn
    superset_fn = getattr(server, superset_name).fn

    legacy_result = legacy_fn(**legacy_kw)
    superset_result = superset_fn(**superset_kw)

    legacy_text = _get_text(legacy_result)
    superset_text = _get_text(superset_result)

    # Assertion 1 — banner present.
    assert legacy_text.startswith("DEPRECATED:"), (
        f"{legacy_name}: output does not start with DEPRECATED banner.\n"
        f"Got: {legacy_text[:120]!r}"
    )

    # Assertion 2 — body equivalence after stripping banner and normalizing refs.
    # Ref IDs ([ref=f1], [ref=m3], etc.) are minted by a global incrementing
    # counter; calling two tools in sequence yields different IDs in the full
    # test suite.  We mask them so the assertion verifies structure + names
    # without being brittle against global counter state.
    legacy_body = _mask_refs(_strip_banner(legacy_text))
    superset_normalized = _mask_refs(superset_text)
    assert legacy_body == superset_normalized, (
        f"{legacy_name}: body after banner-strip does not match superset "
        f"({superset_name}) output.\n"
        f"legacy_body: {legacy_body[:200]!r}\n"
        f"superset:    {superset_normalized[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Section 2 — CONTRACT TESTS
# ---------------------------------------------------------------------------


def test_banner_format_regex(d5_db):
    """(AC-D5-1) Banner text matches the canonical DEPRECATED regex pattern.

    Checks resolve_model as a representative — the banner helper is shared.
    """
    server = importlib.import_module("src.mcp.server")
    result = server.resolve_model.fn(target=_MODEL_NAME, odoo_version=D5_VERSION)
    text = _get_text(result)
    assert _BANNER_RE.match(text), (
        f"Banner does not match expected regex pattern.\n"
        f"Pattern: {_BANNER_RE.pattern!r}\n"
        f"Got:     {text[:200]!r}"
    )


_ALL_10_LEGACY_TOOLS = [
    "resolve_model",
    "resolve_field",
    "resolve_method",
    "resolve_view",
    "list_fields",
    "list_methods",
    "list_views",
    "list_owl_components",
    "list_qweb_templates",
    "list_js_patches",
]

_MINIMAL_KWARGS: dict[str, dict] = {
    "resolve_model": {"target": _MODEL_NAME, "odoo_version": D5_VERSION},
    "resolve_field": {
        "target": f"{_MODEL_NAME}.{_FIELD_NAME}",
        "odoo_version": D5_VERSION,
    },
    "resolve_method": {
        "target": f"{_MODEL_NAME}.{_METHOD_NAME}",
        "odoo_version": D5_VERSION,
    },
    "resolve_view": {"target": _VIEW_XMLID, "odoo_version": D5_VERSION},
    "list_fields": {"model": _MODEL_NAME, "odoo_version": D5_VERSION},
    "list_methods": {"model": _MODEL_NAME, "odoo_version": D5_VERSION},
    "list_views": {"model": _MODEL_NAME, "odoo_version": D5_VERSION},
    "list_owl_components": {"module": _MODULE_NAME, "odoo_version": D5_VERSION},
    "list_qweb_templates": {"module": _MODULE_NAME, "odoo_version": D5_VERSION},
    "list_js_patches": {"odoo_version": D5_VERSION, "module": _MODULE_NAME},
}


@pytest.mark.parametrize("tool_name", _ALL_10_LEGACY_TOOLS, ids=_ALL_10_LEGACY_TOOLS)
def test_all_10_legacy_tools_have_banner(d5_db, tool_name):
    """(AC-D5-4) Each of the 10 legacy tools emits the DEPRECATED banner.

    Verifies that WI-D4 shim wiring covers every tool, not just the ones
    enumerated in the parametrized equivalence test above.
    """
    server = importlib.import_module("src.mcp.server")
    fn = getattr(server, tool_name).fn
    kwargs = _MINIMAL_KWARGS[tool_name]
    result = fn(**kwargs)
    text = _get_text(result)
    assert text.startswith("DEPRECATED:"), (
        f"{tool_name}: output does not start with DEPRECATED banner.\n"
        f"First 200 chars: {text[:200]!r}"
    )
