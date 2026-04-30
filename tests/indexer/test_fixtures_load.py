"""Fixture health tests.

Verify that the odoo_ce_subset + custom_addons fixture corpus loads correctly
through the manifest scanner, load-order simulator, and Python parser without
any errors. These tests do NOT assert semantic correctness (the golden-file
tests cover that) — they assert *structural* health: right number of modules,
no cycles, no parse crashes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osm.indexer.load_order import compute_load_order
from osm.indexer.manifest import scan_addon_root, scan_addon_roots
from osm.indexer.python_parser import parse_file

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent.parent / "fixtures"
CE_SUBSET = FIXTURES / "odoo_ce_subset"
CUSTOM_ADDONS = FIXTURES / "custom_addons"
GOLDEN_DIR = FIXTURES / "golden"

_CE_EXPECTED = {
    "account",
    "base",
    "bus",
    "contacts",
    "mail",
    "product",
    "sale",
    "sale_management",
    "stock",
    "web",
}

# Python-parser fixtures (10 modules).
_CUSTOM_EXPECTED_PYTHON = {
    "viin_fixture_conditional_optional_dep",
    "viin_fixture_depends_added",
    "viin_fixture_field_override_compute",
    "viin_fixture_field_override_no_compute",
    "viin_fixture_inherits_delegation",
    "viin_fixture_method_override_break_super",
    "viin_fixture_method_override_super",
    "viin_fixture_multi_inherit",
    "viin_fixture_order_override",
    "viin_fixture_register_false",
}

# View-parser fixtures (8 modules).
_CUSTOM_EXPECTED_VIEWS = {
    "cv_attributes_op",
    "cv_basic_form",
    "cv_multi_ext_same_target",
    "cv_priority_tie",
    "cv_replace_and_sibling",
    "cv_replace_orphan",
    "cv_simple_ext",
    "cv_xpath_no_match",
}

_CUSTOM_EXPECTED = _CUSTOM_EXPECTED_PYTHON | _CUSTOM_EXPECTED_VIEWS

# ---------------------------------------------------------------------------
# Scanner tests
# ---------------------------------------------------------------------------


def test_ce_subset_scan_count() -> None:
    """CE subset scanner finds exactly 10 modules."""
    records = scan_addon_root(CE_SUBSET)
    names = {r.name for r in records}
    assert names == _CE_EXPECTED, f"Mismatch: {names ^ _CE_EXPECTED}"


def test_custom_addons_scan_count() -> None:
    """Custom addons scanner finds 10 Python fixtures + 8 view fixtures = 18."""
    records = scan_addon_root(CUSTOM_ADDONS)
    names = {r.name for r in records}
    assert names == _CUSTOM_EXPECTED, f"Mismatch: {names ^ _CUSTOM_EXPECTED}"


def test_combined_scan_count() -> None:
    """scan_addon_roots on both roots yields 10 CE + 18 custom = 28 modules."""
    records = scan_addon_roots([CE_SUBSET, CUSTOM_ADDONS])
    assert len(records) == 28


# ---------------------------------------------------------------------------
# Load-order tests
# ---------------------------------------------------------------------------


def test_compute_load_order_no_cycle() -> None:
    """Load-order simulation does not raise CyclicDependencyError."""
    manifests = scan_addon_roots([CE_SUBSET, CUSTOM_ADDONS])
    result = compute_load_order(manifests)
    assert len(result) == 28


def test_all_20_modules_in_load_order() -> None:
    """Every scanned module appears in the load-order result."""
    manifests = scan_addon_roots([CE_SUBSET, CUSTOM_ADDONS])
    result = compute_load_order(manifests)
    names = {r.name for r in result}
    expected = _CE_EXPECTED | _CUSTOM_EXPECTED
    assert names == expected


def test_base_loads_before_mail() -> None:
    """base must load before mail (dep chain: base -> web -> bus -> mail)."""
    manifests = scan_addon_roots([CE_SUBSET, CUSTOM_ADDONS])
    result = compute_load_order(manifests)
    by_name = {r.name: r.load_order for r in result}
    assert by_name["base"] < by_name["mail"]


def test_sale_loads_before_sale_management() -> None:
    """sale loads before sale_management."""
    manifests = scan_addon_roots([CE_SUBSET, CUSTOM_ADDONS])
    result = compute_load_order(manifests)
    by_name = {r.name: r.load_order for r in result}
    assert by_name["sale"] < by_name["sale_management"]


# ---------------------------------------------------------------------------
# Parser health tests
# ---------------------------------------------------------------------------


def _all_model_py_files() -> list[Path]:
    """Collect all *.py files under models/ in both fixture roots."""
    files: list[Path] = []
    for root in (CE_SUBSET, CUSTOM_ADDONS):
        files.extend(sorted(root.rglob("models/*.py")))
    return files


@pytest.mark.parametrize(
    "py_file", _all_model_py_files(), ids=lambda p: str(p.relative_to(FIXTURES))
)
def test_parse_file_no_crash(py_file: Path) -> None:
    """parse_file must not raise on any fixture model file."""
    result = parse_file(py_file)
    # Result can have zero models (init files, non-model files) — no crash is the assertion.
    assert result is not None


# ---------------------------------------------------------------------------
# Spec §5c edge-case presence tests
# ---------------------------------------------------------------------------


def test_conditional_import_fixture_present() -> None:
    """viin_fixture_conditional_optional_dep models/__init__.py has try/except ImportError."""
    init_path = CUSTOM_ADDONS / "viin_fixture_conditional_optional_dep" / "models" / "__init__.py"
    source = init_path.read_text()
    assert "try:" in source
    assert "ImportError" in source


def test_register_false_fixture_present() -> None:
    """viin_fixture_register_false has a model with _register = False."""
    model_path = CUSTOM_ADDONS / "viin_fixture_register_false" / "models" / "abstract_base.py"
    result = parse_file(model_path)
    register_false_models = [m for m in result.models if m.register_false]
    assert len(register_false_models) == 1, "Expected exactly 1 _register=False model"


def test_delegation_fixture_present() -> None:
    """viin_fixture_inherits_delegation has _inherits delegation."""
    model_path = CUSTOM_ADDONS / "viin_fixture_inherits_delegation" / "models" / "custom_product.py"
    result = parse_file(model_path)
    delegation_models = [m for m in result.models if m.inherits]
    assert len(delegation_models) == 1
    assert "product.template" in delegation_models[0].inherits


# ---------------------------------------------------------------------------
# Golden file structure tests
# ---------------------------------------------------------------------------


def test_golden_resolve_model_count() -> None:
    """resolve_model.json has exactly 10 entries."""
    data = json.loads((GOLDEN_DIR / "resolve_model.json").read_text())
    assert len(data) == 10


def test_golden_resolve_field_count() -> None:
    """resolve_field.json has 50 entries (10 full + 40 TODO)."""
    data = json.loads((GOLDEN_DIR / "resolve_field.json").read_text())
    assert len(data) == 50


def test_golden_resolve_method_count() -> None:
    """resolve_method.json has 20 entries (5 full + 15 TODO)."""
    data = json.loads((GOLDEN_DIR / "resolve_method.json").read_text())
    assert len(data) == 20


def test_golden_resolve_model_all_have_required_keys() -> None:
    """Every fully-labelled resolve_model entry has chain, model_name, warnings."""
    data = json.loads((GOLDEN_DIR / "resolve_model.json").read_text())
    for entry in data:
        if "TODO" in entry:
            pytest.skip("TODO entry — labelling pending")
        assert "model_name" in entry, f"Missing model_name in {entry}"
        assert "chain" in entry, f"Missing chain in {entry}"
        assert "warnings" in entry, f"Missing warnings in {entry}"


def test_golden_resolve_field_fully_labelled_entries() -> None:
    """10 fully-labelled field entries have required keys; TODO entries are skipped."""
    data = json.loads((GOLDEN_DIR / "resolve_field.json").read_text())
    full_count = 0
    for entry in data:
        if "TODO" in entry:
            continue
        assert "model_name" in entry
        assert "field_name" in entry
        assert "chain" in entry
        full_count += 1
    assert full_count == 10, f"Expected 10 full entries, got {full_count}"


def test_golden_resolve_method_fully_labelled_entries() -> None:
    """5 fully-labelled method entries have required keys; TODO entries are skipped."""
    data = json.loads((GOLDEN_DIR / "resolve_method.json").read_text())
    full_count = 0
    for entry in data:
        if "TODO" in entry:
            continue
        assert "model_name" in entry
        assert "method_name" in entry
        assert "chain" in entry
        assert "chain_is_broken" in entry
        full_count += 1
    assert full_count == 5, f"Expected 5 full entries, got {full_count}"


def test_golden_spec_5c_conditional_import_present() -> None:
    """resolve_field.json has at least 1 entry with resolution: conditional warning."""
    data = json.loads((GOLDEN_DIR / "resolve_field.json").read_text())
    conditional = [
        e for e in data
        if "TODO" not in e
        and any("conditional" in str(w) for w in e.get("warnings", []))
    ]
    assert len(conditional) >= 1, (
        "Missing spec §5c case 1 (conditional_import) in resolve_field.json"
    )


def test_golden_spec_5c_register_false_present() -> None:
    """resolve_field.json has at least 1 entry with _register=False warning."""
    data = json.loads((GOLDEN_DIR / "resolve_field.json").read_text())
    reg_false = [
        e for e in data
        if "TODO" not in e
        and any("_register" in str(w) or "register" in str(w) for w in e.get("warnings", []))
    ]
    assert len(reg_false) >= 1, "Missing spec §5c case 2 (_register=False) in resolve_field.json"
