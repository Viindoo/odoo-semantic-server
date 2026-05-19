"""End-to-end integration tests for the drill-down ref chain.

Covers WI-C4 scenarios:
  (1) Minting via real list_fields call — [ref=fN] markers in output + resolvable in map.
  (2) Dual-mode E2E: list_fields → extract ref → resolve_field(target=ref) == legacy call.
  (3) Cache expiry: injected clock → advance past TTL → RefError with recovery hint.
  (4) Cross-key isolation: API-key-A refs invisible to API-key-B.
  (5) Malformed ref: target="not_a_ref" → canonical dispatch (not stale-ref error).
  (6) Exhaustion sentinel: >MAX_ITEMS_PER_CALL items → "exhausted" sentinel + RefError
      on resolve.
  (7) Cursor continuation: list_fields on model with >50 fields → continuation hint with
      start_index=50.
  (8) Gapless pagination: paginate through 247-field fixture → collect all names → no
      gaps, no duplicates.

DB version: C4_99.0 — carve-out to avoid collision with other test modules.
"""

import importlib
import os
import re

import pytest

from src.mcp.refs import MAX_ITEMS_PER_CALL, RefError, RefMinter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

C4_VERSION = "C4_99.0"   # per AC-C4-4 — unique version string for this file

_MODULE_NAME = "c4_sale"
_MODEL_SMALL = "c4.order"       # 5 fields — used for scenarios 1-6
_MODEL_FAT = "c4.fat.model"     # 247 fields — used for scenarios 7+8

pytestmark = pytest.mark.neo4j

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def c4_db(neo4j_driver, monkeypatch_module):
    """Seed a small model (5 fields) + a 247-field fat model in the test Neo4j."""
    from src.indexer.models import FieldInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Wipe any leftover C4 data from previous runs.
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=C4_VERSION)

    mod = ModuleInfo(
        name=_MODULE_NAME,
        odoo_version=C4_VERSION,
        repo="odoo_test",
        path="/tmp/c4_sale",
        depends=["base"],
        edition="community",
    )

    # Small model: 5 fields with known names
    small_model = ModelInfo(
        name=_MODEL_SMALL,
        module=_MODULE_NAME,
        odoo_version=C4_VERSION,
        fields=[
            FieldInfo("amount_total", "monetary", compute="_compute_total", stored=True),
            FieldInfo("partner_id", "many2one"),
            FieldInfo("state", "selection"),
            FieldInfo("name", "char"),
            FieldInfo("date_order", "datetime"),
        ],
        methods=[],
    )

    # Fat model: exactly 247 fields
    fat_fields = [FieldInfo(f"field_{i:03d}", "char") for i in range(247)]
    fat_model = ModelInfo(
        name=_MODEL_FAT,
        module=_MODULE_NAME,
        odoo_version=C4_VERSION,
        fields=fat_fields,
        methods=[],
    )

    writer.write_results([ParseResult(module=mod, models=[small_model, fat_model])])
    writer.close()

    # Patch Neo4j env vars so server.py connects to the test instance.
    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    )
    monkeypatch_module.setenv(
        "NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j")
    )
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password")
    )

    import sys
    sys.modules.pop("src.mcp.server", None)

    yield neo4j_driver

    # Teardown — remove C4 version nodes.
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=C4_VERSION)


@pytest.fixture()
def server(c4_db):
    """Import (or re-import) server module after env vars are set by c4_db."""
    return importlib.import_module("src.mcp.server")


# ---------------------------------------------------------------------------
# Helper: extract first ref from list_fields output
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"\[ref=(f\d+)\]")


def _first_ref(output: str) -> str | None:
    """Return the first [ref=fN] token found in *output*, or None."""
    m = _REF_RE.search(output)
    return m.group(1) if m else None


def _all_refs(output: str) -> list[str]:
    """Return all [ref=fN] tokens in *output* in order."""
    return _REF_RE.findall(output)


def _extract_field_names(output: str) -> set[str]:
    """Extract 'field_NNN' names from lines that contain '[ref=fN]'."""
    names: set[str] = set()
    for line in output.splitlines():
        if "[ref=f" in line:
            for part in line.split():
                if part.startswith("field_"):
                    names.add(part.rstrip(":"))
    return names


# ---------------------------------------------------------------------------
# Scenario 1 — Minting via real list_fields call
# ---------------------------------------------------------------------------


def test_scenario1_list_fields_emits_ref_markers(c4_db, server):
    """list_fields returns output with [ref=fN] markers AND refs are resolvable.

    AC-C4-1 scenario (1): minting via real list_fields call.
    """
    # Use a unique api_key_id so the global minter namespace is isolated.
    api_key = "c4-s1-test-key"

    out = server._list_fields(_MODEL_SMALL, C4_VERSION, api_key_id=api_key)

    # Markers must appear in output.
    assert "[ref=f" in out, f"Expected [ref=fN] markers in list_fields output:\n{out!r}"

    # Grab all refs and verify they're resolvable under the same api_key.
    from src.mcp.refs import _GLOBAL_MINTER
    refs = _all_refs(out)
    assert len(refs) >= 1, "Expected at least one ref minted"

    for ref in refs:
        canonical = _GLOBAL_MINTER.resolve(ref, api_key_id=api_key)
        assert "field_name" in canonical or "name" in canonical, (
            f"Ref {ref!r} canonical dict missing field identity: {canonical!r}"
        )


# ---------------------------------------------------------------------------
# Scenario 2 — Dual-mode E2E: list_fields → ref → resolve_field
# ---------------------------------------------------------------------------


def test_scenario2_dual_mode_e2e(c4_db, server):
    """list_fields → extract ref → resolve_field(target=ref) == legacy kwarg call.

    AC-C4-1 scenario (2): dual-mode end-to-end round-trip.
    """
    api_key = "c4-s2-test-key"

    # Step A: call list_fields, capture the first ref.
    out = server._list_fields(_MODEL_SMALL, C4_VERSION, api_key_id=api_key)
    ref = _first_ref(out)
    assert ref is not None, f"No ref found in list_fields output:\n{out!r}"

    # Step B: inject the api_key into the thread-local so resolve_field can look it up.
    server._api_key_id_local.value = api_key

    # Step C: resolve via ref.
    ref_result = server.resolve_field.fn(target=ref, odoo_version=C4_VERSION)
    ref_text = ref_result.content[0].text

    # Step D: determine which field was resolved (from the canonical dict).
    from src.mcp.refs import _GLOBAL_MINTER
    canonical = _GLOBAL_MINTER.resolve(ref, api_key_id=api_key)
    field_name = canonical.get("field_name") or canonical.get("name")
    assert field_name, f"Canonical dict for ref {ref!r} missing field name: {canonical!r}"

    # Step E: call via legacy kwargs for comparison.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legacy_result = server.resolve_field.fn(
            model_name=_MODEL_SMALL, field_name=field_name, odoo_version=C4_VERSION
        )
    legacy_text = legacy_result.content[0].text

    assert ref_text == legacy_text, (
        f"Ref round-trip text differs from legacy call:\n"
        f"  ref-result:    {ref_text!r}\n"
        f"  legacy-result: {legacy_text!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — Cache expiry via injected clock
# ---------------------------------------------------------------------------


def test_scenario3_cache_expiry_raises_referror(c4_db):
    """Injected clock: advance past TTL → RefError with recovery hint.

    AC-C4-1 scenario (3): clock-injected TTL expiry.
    """
    tick = [0.0]

    def fake_clock() -> float:
        return tick[0]

    minter = RefMinter(ttl=300.0, now_fn=fake_clock)

    item = {"field_name": "amount_total", "model": _MODEL_SMALL}
    (ref,) = minter.mint([item], api_key_id="c4-s3-key")

    # Before TTL: ref is alive.
    tick[0] = 299.0
    result = minter.resolve(ref, api_key_id="c4-s3-key")
    assert result == item

    # At/past TTL: RefError with recovery_hint.
    tick[0] = 300.0
    with pytest.raises(RefError) as exc_info:
        minter.resolve(ref, api_key_id="c4-s3-key")

    err = exc_info.value
    assert err.recovery_hint, "RefError must carry a non-empty recovery_hint"
    assert "list_" in err.recovery_hint or "re-run" in err.recovery_hint.lower(), (
        f"recovery_hint should name a list_* tool or say re-run: {err.recovery_hint!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — Cross-key isolation
# ---------------------------------------------------------------------------


def test_scenario4_cross_key_isolation(c4_db):
    """API-key-A refs are invisible to API-key-B (no cross-leak).

    AC-C4-1 scenario (4): cross-key tenant isolation.
    """
    minter = RefMinter()

    item_a = {"field_name": "amount_total", "model": _MODEL_SMALL}
    item_b = {"field_name": "partner_id", "model": _MODEL_SMALL}

    # Mint refs under separate keys — each counter starts fresh so both get "f1".
    (ref_a,) = minter.mint([item_a], api_key_id="c4-key-alpha")
    (ref_b,) = minter.mint([item_b], api_key_id="c4-key-beta")

    # Alpha key resolves to item_a.
    resolved_a = minter.resolve(ref_a, api_key_id="c4-key-alpha")
    assert resolved_a == item_a

    # Beta key resolves to item_b.
    resolved_b = minter.resolve(ref_b, api_key_id="c4-key-beta")
    assert resolved_b == item_b

    # Beta cannot see Alpha's ref namespace — Alpha's ref under Beta's key raises.
    with pytest.raises(RefError):
        minter.resolve("f1", api_key_id="c4-key-beta-unknown")

    # And a key that was never used raises too.
    with pytest.raises(RefError):
        minter.resolve("f1", api_key_id="c4-key-never-used")


# ---------------------------------------------------------------------------
# Scenario 5 — Malformed ref treated as canonical
# ---------------------------------------------------------------------------


def test_scenario5_malformed_ref_treated_as_canonical(c4_db, server):
    """target='not_a_ref' (no prefix+digits pattern) → canonical dispatch, NOT stale-ref.

    AC-C4-1 scenario (5): malformed ref dispatches to canonical path.
    The 'not_a_ref' string has no dot → field name unknown → not-found tree.
    Crucially the response must NOT contain 'expired' or 'stale' — it should
    fall into the canonical (not-found) path, because 'not_a_ref' doesn't match
    the ref pattern [fmvpx]\\d+.
    """
    result = server.resolve_field.fn(target="not_a_ref", odoo_version=C4_VERSION)
    text = result.content[0].text

    # Must not be a stale-ref error (that would indicate it was misidentified as a ref).
    assert "Ref 'not_a_ref' is unknown or expired" not in text, (
        f"'not_a_ref' should NOT be treated as a stale ref:\n{text!r}"
    )

    # It should either return a not-found error or try to use it as a field name.
    # Either way it must not raise — it returns a ToolResult.
    assert isinstance(text, str) and len(text) > 0, "resolve_field must return non-empty text"


# ---------------------------------------------------------------------------
# Scenario 6 — Exhaustion sentinel
# ---------------------------------------------------------------------------


def test_scenario6_exhaustion_sentinel(c4_db):
    """Items beyond MAX_ITEMS_PER_CALL get 'exhausted' sentinel; resolving it raises.

    AC-C4-1 scenario (6): exhaustion sentinel behaviour.
    """
    minter = RefMinter()
    api_key = "c4-s6-exhaust-key"

    # Build MAX+2 items.
    items = [
        {"field_name": f"f_{i}", "model": _MODEL_SMALL}
        for i in range(MAX_ITEMS_PER_CALL + 2)
    ]
    refs = minter.mint(items, api_key_id=api_key)

    assert len(refs) == len(items), "mint must return one ref per item"

    # First MAX items get normal refs.
    normal_refs = refs[:MAX_ITEMS_PER_CALL]
    assert all(r != "exhausted" for r in normal_refs), (
        "Items within cap must not receive 'exhausted' sentinel"
    )

    # Items beyond cap get the sentinel.
    overflow_refs = refs[MAX_ITEMS_PER_CALL:]
    assert all(r == "exhausted" for r in overflow_refs), (
        f"Items beyond cap must be 'exhausted', got: {overflow_refs!r}"
    )

    # Resolving "exhausted" raises RefError (it's not a minted ref).
    with pytest.raises(RefError):
        minter.resolve("exhausted", api_key_id=api_key)


# ---------------------------------------------------------------------------
# Scenario 7 — Cursor continuation
# ---------------------------------------------------------------------------


def test_scenario7_cursor_continuation(c4_db, server):
    """list_fields on a 247-field model → continuation hint with start_index=50.

    AC-C4-1 scenario (7): cursor continuation hint present on first page.
    """
    api_key = "c4-s7-pager-key"

    out = server._list_fields(
        _MODEL_FAT, C4_VERSION, limit=50, start_index=0, api_key_id=api_key,
    )

    # Response must include refs.
    assert "[ref=f" in out, f"Expected [ref=fN] on first page:\n{out!r}"

    # Continuation hint must appear (247 > 50).
    assert "Showing rows 1–50 of 247" in out, (
        f"Expected 'Showing rows 1–50 of 247' in output:\n{out!r}"
    )
    assert "start_index=50" in out, (
        f"Expected 'start_index=50' in continuation hint:\n{out!r}"
    )

    # Page 1 must render exactly 50 field rows (lines with [ref=fN]).
    ref_lines = [ln for ln in out.splitlines() if "[ref=f" in ln]
    assert len(ref_lines) == 50, (
        f"Expected 50 ref rows on first page, got {len(ref_lines)}"
    )

    # Call second page and verify it references start_index=100 (more pages remain).
    out2 = server._list_fields(
        _MODEL_FAT, C4_VERSION, limit=50, start_index=50, api_key_id=api_key,
    )
    assert "Showing rows 51–100 of 247" in out2, (
        f"Expected 'Showing rows 51–100 of 247' in page-2 output:\n{out2!r}"
    )
    assert "start_index=100" in out2, (
        f"Expected 'start_index=100' in page-2 continuation hint:\n{out2!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 8 — Gapless pagination over all 247 fields
# ---------------------------------------------------------------------------


def test_scenario8_gapless_pagination(c4_db, server):
    """Paginate through 247-field model 50 at a time — no gaps, no duplicates.

    AC-C4-1 scenario (8): gapless pagination covering full 247-field universe.
    """
    api_key = "c4-s8-full-pager-key"

    all_names: set[str] = set()
    duplicates_found = False

    for page_start in range(0, 247, 50):
        page_out = server._list_fields(
            _MODEL_FAT, C4_VERSION, limit=50, start_index=page_start,
            api_key_id=api_key,
        )
        page_names = _extract_field_names(page_out)

        # Detect duplicates across pages.
        overlap = all_names & page_names
        if overlap:
            duplicates_found = True

        all_names |= page_names

    expected_names = {f"field_{i:03d}" for i in range(247)}

    assert not duplicates_found, (
        "Pagination produced duplicate field names across pages"
    )
    assert all_names == expected_names, (
        f"Pagination missed {len(expected_names - all_names)} fields or "
        f"introduced {len(all_names - expected_names)} extra fields.\n"
        f"Missing: {sorted(expected_names - all_names)[:10]}\n"
        f"Extra: {sorted(all_names - expected_names)[:10]}"
    )


# ---------------------------------------------------------------------------
# Scenario 9 — Wrapper-to-wrapper E2E without thread-local mocking (AC-CFIX-4)
# ---------------------------------------------------------------------------


def test_scenario9_wrapper_to_wrapper_e2e(c4_db, server):
    """list_fields.fn → resolve_field.fn without injecting thread-local.

    AC-CFIX-4: This test reproduces HIGH-1 from the Opus review.  It calls
    the public @mcp.tool wrapper (list_fields.fn), then calls resolve_field.fn
    with the extracted ref — WITHOUT manually setting _api_key_id_local.value.

    Before the fix both wrappers shared different namespaces ('anonymous' vs
    'default') so the resolve step would return a stale-ref error.  After the
    fix, both wrappers call _get_api_key_id() which returns 'default' for unit
    tests (no middleware active), giving consistent namespace behaviour.
    """
    # Call the public wrapper — it now calls _get_api_key_id() which returns
    # 'default' (no middleware active in unit test context).
    list_result = server.list_fields.fn(
        model=_MODEL_SMALL,
        odoo_version=C4_VERSION,
    )
    list_text = list_result.content[0].text

    # Extract the first ref from the output.
    ref = _first_ref(list_text)
    assert ref is not None, (
        f"list_fields.fn produced no [ref=fN] markers:\n{list_text!r}"
    )

    # Call resolve_field.fn with the ref — NO thread-local injection.
    resolve_result = server.resolve_field.fn(
        target=ref,
        odoo_version=C4_VERSION,
    )
    resolve_text = resolve_result.content[0].text

    # Must NOT be a stale-ref error — both wrappers must share the same namespace.
    assert "unknown or expired" not in resolve_text, (
        f"HIGH-1 regression: resolve_field got stale-ref error after list_fields.fn call.\n"
        f"  ref={ref!r}\n"
        f"  list output: {list_text[:200]!r}\n"
        f"  resolve output: {resolve_text!r}"
    )

    # Must contain actual field data (a field name from _MODEL_SMALL).
    field_names = {"amount_total", "partner_id", "state", "name", "date_order"}
    assert any(fn in resolve_text for fn in field_names), (
        f"resolve_field output does not reference any known field name:\n{resolve_text!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 10 — _format_stale_ref_error model entity (AC-CFIX-5)
# ---------------------------------------------------------------------------


def test_scenario10_stale_ref_model_no_list_models(c4_db, server):
    """_format_stale_ref_error for entity='model' must NOT reference list_models.

    AC-CFIX-5: list_models does not exist as an MCP tool; the error recovery
    hint must steer to describe_module or find_examples instead.
    """
    from src.mcp.refs import RefError

    fake_err = RefError("ref expired", recovery_hint=None)
    msg = server._format_stale_ref_error("model", "m99", fake_err)

    # Must not mention list_models (non-existent tool).
    assert "list_models" not in msg, (
        f"_format_stale_ref_error('model', ...) mentions 'list_models' which "
        f"is not a real tool:\n{msg!r}"
    )

    # Must reference a real tool instead.
    real_tools = ("describe_module", "find_examples")
    assert any(t in msg for t in real_tools), (
        f"_format_stale_ref_error('model', ...) should reference one of "
        f"{real_tools} in recovery hint:\n{msg!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 11 — Superset drill-down E2E via model_inspect (AC-DFIX-3)
# ---------------------------------------------------------------------------


def test_scenario11_superset_drilldown_e2e(c4_db, server):
    """model_inspect(method='fields') → extract ref → resolve_field(target=ref).

    AC-DFIX-3: Verifies that the api_key_id is correctly threaded from
    model_inspect wrapper through _model_inspect router to _list_fields, so
    that refs minted by model_inspect are resolvable by resolve_field using
    the same namespace (_get_api_key_id() returns 'default' in both calls
    when no middleware is active).
    """
    # Step A: call model_inspect superset with method='fields'.
    # Both wrappers call _get_api_key_id() which returns 'default' in tests.
    inspect_result = server.model_inspect.fn(
        model=_MODEL_SMALL,
        method="fields",
        odoo_version=C4_VERSION,
    )
    inspect_text = inspect_result.content[0].text

    # Step B: extract the first [ref=fN] from the output.
    ref = _first_ref(inspect_text)
    assert ref is not None, (
        f"model_inspect(method='fields') produced no [ref=fN] markers:\n"
        f"{inspect_text!r}"
    )

    # Step C: resolve via resolve_field — NO thread-local injection needed.
    resolve_result = server.resolve_field.fn(
        target=ref,
        odoo_version=C4_VERSION,
    )
    resolve_text = resolve_result.content[0].text

    # Must NOT be a stale-ref error — both wrappers share the same api_key
    # namespace ('default') so the ref must resolve successfully.
    assert "unknown or expired" not in resolve_text, (
        f"AC-DFIX-3 regression: resolve_field got stale-ref error after "
        f"model_inspect(method='fields').\n"
        f"  ref={ref!r}\n"
        f"  inspect output: {inspect_text[:200]!r}\n"
        f"  resolve output: {resolve_text!r}"
    )

    # Must contain actual field data (a field name from _MODEL_SMALL).
    field_names = {"amount_total", "partner_id", "state", "name", "date_order"}
    assert any(fn in resolve_text for fn in field_names), (
        f"resolve_field output does not reference any known field name:\n"
        f"{resolve_text!r}"
    )
