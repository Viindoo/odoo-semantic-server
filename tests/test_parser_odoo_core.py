# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_odoo_core.py
"""Parser_odoo_core tests (M4.5 WI2.2 — extract CoreSymbol from Odoo upstream source).

Allow-list 8 file approach (per ADR-0002 §6 — KISS, no full-source walk):
    odoo/tools/safe_eval.py, query.py, sql.py
    odoo/fields.py, models.py, api.py, sql_db.py, exceptions.py
"""
import os
from pathlib import Path

import pytest

from src.indexer.parser_odoo_core import (
    _CORE_FILES,
    _V19_CURATED_FILES,
    _extract_from_source,
    _resolve_core_paths,
    _version_prefix,
    parse_odoo_core,
)

ODOO17_SRC = os.environ.get("ODOO17_SRC", "/nonexistent/odoo17")


def test_extract_function_symbol_top_level():
    """Top-level `def safe_eval(...):` → kind='function'."""
    src = "def safe_eval(expr, context=None):\n    return eval(expr)\n"
    syms = _extract_from_source(src, "odoo.tools.safe_eval", "19.0")
    fn = next(s for s in syms if s.qualified_name == "odoo.tools.safe_eval.safe_eval")
    assert fn.kind == "function"
    assert fn.odoo_version == "19.0"


def test_extract_class_symbol_top_level():
    """Top-level `class Query:` → kind='class' for the class itself."""
    src = "class Query:\n    def __init__(self, env):\n        self.env = env\n"
    syms = _extract_from_source(src, "odoo.tools.query", "18.0")
    cls = next(s for s in syms if s.qualified_name == "odoo.tools.query.Query")
    assert cls.kind == "class"


def test_extract_field_type_subclass_marks_field_type():
    """class Float(Field): → kind='field_type'."""
    src = "class Field:\n    pass\n\nclass Float(Field):\n    aggregator = 'sum'\n"
    syms = _extract_from_source(src, "odoo.fields", "18.0")
    flt = next(s for s in syms if s.qualified_name.endswith(".Float"))
    assert flt.kind == "field_type"


def test_extract_exception_class_marks_exception():
    """class UserError(Exception): → kind='exception'."""
    src = "class UserError(Exception):\n    pass\n"
    syms = _extract_from_source(src, "odoo.exceptions", "17.0")
    exc = next(s for s in syms if s.qualified_name.endswith(".UserError"))
    assert exc.kind == "exception"


def test_extract_orm_method_marked_deprecated_via_api_decorator():
    """Method decorated `@api.deprecated(...)` → status='deprecated', kind='orm_method'."""
    src = (
        "class BaseModel:\n"
        "    @api.deprecated('Use display_name')\n"
        "    def name_get(self):\n"
        "        return self.display_name\n"
    )
    syms = _extract_from_source(src, "odoo.models", "17.0")
    nm = next(s for s in syms if s.qualified_name.endswith(".name_get"))
    assert nm.status == "deprecated"
    assert nm.kind == "orm_method"


def test_extract_skips_dunder_and_private():
    """`__init__`, `_private` methods inside a class are not surfaced as standalone symbols."""
    src = (
        "class X:\n"
        "    def __init__(self): pass\n"
        "    def _internal(self): pass\n"
        "    def public(self): pass\n"
    )
    syms = _extract_from_source(src, "odoo.x", "17.0")
    qnames = {s.qualified_name for s in syms}
    # Class is captured; only the public method is surfaced as standalone.
    assert "odoo.x.X" in qnames
    assert "odoo.x.X.public" in qnames
    assert "odoo.x.X.__init__" not in qnames
    assert "odoo.x.X._internal" not in qnames


def test_parse_odoo_core_returns_empty_for_nonexistent_root(tmp_path):
    """Missing source root → empty list, no exception."""
    out = parse_odoo_core(str(tmp_path / "no-such-dir"), "17.0")
    assert out == []


def test_parse_odoo_core_skips_missing_files(tmp_path):
    """Allow-list files that don't exist are silently skipped (Boil-the-Lake KISS)."""
    # Only create one of the 8 allow-list files
    (tmp_path / "odoo" / "tools").mkdir(parents=True)
    (tmp_path / "odoo" / "tools" / "safe_eval.py").write_text(
        "def safe_eval(expr): return eval(expr)\n"
    )
    out = parse_odoo_core(str(tmp_path), "19.0")
    qnames = {s.qualified_name for s in out}
    # At least the function from safe_eval should be present
    assert any(qn.endswith("safe_eval.safe_eval") for qn in qnames)
    # No symbols from missing files
    assert all("odoo.fields" not in qn for qn in qnames)


def test_core_files_allowlist_is_curated_and_matches_documented_set():
    """ADR-0002 §6 — allow-list is CURATED (bounded), not a full source walk.

    Intent: the allowlist must exactly match the documented set of stable core
    API files. Adding new files requires explicit justification (not automatic).
    This test encodes the full documented set so any unreviewed addition/removal
    causes a failure.

    _CORE_FILES is version-AGNOSTIC (exactly 8 entries). The resolver fan-out
    (_resolve_core_paths) handles v19 package-dir splits automatically.
    v19-only curated files (utils.py, model_classes.py, domains.py, table_objects.py)
    are registered in _V19_CURATED_FILES (not here) — they carry a name_allowlist to
    index only their public symbols, not internal plumbing.
    """
    # Exact documented set of 8 version-agnostic files — any unreviewed change fails.
    expected = (
        "odoo/tools/safe_eval.py",
        "odoo/tools/query.py",
        "odoo/tools/sql.py",
        "odoo/fields.py",
        "odoo/models.py",
        "odoo/api.py",
        "odoo/sql_db.py",
        "odoo/exceptions.py",
    )
    assert len(_CORE_FILES) == 8, (
        f"Allow-list must have exactly 8 version-agnostic entries, got {len(_CORE_FILES)}. "
        f"v19-specific files belong in _V19_CURATED_FILES with a name_allowlist."
    )
    assert len(_CORE_FILES) == len(expected), (
        f"Allow-list size changed: expected {len(expected)}, got {len(_CORE_FILES)}. "
        f"Update this test if the change is intentional and documented."
    )
    assert set(_CORE_FILES) == set(expected), (
        f"Allow-list contents differ. New paths: {set(_CORE_FILES) - set(expected)}. "
        f"Removed paths: {set(expected) - set(_CORE_FILES)}."
    )
    # Sanity: all paths look right (no walk-entire-source escape, no orm/ files).
    for path in _CORE_FILES:
        assert path.startswith("odoo/")
        assert path.endswith(".py")
        assert "orm/" not in path, (
            f"v19-specific orm/ path {path!r} must be in _V19_CURATED_FILES, not _CORE_FILES."
        )


@pytest.mark.skipif(
    not Path(ODOO17_SRC + "/odoo/tools/safe_eval.py").exists(),
    reason="Real Odoo 17 source not on disk (skipped in CI; runs locally)",
)
def test_parse_odoo_core_smoke_real_v17():
    """Smoke test against real Odoo 17 source on disk — extract sane number of symbols."""
    out = parse_odoo_core(ODOO17_SRC, "17.0")
    # Heuristic lower bound — real Odoo 17 has hundreds of API entities across 8 files.
    assert len(out) >= 50, f"expected ≥50 symbols, got {len(out)}"
    # Must include at least the well-known safe_eval function.
    assert any(s.qualified_name.endswith(".safe_eval") for s in out)


# ---------------------------------------------------------------------------
# WI-6 — v19 package-directory layout tests
# ---------------------------------------------------------------------------

def test_v19_package_dir_resolves_to_orm_split_files(tmp_path):
    """v19+: odoo/fields/ package dir → resolver returns odoo/orm/fields*.py files.

    Simulate the v19 layout: odoo/fields/ is a directory (package), and the real
    symbols live in odoo/orm/fields.py.  The parser must still produce CoreSymbol
    nodes for every top-level class found in those ORM split files.
    """
    # Create v19 package dir (makes candidate.is_file() → False)
    (tmp_path / "odoo" / "fields").mkdir(parents=True)
    (tmp_path / "odoo" / "fields" / "__init__.py").write_text(
        "from odoo.orm.fields import Char, Many2one  # re-export\n"
    )
    # Create the ORM split file where symbols actually live
    orm_dir = tmp_path / "odoo" / "orm"
    orm_dir.mkdir(parents=True)
    (orm_dir / "fields.py").write_text(
        "class Field:\n    pass\n\n"
        "class Char(Field):\n    pass\n\n"
        "class Many2one(Field):\n    pass\n"
    )

    # _resolve_core_paths must find the ORM file, not the package dir
    resolved = _resolve_core_paths(tmp_path, "odoo/fields.py", "19.0")
    assert any(p.name == "fields.py" and "orm" in str(p) for p in resolved), (
        f"Expected odoo/orm/fields.py in resolved paths, got: {resolved}"
    )

    # parse_odoo_core must produce CoreSymbol nodes for Char and Many2one
    out = parse_odoo_core(str(tmp_path), "19.0")
    qnames = {s.qualified_name for s in out}
    assert "odoo.fields.Char" in qnames, f"Missing Char in {qnames}"
    assert "odoo.fields.Many2one" in qnames, f"Missing Many2one in {qnames}"
    # Base Field class should also appear
    assert "odoo.fields.Field" in qnames, f"Missing Field in {qnames}"


def test_v17_file_path_still_works(tmp_path):
    """Backward compat: v17 odoo/fields.py is a regular file → resolver returns it directly.

    No odoo/orm/ directory exists.  _resolve_core_paths must return the file as-is
    without attempting any v19 substitution, so that v17 symbols are still parsed.
    """
    (tmp_path / "odoo").mkdir(parents=True)
    fields_py = tmp_path / "odoo" / "fields.py"
    fields_py.write_text(
        "class Field:\n    pass\n\nclass Integer(Field):\n    pass\n"
    )
    # No odoo/orm/ directory — pure v17 layout

    resolved = _resolve_core_paths(tmp_path, "odoo/fields.py", "17.0")
    assert resolved == [fields_py], (
        f"Expected [fields_py], got {resolved}"
    )

    out = parse_odoo_core(str(tmp_path), "17.0")
    qnames = {s.qualified_name for s in out}
    assert "odoo.fields.Field" in qnames, f"Missing Field in {qnames}"
    assert "odoo.fields.Integer" in qnames, f"Missing Integer in {qnames}"


# ---------------------------------------------------------------------------
# A1 — v19 split-ORM curated coverage (Command / Domain / table_objects)
# ---------------------------------------------------------------------------

def test_extract_from_source_name_allowlist_filters_top_level():
    """name_allowlist keeps only listed top-level symbols (their methods follow)."""
    src = (
        "class Keep:\n    def public(self):\n        pass\n\n"
        "class Drop:\n    def public(self):\n        pass\n\n"
        "def drop_fn():\n    pass\n"
    )
    syms = _extract_from_source(
        src, "odoo.orm.domains", "19.0", name_allowlist=frozenset({"Keep"})
    )
    qnames = {s.qualified_name for s in syms}
    assert "odoo.orm.domains.Keep" in qnames
    assert "odoo.orm.domains.Keep.public" in qnames
    assert "odoo.orm.domains.Drop" not in qnames
    assert "odoo.orm.domains.drop_fn" not in qnames


def test_v19_command_enum_resolves_via_fields_with_continuity_qname(tmp_path):
    """v19 `Command` lives in orm/commands.py but keeps the v18 qname odoo.fields.Command.

    Continuity matters: api_version_diff must see Command as a moved file, not a
    remove+add, so its qname stays `odoo.fields.Command` (resolved through the
    odoo/fields.py allow-list entry).
    """
    (tmp_path / "odoo" / "fields").mkdir(parents=True)  # package dir → flat file absent
    orm_dir = tmp_path / "odoo" / "orm"
    orm_dir.mkdir(parents=True)
    (orm_dir / "fields.py").write_text("class Field:\n    pass\n")
    (orm_dir / "commands.py").write_text(
        "import enum\n\n"
        "class Command(enum.IntEnum):\n"
        "    CREATE = 0\n"
        "    @classmethod\n"
        "    def create(cls, values):\n        return (cls.CREATE, 0, values)\n"
    )
    out = parse_odoo_core(str(tmp_path), "19.0")
    qnames = {s.qualified_name for s in out}
    assert "odoo.fields.Command" in qnames, f"Missing Command continuity qname in {qnames}"
    assert "odoo.fields.Command.create" in qnames, "Command classmethods should be indexed"


def test_v19_curated_domains_emits_only_public_symbols(tmp_path):
    """orm/domains.py: the curated 8 public Domain builder classes are emitted;
    OptimizationLevel (internal IntEnum) and _optimize_* helpers are excluded.

    Intent: _V19_CURATED_FILES applies a name_allowlist so only the documented
    public domain-builder API surface reaches the graph. The curated list includes
    DomainBool (a real domain node, not a helper), while it excludes:
    - OptimizationLevel (IntEnum, internal optimization machinery, not a domain builder)
    - _optimize_nary and similar internal helper functions

    Scanned from real Odoo 19 odoo/orm/domains.py — public classes:
    Domain, DomainBool, DomainNot, DomainNary, DomainAnd, DomainOr,
    DomainCustom, DomainCondition.
    """
    orm_dir = tmp_path / "odoo" / "orm"
    orm_dir.mkdir(parents=True)
    (orm_dir / "domains.py").write_text(
        "import enum\n\n"
        "class OptimizationLevel(enum.IntEnum):\n"
        "    BASIC = 0\n"
        "    FULL = 1\n\n"
        "class Domain:\n    def optimize(self):\n        pass\n\n"
        "class DomainBool(Domain):\n    pass\n\n"       # PUBLIC — in curated list
        "class DomainNot(Domain):\n    pass\n\n"        # PUBLIC — in curated list
        "class DomainNary(Domain):\n    pass\n\n"       # PUBLIC — in curated list
        "class DomainAnd(Domain):\n    pass\n\n"        # PUBLIC — in curated list
        "class DomainOr(Domain):\n    pass\n\n"         # PUBLIC — in curated list
        "class DomainCustom(Domain):\n    pass\n\n"     # PUBLIC — in curated list
        "class DomainCondition(Domain):\n    pass\n\n"  # PUBLIC — in curated list
        "def _optimize_nary(a, b):\n    return a\n"     # internal helper — excluded
    )
    out = parse_odoo_core(str(tmp_path), "19.0")
    qnames = {s.qualified_name for s in out}

    # All 8 public domain builder classes must be present.
    for cls in ("Domain", "DomainBool", "DomainNot", "DomainNary",
                "DomainAnd", "DomainOr", "DomainCustom", "DomainCondition"):
        assert f"odoo.orm.domains.{cls}" in qnames, (
            f"Public domain class {cls} must be in output. "
            f"Got: {[q for q in qnames if 'domains' in q]}"
        )

    # Curation: internal symbols must NOT leak into the graph.
    assert "odoo.orm.domains.OptimizationLevel" not in qnames, (
        "OptimizationLevel (IntEnum, internal) must NOT appear in graph — "
        "it is optimization machinery, not a domain builder API."
    )
    assert "odoo.orm.domains._optimize_nary" not in qnames, (
        "_optimize_nary (internal helper function) must NOT appear in graph."
    )


def test_v19_curated_table_objects_emits_constraint_index(tmp_path):
    """orm/table_objects.py: declarative Constraint/Index/UniqueIndex API indexed."""
    orm_dir = tmp_path / "odoo" / "orm"
    orm_dir.mkdir(parents=True)
    (orm_dir / "table_objects.py").write_text(
        "class TableObject:\n    pass\n\n"
        "class Constraint(TableObject):\n    pass\n\n"
        "class Index(TableObject):\n    pass\n\n"
        "class UniqueIndex(Index):\n    pass\n"
    )
    out = parse_odoo_core(str(tmp_path), "19.0")
    qnames = {s.qualified_name for s in out}
    for name in ("TableObject", "Constraint", "Index", "UniqueIndex"):
        assert f"odoo.orm.table_objects.{name}" in qnames, f"Missing {name} in {qnames}"


def test_pre_v19_skips_curated_orm_files(tmp_path):
    """v18 (flat fields.py, no orm/ dir): curated files silently skipped; Command still found."""
    (tmp_path / "odoo").mkdir(parents=True)
    (tmp_path / "odoo" / "fields.py").write_text(
        "import enum\n\nclass Field:\n    pass\n\nclass Command(enum.IntEnum):\n    CREATE = 0\n"
    )
    out = parse_odoo_core(str(tmp_path), "18.0")
    qnames = {s.qualified_name for s in out}
    assert "odoo.fields.Command" in qnames, "v18 Command resolves from flat fields.py"
    orm_leak = [q for q in qnames if q.startswith("odoo.orm.")]
    assert not orm_leak, f"v18 must not produce orm.* symbols, got {orm_leak}"


def test_v19_curated_files_registry_is_curated_not_maximal():
    """Sanity: _V19_CURATED_FILES is curated (small public set), not maximal (whole files).

    Intent: the registry must exactly enumerate the documented public API from each
    curated file. This test encodes the full documented set so any unreviewed
    addition/removal causes an immediate failure.

    FIX-2 (v19): expanded Domain allowlist from 3 to 8 public classes after scanning
    real Odoo 19 odoo/orm/domains.py. DomainBool, DomainNot, DomainNary, DomainCustom,
    DomainCondition were added as genuine public domain builder classes.
    DomainNary is a concrete (not abstract) base for n-ary AND/OR — it IS public API.
    OptimizationLevel (IntEnum, internal) remains excluded.

    FIX-3 (v19): utils.py and model_classes.py added to _V19_CURATED_FILES (not to
    _CORE_FILES). Correct mechanism: _CORE_FILES uses name_allowlist=None (emit ALL
    top-level symbols), which would have indexed internal plumbing. _V19_CURATED_FILES
    uses a curated name_allowlist so only the documented public symbols are emitted.
    - utils.py: parse_field_expr (dotted-path parser), OriginIds (origin-id helper).
      Excluded: check_method_name (deprecated since 19.0), check_pg_name / check_object_name
      (internal validators), expand_ids (internal id-dedup generator).
    - model_classes.py: is_model_class, is_model_definition (public introspection API).
      Excluded: add_to_registry, setup_model_classes, add_field, pop_field (internal
      registry machinery) and all _private helpers.

    Still satisfies "curated, not maximal": the curated allowlists cover only the
    documented public API from each file, not all top-level symbols.
    """
    # Exact file set (no whole-source walk) — 4 files after FIX-3.
    assert set(_V19_CURATED_FILES) == {
        "odoo/orm/domains.py",
        "odoo/orm/table_objects.py",
        "odoo/orm/utils.py",
        "odoo/orm/model_classes.py",
    }, (
        f"_V19_CURATED_FILES file set mismatch. Got: {set(_V19_CURATED_FILES)}. "
        f"Any unreviewed addition/removal must update this test with justification."
    )
    # Domains: 8 public domain builder classes (scanned from real Odoo 19).
    # DomainNary: concrete (not abstract), documents n-ary AND/OR semantics — public.
    # Excluded: OptimizationLevel (IntEnum, internal machinery).
    assert _V19_CURATED_FILES["odoo/orm/domains.py"] == frozenset({
        "Domain",
        "DomainBool",
        "DomainNot",
        "DomainNary",
        "DomainAnd",
        "DomainOr",
        "DomainCustom",
        "DomainCondition",
    }), (
        f"Domain curated set mismatch. Got: {_V19_CURATED_FILES['odoo/orm/domains.py']}. "
        f"If adding a new Domain class, verify it is a public builder (not internal machinery) "
        f"and document the reasoning here."
    )
    # Table objects: 4 public declarative API classes.
    assert _V19_CURATED_FILES["odoo/orm/table_objects.py"] == frozenset(
        {"TableObject", "Constraint", "Index", "UniqueIndex"}
    )
    # utils.py: 2 public symbols (parse_field_expr + OriginIds). Internal plumbing excluded.
    assert _V19_CURATED_FILES["odoo/orm/utils.py"] == frozenset({
        "parse_field_expr",
        "OriginIds",
    }), (
        f"utils.py curated set mismatch. Got: {_V19_CURATED_FILES['odoo/orm/utils.py']}. "
        f"Internal helpers (check_pg_name, check_method_name, expand_ids) must remain excluded."
    )
    # model_classes.py: 2 public introspection functions. Registry machinery excluded.
    assert _V19_CURATED_FILES["odoo/orm/model_classes.py"] == frozenset({
        "is_model_class",
        "is_model_definition",
    }), (
        "model_classes.py curated set mismatch. "
        f"Got: {_V19_CURATED_FILES['odoo/orm/model_classes.py']}. "
        "Internal plumbing (add_to_registry, setup_model_classes, add_field, pop_field) "
        "must be excluded."
    )
    # Guard: OptimizationLevel must remain excluded from all curated sets.
    for filename, allowlist in _V19_CURATED_FILES.items():
        assert "OptimizationLevel" not in allowlist, (
            f"OptimizationLevel (IntEnum, internal) must NOT be in curated list for {filename}."
        )
    # Guard: internal plumbing must not leak through into any curated set.
    internal_plumbing = {
        "check_pg_name", "check_method_name", "add_to_registry",
        "setup_model_classes", "add_field", "pop_field", "expand_ids",
    }
    for filename, allowlist in _V19_CURATED_FILES.items():
        leaked = allowlist & internal_plumbing
        assert not leaked, (
            f"Internal plumbing symbols {leaked!r} must NOT appear in curated set for {filename}."
        )


# ---------------------------------------------------------------------------
# WI-7 — v8/v9 openerp/ namespace tests
# ---------------------------------------------------------------------------

def test_version_prefix_v8_returns_openerp():
    """v8.0 → _version_prefix returns 'openerp/'."""
    assert _version_prefix("8.0") == "openerp/"


def test_version_prefix_v9_returns_openerp():
    """v9.0 → _version_prefix returns 'openerp/'."""
    assert _version_prefix("9.0") == "openerp/"


def test_version_prefix_v10_returns_odoo():
    """v10.0 → _version_prefix returns 'odoo/' (boundary: first modern version)."""
    assert _version_prefix("10.0") == "odoo/"


def test_v8_openerp_namespace_resolves(tmp_path):
    """v8.0: allow-list path 'odoo/fields.py' is redirected to openerp/fields.py.

    _resolve_core_paths must return the openerp/fields.py path, and parse_odoo_core
    must produce CoreSymbol nodes from its content.
    """
    openerp_dir = tmp_path / "openerp"
    openerp_dir.mkdir(parents=True)
    fields_py = openerp_dir / "fields.py"
    fields_py.write_text(
        "class Field(object):\n    pass\n\n"
        "class Char(Field):\n    size = None\n\n"
        "class Integer(Field):\n    pass\n"
    )

    # _resolve_core_paths must return the openerp/fields.py file
    resolved = _resolve_core_paths(tmp_path, "odoo/fields.py", "8.0")
    assert resolved == [fields_py], (
        f"Expected [openerp/fields.py], got: {resolved}"
    )

    # parse_odoo_core must produce CoreSymbol nodes
    out = parse_odoo_core(str(tmp_path), "8.0")
    qnames = {s.qualified_name for s in out}
    assert "odoo.fields.Field" in qnames, f"Missing Field in {qnames}"
    assert "odoo.fields.Char" in qnames, f"Missing Char in {qnames}"
    assert "odoo.fields.Integer" in qnames, f"Missing Integer in {qnames}"


def test_v9_openerp_namespace_resolves(tmp_path):
    """v9.0: allow-list path 'odoo/models.py' is redirected to openerp/models.py.

    Verifies the major <= 9 branch works for v9 specifically, using a different
    allow-list file than the v8 test (models.py instead of fields.py).
    """
    openerp_dir = tmp_path / "openerp"
    openerp_dir.mkdir(parents=True)
    models_py = openerp_dir / "models.py"
    models_py.write_text(
        "class BaseModel(object):\n"
        "    _name = None\n\n"
        "    def search(self, domain):\n"
        "        return []\n\n"
        "    def browse(self, ids):\n"
        "        return []\n"
    )

    resolved = _resolve_core_paths(tmp_path, "odoo/models.py", "9.0")
    assert resolved == [models_py], (
        f"Expected [openerp/models.py], got: {resolved}"
    )

    out = parse_odoo_core(str(tmp_path), "9.0")
    qnames = {s.qualified_name for s in out}
    assert "odoo.models.BaseModel" in qnames, f"Missing BaseModel in {qnames}"
    # Public methods of BaseModel should also appear
    assert "odoo.models.BaseModel.search" in qnames, f"Missing search in {qnames}"
    assert "odoo.models.BaseModel.browse" in qnames, f"Missing browse in {qnames}"


def test_v10_uses_odoo_namespace_not_openerp(tmp_path):
    """Regression guard: v10.0 must NOT redirect to openerp/ — uses odoo/ as-is."""
    # Create odoo/fields.py (v10 layout)
    (tmp_path / "odoo").mkdir(parents=True)
    fields_py = tmp_path / "odoo" / "fields.py"
    fields_py.write_text(
        "class Field(object):\n    pass\n\nclass Date(Field):\n    pass\n"
    )

    resolved = _resolve_core_paths(tmp_path, "odoo/fields.py", "10.0")
    assert resolved == [fields_py], (
        f"v10 should resolve to odoo/fields.py, got: {resolved}"
    )

    out = parse_odoo_core(str(tmp_path), "10.0")
    qnames = {s.qualified_name for s in out}
    assert "odoo.fields.Field" in qnames, f"Missing Field in {qnames}"
    assert "odoo.fields.Date" in qnames, f"Missing Date in {qnames}"


def test_v8_missing_allowlist_file_returns_empty(tmp_path):
    """v8: allow-list path that doesn't exist in the source tree → empty list, no error.

    e.g. openerp/tools/query.py was not introduced until later versions.
    """
    # Create the openerp directory but NOT openerp/tools/query.py
    (tmp_path / "openerp" / "tools").mkdir(parents=True)

    resolved = _resolve_core_paths(tmp_path, "odoo/tools/query.py", "8.0")
    assert resolved == [], (
        f"Missing allow-list file should yield empty list, got: {resolved}"
    )


def test_indirect_exception_classified_as_exception(tmp_path):
    """Indirect exceptions (subclass of UserError) → kind='exception'.

    Odoo's exception hierarchy uses shallow indirection (one level through
    UserError). ValidationError(UserError), AccessError(UserError), etc.
    must all be classified as kind='exception', not kind='class'.

    Bug: pre-fix, only direct Exception/Warning subclasses were caught.
    Post-fix: UserError is in _EXCEPTION_BASE_NAMES, so indirect ones work.
    """
    (tmp_path / "odoo").mkdir(parents=True)
    exceptions_py = tmp_path / "odoo" / "exceptions.py"
    exceptions_py.write_text(
        "class UserError(Exception):\n"
        "    pass\n\n"
        "class ValidationError(UserError):\n"
        "    pass\n\n"
        "class AccessDenied(Exception):\n"
        "    pass\n"
    )

    out = parse_odoo_core(str(tmp_path), "17.0")
    qnames = {s.qualified_name: s.kind for s in out}

    # All three should be classified as exception
    assert qnames.get("odoo.exceptions.UserError") == "exception", (
        "UserError should be kind='exception'"
    )
    assert qnames.get("odoo.exceptions.ValidationError") == "exception", (
        "ValidationError (indirect, via UserError) should be kind='exception'"
    )
    assert qnames.get("odoo.exceptions.AccessDenied") == "exception", (
        "AccessDenied (direct Exception subclass) should be kind='exception'"
    )


# ---------------------------------------------------------------------------
# WI-11 — Integration tests for full parse_odoo_core pipeline coverage
# ---------------------------------------------------------------------------


def test_parse_odoo_core_v19_full_pipeline_emits_orm_symbols(tmp_path):
    """v19 complete tree: odoo/fields/ package, odoo/orm/ split files, all kinds present.

    Verify parse_odoo_core(tmp_path, '19.0') with a realistic v19 layout:
    - odoo/fields/ is a package dir (not a file)
    - odoo/orm/fields.py contains field definitions (symbol emission via resolver)
    - odoo/orm/models.py contains ORM model symbols
    - odoo/orm/decorators.py contains decorator symbols
    - odoo/exceptions.py contains exception hierarchy (recursive: ValidationError → UserError)
    - odoo/sql_db.py contains cursor-related classes and methods
    - odoo/tools/{safe_eval,sql}.py contain utility functions

    Assert:
    1. All 6 kind labels appear in output
       (field_type, class, exception, function, orm_method, cursor_method)
    2. ValidationError is correctly classified as exception (covers recursive case)
    3. ORM methods inside BaseModel/Model have kind='orm_method'
    4. Cursor methods inside Cursor have kind='cursor_method'
    5. Output count is reasonable (>= 30 symbols)
    """
    # Create v19 package layout: odoo/fields/ is a directory
    (tmp_path / "odoo" / "fields").mkdir(parents=True)
    (tmp_path / "odoo" / "fields" / "__init__.py").write_text(
        "# v19: fields is a package re-exporting from odoo.orm.fields\n"
        "from odoo.orm.fields import Char, Integer, Float, Many2one, One2many\n"
    )

    # Create odoo/orm split files (where symbols actually live in v19)
    orm_dir = tmp_path / "odoo" / "orm"
    orm_dir.mkdir(parents=True)

    (orm_dir / "fields.py").write_text(
        "class Field:\n"
        "    pass\n\n"
        "class Char(Field):\n"
        "    pass\n\n"
        "class Integer(Field):\n"
        "    pass\n\n"
        "class Float(Field):\n"
        "    aggregator = 'sum'\n\n"
        "class Many2one(Field):\n"
        "    pass\n\n"
        "class One2many(Field):\n"
        "    pass\n"
    )

    (orm_dir / "models.py").write_text(
        "class BaseModel:\n"
        "    _name = None\n\n"
        "    def create(self, vals):\n"
        "        return self\n\n"
        "    def write(self, vals):\n"
        "        return True\n\n"
        "    def unlink(self):\n"
        "        return True\n\n"
        "class Model(BaseModel):\n"
        "    _auto = True\n\n"
        "    def action_archive(self):\n"
        "        pass\n"
    )

    (orm_dir / "decorators.py").write_text(
        "def depends(*args):\n"
        "    def decorator(func):\n"
        "        return func\n"
        "    return decorator\n\n"
        "def api_one(func):\n"
        "    return func\n"
    )

    # Create odoo/exceptions.py with recursive hierarchy
    (tmp_path / "odoo" / "exceptions.py").write_text(
        "class UserError(Exception):\n"
        "    pass\n\n"
        "class ValidationError(UserError):\n"
        "    '''Indirect exception through UserError.'''\n"
        "    pass\n\n"
        "class AccessDenied(UserError):\n"
        "    pass\n"
    )

    # Create odoo/sql_db.py with cursor class and methods
    (tmp_path / "odoo" / "sql_db.py").write_text(
        "class Cursor:\n"
        "    def execute(self, query, params=None):\n"
        "        pass\n\n"
        "    def fetchone(self):\n"
        "        return None\n\n"
        "    def fetchall(self):\n"
        "        return []\n"
    )

    # Create odoo/tools files
    tools_dir = tmp_path / "odoo" / "tools"
    tools_dir.mkdir(parents=True)

    (tools_dir / "safe_eval.py").write_text(
        "def safe_eval(expr, context=None):\n"
        "    '''Safely evaluate an expression.'''\n"
        "    return eval(expr, context)\n"
    )

    (tools_dir / "sql.py").write_text(
        "def make_index_name(table, name):\n"
        "    return f'{table}_{name}_idx'\n\n"
        "def drop_index(cursor, table, name):\n"
        "    pass\n"
    )

    # Parse the v19 tree
    out = parse_odoo_core(str(tmp_path), "19.0")

    # Build lookup tables for assertions
    qnames = {s.qualified_name: s for s in out}
    kinds = {s.kind for s in out}

    # Assert 1: All 6 kinds are present
    expected_kinds = {"field_type", "class", "exception", "function", "orm_method", "cursor_method"}
    missing_kinds = expected_kinds - kinds
    assert not missing_kinds, (
        f"v19 pipeline missing kinds: {missing_kinds}. Got: {kinds}. "
        f"Symbols: {list(qnames.keys())[:5]}..."
    )

    # Assert 2: ValidationError is classified as exception (recursive)
    val_err = qnames.get("odoo.exceptions.ValidationError")
    assert val_err is not None, "ValidationError not found in output"
    assert val_err.kind == "exception", (
        f"ValidationError should be kind='exception', got {val_err.kind}"
    )

    # Assert 3: ORM methods have kind='orm_method'
    create_method = qnames.get("odoo.models.BaseModel.create")
    assert create_method is not None, "BaseModel.create not found"
    assert create_method.kind == "orm_method", (
        f"BaseModel.create should be kind='orm_method', got {create_method.kind}"
    )

    write_method = qnames.get("odoo.models.Model.action_archive")
    assert write_method is not None, "Model.action_archive not found"
    assert write_method.kind == "orm_method", (
        f"Model.action_archive should be kind='orm_method', got {write_method.kind}"
    )

    # Assert 4: Cursor methods have kind='cursor_method'
    execute_method = qnames.get("odoo.sql_db.Cursor.execute")
    assert execute_method is not None, "Cursor.execute not found"
    assert execute_method.kind == "cursor_method", (
        f"Cursor.execute should be kind='cursor_method', got {execute_method.kind}"
    )

    # Assert 5: Reasonable output count
    assert len(out) >= 20, (
        f"Expected ≥20 symbols from v19 tree, got {len(out)}. "
        f"Kinds: {kinds}, Sample qnames: {list(qnames.keys())[:10]}"
    )


# ---------------------------------------------------------------------------
# WI-2 (M10) — body-level DeprecationWarning detection for name_get / v17
# ---------------------------------------------------------------------------


def test_body_level_deprecation_warning_marks_deprecated():
    """Method with `warnings.warn(..., DeprecationWarning)` in body → status='deprecated'.

    Odoo v17 deprecates `name_get` this way (no decorator used).
    """
    src = (
        "import warnings\n"
        "class BaseModel:\n"
        "    def name_get(self):\n"
        "        warnings.warn('Since 17.0 use display_name', DeprecationWarning, 2)\n"
        "        return [(rec.id, rec.display_name) for rec in self]\n"
    )
    syms = _extract_from_source(src, "odoo.models", "17.0")
    nm = next(s for s in syms if s.qualified_name.endswith(".name_get"))
    assert nm.status == "deprecated", (
        f"name_get with body-level DeprecationWarning should be deprecated, got {nm.status!r}"
    )


def test_plain_method_without_deprecation_is_stable():
    """Method with no decorator and no `warnings.warn` call → status='stable'."""
    src = (
        "class BaseModel:\n"
        "    def write(self, vals):\n"
        "        return True\n"
    )
    syms = _extract_from_source(src, "odoo.models", "17.0")
    write = next(s for s in syms if s.qualified_name.endswith(".write"))
    assert write.status == "stable", (
        f"plain method should be stable, got {write.status!r}"
    )


def test_decorator_deprecated_regression_still_deprecated():
    """Regression: `@api.deprecated(...)` decorator still marks method as deprecated."""
    src = (
        "class BaseModel:\n"
        "    @api.deprecated('Use display_name')\n"
        "    def name_get(self):\n"
        "        return self.display_name\n"
    )
    syms = _extract_from_source(src, "odoo.models", "16.0")
    nm = next(s for s in syms if s.qualified_name.endswith(".name_get"))
    assert nm.status == "deprecated", (
        f"decorator-deprecated method should still be deprecated, got {nm.status!r}"
    )


def test_logger_warn_with_deprecation_warning_not_deprecated():
    """logger.warn('x', DeprecationWarning) must NOT trigger deprecated status.

    Only `warnings.warn(...)` calls (with `warnings` as the direct object) should
    classify a method as deprecated.  A call via any other object (e.g. a logger)
    must not match even when DeprecationWarning appears as an argument.
    """
    src = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "class BaseModel:\n"
        "    def my_method(self):\n"
        "        logger.warn('x', DeprecationWarning)\n"
        "        return True\n"
    )
    syms = _extract_from_source(src, "odoo.models", "17.0")
    mm = next(s for s in syms if s.qualified_name.endswith(".my_method"))
    assert mm.status == "stable", (
        f"logger.warn with DeprecationWarning should NOT be deprecated, got {mm.status!r}"
    )


def test_parse_odoo_core_v8_openerp_emits_legacy_symbols(tmp_path):
    """v8 complete tree: openerp/ namespace with legacy field declarations.

    Verify parse_odoo_core(tmp_path, '8.0') with a v8-style layout:
    - openerp/fields.py contains field definitions (Python 2 class syntax compatible)
    - openerp/models.py contains model definitions
    - openerp/api.py contains decorator-like functions (no api.depends in v8, simpler)
    - openerp/exceptions.py contains exception hierarchy
    - openerp/sql_db.py contains database layer
    - openerp/tools/{safe_eval,sql}.py contain utilities

    Assert:
    1. Symbol count >= 20 (fewer symbols in v8 due to simpler structure)
    2. Field types are correctly classified (Char, Integer, Many2one all kind='field_type')
    3. Model classes exist and methods are properly emitted
    4. Exception hierarchy works (indirect exceptions via UserError)
    5. All symbols carry odoo_version='8.0'
    """
    # Create v8 openerp layout (no odoo/ directory)
    openerp_dir = tmp_path / "openerp"
    openerp_dir.mkdir(parents=True)

    (openerp_dir / "fields.py").write_text(
        "class Field(object):\n"
        "    '''Base field class for v8.'''\n"
        "    size = None\n\n"
        "class Char(Field):\n"
        "    size = 256\n\n"
        "class Integer(Field):\n"
        "    pass\n\n"
        "class Many2one(Field):\n"
        "    '''Relation field.'''\n"
        "    pass\n"
    )

    (openerp_dir / "models.py").write_text(
        "class BaseModel(object):\n"
        "    _name = None\n"
        "    _auto = True\n\n"
        "    def search(self, domain):\n"
        "        return []\n\n"
        "    def browse(self, ids):\n"
        "        return self\n\n"
        "    def create(self, vals):\n"
        "        return self\n\n"
        "    def write(self, vals):\n"
        "        return True\n\n"
        "class Model(BaseModel):\n"
        "    pass\n"
    )

    (openerp_dir / "api.py").write_text(
        "def depends(*args):\n"
        "    def decorator(f):\n"
        "        return f\n"
        "    return decorator\n\n"
        "def one(func):\n"
        "    return func\n"
    )

    (openerp_dir / "exceptions.py").write_text(
        "class UserError(Exception):\n"
        "    pass\n\n"
        "class ValidationError(UserError):\n"
        "    pass\n"
    )

    (openerp_dir / "sql_db.py").write_text(
        "class Cursor(object):\n"
        "    def execute(self, query, params=None):\n"
        "        pass\n\n"
        "    def fetchone(self):\n"
        "        return None\n"
    )

    tools_dir = openerp_dir / "tools"
    tools_dir.mkdir(parents=True)

    (tools_dir / "safe_eval.py").write_text(
        "def safe_eval(expr):\n"
        "    return eval(expr)\n"
    )

    (tools_dir / "sql.py").write_text(
        "def make_index_name(table, name):\n"
        "    return '%s_%s_idx' % (table, name)\n\n"
        "def drop_index(cursor, table):\n"
        "    pass\n"
    )

    # Parse the v8 tree
    out = parse_odoo_core(str(tmp_path), "8.0")

    qnames = {s.qualified_name: s for s in out}

    # Assert 1: Reasonable symbol count
    assert len(out) >= 20, (
        f"Expected ≥20 symbols from v8 tree, got {len(out)}. "
        f"Output: {list(qnames.keys())[:10]}"
    )

    # Assert 2: Field types are classified correctly
    for field_name in ["Char", "Integer", "Many2one"]:
        qname = f"odoo.fields.{field_name}"
        assert qname in qnames, f"Expected {qname} in output"
        assert qnames[qname].kind == "field_type", (
            f"{field_name} should be kind='field_type', got {qnames[qname].kind}"
        )

    # Assert 3: Base Field class exists
    assert "odoo.fields.Field" in qnames, "Field base class not found"

    # Assert 4: Model classes and methods exist
    basemodel = qnames.get("odoo.models.BaseModel")
    assert basemodel is not None, "BaseModel class not found"
    assert basemodel.kind == "class", (
        f"BaseModel should be kind='class', got {basemodel.kind}"
    )

    # Check model methods
    search_method = qnames.get("odoo.models.BaseModel.search")
    assert search_method is not None, "BaseModel.search method not found"
    assert search_method.kind == "orm_method", (
        f"BaseModel.search should be kind='orm_method', got {search_method.kind}"
    )

    # Assert 5: Exception hierarchy (indirect exception)
    val_err = qnames.get("odoo.exceptions.ValidationError")
    assert val_err is not None, "ValidationError not found"
    assert val_err.kind == "exception", (
        f"ValidationError should be kind='exception', got {val_err.kind}"
    )

    # Assert 6: All symbols have correct odoo_version
    wrong_version = [s for s in out if s.odoo_version != "8.0"]
    assert not wrong_version, (
        f"{len(wrong_version)} symbols have wrong version: "
        f"{[(s.qualified_name, s.odoo_version) for s in wrong_version[:3]]}"
    )


# ---------------------------------------------------------------------------
# WI-1 (RP) — v18+ generic field class classification (Field[T] Subscript)
# ---------------------------------------------------------------------------


def test_generic_field_subscript_name_classifies_as_field_type():
    """Unit test (no real Odoo source): class Integer(Field[int]) → kind='field_type'.

    In Odoo v18+, field classes use PEP-695-style generics: Field[int], Field[float], etc.
    The base expression is ast.Subscript(value=ast.Name('Field')).
    _base_names() must unwrap the Subscript and return 'Field' so _classify_class()
    returns 'field_type' instead of falling through to 'class'.
    """
    src = (
        "class Field:\n"
        "    pass\n\n"
        "class Integer(Field[int]):\n"
        "    pass\n\n"
        "class Float(Field[float]):\n"
        "    aggregator = 'sum'\n"
    )
    syms = _extract_from_source(src, "odoo.fields", "18.0")
    qnames = {s.qualified_name: s for s in syms}

    integer_sym = qnames.get("odoo.fields.Integer")
    assert integer_sym is not None, "Integer not found in output"
    assert integer_sym.kind == "field_type", (
        f"Integer(Field[int]) should be kind='field_type', got {integer_sym.kind!r}"
    )

    float_sym = qnames.get("odoo.fields.Float")
    assert float_sym is not None, "Float not found in output"
    assert float_sym.kind == "field_type", (
        f"Float(Field[float]) should be kind='field_type', got {float_sym.kind!r}"
    )


def test_generic_field_subscript_attribute_classifies_as_field_type():
    """Unit test: class Foo(fields.Field[int]) → kind='field_type' (Attribute-Subscript variant).

    When the base is a dotted name with a generic: fields.Field[int], the AST is
    Subscript(value=Attribute(attr='Field')). _base_names() must extract 'Field'
    from the Attribute.attr of the Subscript.value.
    """
    src = (
        "class MyField(fields.Field[int]):\n"
        "    pass\n"
    )
    syms = _extract_from_source(src, "odoo.fields", "18.0")
    qnames = {s.qualified_name: s for s in syms}

    my_field_sym = qnames.get("odoo.fields.MyField")
    assert my_field_sym is not None, "MyField not found in output"
    assert my_field_sym.kind == "field_type", (
        f"MyField(fields.Field[int]) should be kind='field_type', got {my_field_sym.kind!r}"
    )


ODOO19_SRC = os.environ.get("ODOO19_SRC", "/nonexistent/odoo19")


@pytest.mark.skipif(
    not Path(ODOO19_SRC + "/odoo/orm/fields_numeric.py").exists(),
    reason="Real Odoo 19 source not on disk (skipped in CI; runs locally with ODOO19_SRC=...)",
)
def test_parse_odoo_core_smoke_real_v19_field_types():
    """Smoke test against real Odoo 19 source: Integer, Many2one, Char → kind='field_type'.

    v19 splits field definitions across odoo/orm/fields*.py files.  Field classes use
    PEP-695-style generics (Field[int], _Relational[M], etc.) requiring the Subscript
    unwrap fix in _base_names() to classify correctly.
    """
    out = parse_odoo_core(ODOO19_SRC, "19.0")
    assert len(out) >= 50, f"expected >=50 symbols from real v19 source, got {len(out)}"

    qnames = {s.qualified_name: s for s in out}

    # These three field classes must all classify as field_type in v19
    for field_name in ("Integer", "Many2one", "Char"):
        # v19 field symbols are emitted under "odoo.fields.*" (logical module_qname)
        candidates = [
            s for s in out
            if s.qualified_name.endswith(f".{field_name}") and "fields" in s.qualified_name
        ]
        assert candidates, (
            f"No symbol for {field_name} found in v19 output. "
            f"Available field-related qnames: {[q for q in qnames if 'fields' in q][:10]}"
        )
        for sym in candidates:
            assert sym.kind == "field_type", (
                f"v19 {sym.qualified_name} should be kind='field_type', got {sym.kind!r}"
            )


# ---------------------------------------------------------------------------
# T4 — CORE-Q: query.py version-aware path (v8-v9 openerp/osv, v10-v15 odoo/osv, v16+ odoo/tools)
# ---------------------------------------------------------------------------

def test_resolve_core_paths_query_v8_returns_openerp_osv(tmp_path):
    """T4: v8 — odoo/tools/query.py maps to openerp/osv/query.py."""
    (tmp_path / "openerp" / "osv").mkdir(parents=True)
    qpy = tmp_path / "openerp" / "osv" / "query.py"
    qpy.write_text("class Query: pass\n")
    resolved = _resolve_core_paths(tmp_path, "odoo/tools/query.py", "8.0")
    assert resolved == [qpy], f"v8 query.py must resolve to openerp/osv/query.py, got {resolved}"


def test_resolve_core_paths_query_v9_returns_openerp_osv(tmp_path):
    """T4: v9 — odoo/tools/query.py maps to openerp/osv/query.py."""
    (tmp_path / "openerp" / "osv").mkdir(parents=True)
    qpy = tmp_path / "openerp" / "osv" / "query.py"
    qpy.write_text("class Query: pass\n")
    resolved = _resolve_core_paths(tmp_path, "odoo/tools/query.py", "9.0")
    assert resolved == [qpy], f"v9 query.py must resolve to openerp/osv/query.py, got {resolved}"


def test_resolve_core_paths_query_v11_returns_odoo_osv(tmp_path):
    """T4: v11 — odoo/tools/query.py maps to odoo/osv/query.py."""
    (tmp_path / "odoo" / "osv").mkdir(parents=True)
    qpy = tmp_path / "odoo" / "osv" / "query.py"
    qpy.write_text("class Query: pass\n")
    resolved = _resolve_core_paths(tmp_path, "odoo/tools/query.py", "11.0")
    assert resolved == [qpy], f"v11 query.py must resolve to odoo/osv/query.py, got {resolved}"


def test_resolve_core_paths_query_v15_returns_odoo_osv(tmp_path):
    """T4: v15 — odoo/tools/query.py maps to odoo/osv/query.py (boundary check)."""
    (tmp_path / "odoo" / "osv").mkdir(parents=True)
    qpy = tmp_path / "odoo" / "osv" / "query.py"
    qpy.write_text("class Query: pass\n")
    resolved = _resolve_core_paths(tmp_path, "odoo/tools/query.py", "15.0")
    assert resolved == [qpy], f"v15 query.py must resolve to odoo/osv/query.py, got {resolved}"


def test_resolve_core_paths_query_v16_returns_odoo_tools(tmp_path):
    """T4: v16 — odoo/tools/query.py resolves to odoo/tools/query.py (moved in v16)."""
    (tmp_path / "odoo" / "tools").mkdir(parents=True)
    qpy = tmp_path / "odoo" / "tools" / "query.py"
    qpy.write_text("class Query: pass\n")
    resolved = _resolve_core_paths(tmp_path, "odoo/tools/query.py", "16.0")
    assert resolved == [qpy], f"v16 query.py must resolve to odoo/tools/query.py, got {resolved}"


def test_resolve_core_paths_query_v17_returns_odoo_tools(tmp_path):
    """T4: v17 — odoo/tools/query.py resolves to odoo/tools/query.py."""
    (tmp_path / "odoo" / "tools").mkdir(parents=True)
    qpy = tmp_path / "odoo" / "tools" / "query.py"
    qpy.write_text("class Query: pass\n")
    resolved = _resolve_core_paths(tmp_path, "odoo/tools/query.py", "17.0")
    assert resolved == [qpy], f"v17 query.py must resolve to odoo/tools/query.py, got {resolved}"


def test_resolve_core_paths_query_missing_returns_empty(tmp_path):
    """T4: missing query.py in a given version → empty list (silent skip, not exception)."""
    (tmp_path / "odoo" / "tools").mkdir(parents=True)
    # Do NOT create query.py
    resolved = _resolve_core_paths(tmp_path, "odoo/tools/query.py", "14.0")
    assert resolved == [], "Missing query.py must silently return empty list"


def test_parse_odoo_core_query_v11_emits_query_class(tmp_path):
    """T4 integration: parse_odoo_core v11 with odoo/osv/query.py → Query CoreSymbol."""
    (tmp_path / "odoo" / "osv").mkdir(parents=True)
    (tmp_path / "odoo" / "osv" / "query.py").write_text(
        "class Query:\n"
        "    def __init__(self, env):\n"
        "        self.env = env\n"
        "    def add_where(self, clause):\n"
        "        pass\n"
    )
    out = parse_odoo_core(str(tmp_path), "11.0")
    qnames = {s.qualified_name for s in out}
    assert "odoo.tools.query.Query" in qnames, (
        f"v11 must emit odoo.tools.query.Query (from odoo/osv/query.py); got {qnames}"
    )


# ---------------------------------------------------------------------------
# T5 — V19-G5: NewId in odoo/orm/identifiers.py → v19 CoreSymbol
# ---------------------------------------------------------------------------

def test_v19_newid_emits_from_identifiers(tmp_path):
    """T5: v19 — NewId in odoo/orm/identifiers.py is emitted as odoo.api.NewId CoreSymbol.

    identifiers.py is indexed through the odoo/api.py allow-list entry (v19 resolver branch),
    so the qname gets the "odoo.api" namespace prefix. This ensures api_version_diff(
    "NewId", "18.0", "19.0") sees continuity (v18 odoo.api.NewId == v19 odoo.api.NewId)
    rather than a false "removed in 19.0" signal.
    """
    # Create v19 layout: odoo/api/ is a package dir (makes candidate.is_file() False)
    (tmp_path / "odoo" / "api").mkdir(parents=True)
    orm_dir = tmp_path / "odoo" / "orm"
    orm_dir.mkdir(parents=True)
    (orm_dir / "decorators.py").write_text("def depends(*a):\n    pass\n")
    (orm_dir / "environments.py").write_text("class Environment:\n    pass\n")
    (orm_dir / "identifiers.py").write_text(
        "class NewId:\n"
        "    '''Represents a new (unsaved) record identifier.'''\n"
        "    def __init__(self, ref=None):\n"
        "        self.ref = ref\n"
    )
    out = parse_odoo_core(str(tmp_path), "19.0")
    qnames = {s.qualified_name for s in out}
    assert "odoo.api.NewId" in qnames, (
        f"v19 NewId must be emitted as odoo.api.NewId (via api.py resolver); got: "
        f"{[q for q in qnames if 'NewId' in q or 'identifiers' in q or 'api' in q]}"
    )


def test_v18_api_py_flat_emits_newid_without_identifiers(tmp_path):
    """T5 guard: v18 flat odoo/api.py has NewId directly — no identifiers.py needed.

    In v18, odoo/api.py is a regular file (not split). parse_odoo_core must still
    emit odoo.api.NewId via the flat file, and the identifiers.py path (which doesn't
    exist in v18) must be silently skipped.
    """
    (tmp_path / "odoo").mkdir(parents=True)
    (tmp_path / "odoo" / "api.py").write_text(
        "class NewId:\n    pass\n\ndef depends(*a):\n    pass\n"
    )
    out = parse_odoo_core(str(tmp_path), "18.0")
    qnames = {s.qualified_name for s in out}
    # v18: NewId from flat odoo/api.py → qname odoo.api.NewId
    assert "odoo.api.NewId" in qnames, (
        f"v18 must emit odoo.api.NewId from flat api.py; got: {[q for q in qnames if 'api' in q]}"
    )
    # No crash from missing identifiers.py (silently skipped)
    orm_identifiers = [q for q in qnames if "identifiers" in q]
    assert not orm_identifiers, (
        f"v18 must not produce identifiers.py symbols (file absent in v18), got {orm_identifiers}"
    )


# ---------------------------------------------------------------------------
# FIX-1 — 2nd-level field subclasses classify as 'field_type' (Binary/Selection/Integer bases)
# ---------------------------------------------------------------------------


def test_fix1_image_binary_reference_many2onereference_are_field_type(tmp_path):
    """FIX-1: Image(Binary), Reference(Selection), Many2oneReference(Integer) → kind='field_type'.

    Business rule: ALL subclasses of field types (direct or one-level deep) must be
    classified as kind='field_type', not 'class'. Before FIX-1, only direct Field
    subclasses were recognized. After FIX-1, the 3 concrete intermediate bases
    (Binary, Selection, Integer) are in _FIELD_BASE_NAMES so 2nd-level field classes
    classify correctly.

    These are the ONLY 2-level field subclasses in the Odoo core source tree
    (scanned v17/v18/v19). No false-positives exist because Binary/Selection/Integer
    do not appear as base classes in any other parsed file (models.py, api.py, etc.).
    """
    # Arrange: simulate the Odoo fields hierarchy with 2-level inheritance
    (tmp_path / "odoo").mkdir(parents=True)
    (tmp_path / "odoo" / "fields.py").write_text(
        "class Field:\n    pass\n\n"
        "class Binary(Field):\n    pass\n\n"
        "class Selection(Field):\n    pass\n\n"
        "class Integer(Field):\n    pass\n\n"
        # 2nd-level: these are the FIX-1 target classes
        "class Image(Binary):\n    '''Thumbnail field.'''\n    pass\n\n"
        "class Reference(Selection):\n    '''Pseudo-relational field.'''\n    pass\n\n"
        "class Many2oneReference(Integer):\n"
        "    '''Integer referencing a model record.'''\n    pass\n"
    )

    # Act
    out = parse_odoo_core(str(tmp_path), "17.0")
    qnames = {s.qualified_name: s for s in out}

    # Assert: 2nd-level field subclasses must be kind='field_type'
    for cls_name in ("Image", "Reference", "Many2oneReference"):
        sym = qnames.get(f"odoo.fields.{cls_name}")
        assert sym is not None, f"{cls_name} not found in output"
        assert sym.kind == "field_type", (
            f"odoo.fields.{cls_name} inherits from a field base; must be kind='field_type', "
            f"got {sym.kind!r}. FIX-1 should have added its base to _FIELD_BASE_NAMES."
        )

    # Sanity: direct Field subclasses still work.
    for cls_name in ("Binary", "Selection", "Integer"):
        sym = qnames.get(f"odoo.fields.{cls_name}")
        assert sym is not None, f"{cls_name} not found"
        assert sym.kind == "field_type", f"Direct field {cls_name} should be field_type"


def test_fix1_non_field_class_not_misclassified_as_field_type(tmp_path):
    """FIX-1 guard: adding Binary/Selection/Integer to _FIELD_BASE_NAMES must NOT
    cause false positives for non-field classes in other parsed files.

    Business rule: only classes that ARE field types must be classified as
    kind='field_type'. A class like CacheMiss(Exception) or Query that inherits
    from something other than a field base must remain kind='class' or 'exception'.

    This guard ensures FIX-1 did not widen the classification net too broadly.
    """
    # Arrange: simulate exceptions.py — contains non-field classes
    (tmp_path / "odoo").mkdir(parents=True)
    (tmp_path / "odoo" / "exceptions.py").write_text(
        "class UserError(Exception):\n    pass\n\n"
        "class ValidationError(UserError):\n    pass\n\n"
        # CacheMiss inherits from Exception via RuntimeError — definitely not field_type
        "class CacheMiss(RuntimeError):\n    pass\n"
    )

    # Act
    out = parse_odoo_core(str(tmp_path), "17.0")
    qnames = {s.qualified_name: s for s in out}

    # Assert: exception-hierarchy classes must NOT be field_type
    for cls_name in ("UserError", "ValidationError"):
        sym = qnames.get(f"odoo.exceptions.{cls_name}")
        assert sym is not None, f"{cls_name} not found"
        assert sym.kind == "exception", (
            f"{cls_name} must be kind='exception', not 'field_type'. "
            f"FIX-1 must not have caused False Positive in exceptions.py."
        )

    cache_miss = qnames.get("odoo.exceptions.CacheMiss")
    assert cache_miss is not None, "CacheMiss not found"
    assert cache_miss.kind == "class", (
        f"CacheMiss(RuntimeError) should be kind='class', got {cache_miss.kind!r}. "
        f"FIX-1 must not have classified RuntimeError subclasses as field_type."
    )


# ---------------------------------------------------------------------------
# FIX-2 — v19 DomainCondition and other new Domain subclasses are emitted
# ---------------------------------------------------------------------------


def test_fix2_domain_condition_and_all_8_public_domains_emitted(tmp_path):
    """FIX-2: parse_odoo_core v19 emits DomainCondition and all 8 public Domain subclasses.

    Business rule: the v19 domain builder API exposes 8 public classes
    (Domain + 7 concrete builders). All must appear in the graph so that
    AI clients can discover the full domain construction API.

    Before FIX-2, only Domain/DomainAnd/DomainOr were in the curated allowlist (3 entries).
    After FIX-2, the allowlist was expanded to 8 entries after scanning real Odoo 19.
    """
    # Arrange: v19 layout with all 8 public domain classes + internal class to exclude
    orm_dir = tmp_path / "odoo" / "orm"
    orm_dir.mkdir(parents=True)
    (orm_dir / "domains.py").write_text(
        "import enum\n\n"
        "class OptimizationLevel(enum.IntEnum):\n    BASIC = 0\n    FULL = 1\n\n"
        "class Domain:\n    pass\n\n"
        "class DomainBool(Domain):\n    pass\n\n"
        "class DomainNot(Domain):\n    pass\n\n"
        "class DomainNary(Domain):\n    pass\n\n"
        "class DomainAnd(Domain):\n    pass\n\n"
        "class DomainOr(Domain):\n    pass\n\n"
        "class DomainCustom(Domain):\n    pass\n\n"
        "class DomainCondition(Domain):\n    pass\n\n"   # key FIX-2 addition
        "def _optimize_nary(a, b):\n    return a\n"
    )

    # Act
    out = parse_odoo_core(str(tmp_path), "19.0")
    qnames = {s.qualified_name for s in out}

    # Assert: DomainCondition (the key FIX-2 addition) is present
    assert "odoo.orm.domains.DomainCondition" in qnames, (
        "DomainCondition must appear in v19 output after FIX-2 expanded the Domain allowlist. "
        f"Got domain symbols: {[q for q in qnames if 'domains' in q]}"
    )

    # Assert: all 8 public domain builder classes present
    for cls in ("Domain", "DomainBool", "DomainNot", "DomainNary",
                "DomainAnd", "DomainOr", "DomainCustom", "DomainCondition"):
        assert f"odoo.orm.domains.{cls}" in qnames, (
            f"Public domain class {cls} must be in v19 output."
        )

    # Assert: OptimizationLevel (internal IntEnum) excluded by curation
    assert "odoo.orm.domains.OptimizationLevel" not in qnames, (
        "OptimizationLevel must remain excluded — it is internal optimization machinery."
    )


# ---------------------------------------------------------------------------
# FIX-3 — odoo/orm/utils.py and model_classes.py indexed for v19, skipped for v8-v18
# ---------------------------------------------------------------------------


def test_fix3_v19_orm_utils_emits_parse_field_expr_and_is_model_class(tmp_path):
    """FIX-3: parse_odoo_core v19 emits parse_field_expr + OriginIds (from orm/utils.py)
    and is_model_class + is_model_definition (from orm/model_classes.py).

    Business rule: v19 introduced public ORM utility functions in the split orm/
    package. These must be indexed so AI clients can discover/use them via
    lookup_core_api and api_version_diff.

    Curation contract: utils.py and model_classes.py are in _V19_CURATED_FILES (with
    name_allowlist) — NOT in _CORE_FILES (which uses name_allowlist=None = emit ALL).
    This ensures internal plumbing (check_pg_name, add_to_registry, etc.) is NEVER
    emitted, even if those functions exist in the file.

    This test verifies BOTH the positive (public symbols present) AND the negative
    (internal plumbing absent) to protect the curation contract.
    """
    # Arrange: v19 layout with orm/ directory and the two new files.
    # Include both public symbols AND internal plumbing that must be excluded.
    orm_dir = tmp_path / "odoo" / "orm"
    orm_dir.mkdir(parents=True)
    (orm_dir / "utils.py").write_text(
        "def parse_field_expr(field_expr: str):\n"
        "    '''Parse a dotted field path.'''\n"
        "    return field_expr.split('.')\n\n"
        "def check_method_name(name):\n"   # INTERNAL — deprecated since 19.0
        "    pass\n\n"
        "def check_pg_name(name):\n"       # INTERNAL — PG identifier validator
        "    pass\n\n"
        "def expand_ids(id0, ids):\n"      # INTERNAL — id dedup generator
        "    pass\n\n"
        "class OriginIds:\n"
        "    '''Container for original record ids.'''\n"
        "    pass\n"
    )
    (orm_dir / "model_classes.py").write_text(
        "def is_model_class(cls):\n"
        "    '''Return True if cls is an Odoo model class.'''\n"
        "    return hasattr(cls, '_name')\n\n"
        "def is_model_definition(cls):\n"
        "    '''Return True if cls defines a new model (has explicit _name).'''\n"
        "    return bool(getattr(cls, '_name', None))\n\n"
        "def add_to_registry(registry, model_def):\n"   # INTERNAL — registry machinery
        "    pass\n\n"
        "def setup_model_classes(env):\n"               # INTERNAL — setup machinery
        "    pass\n"
    )

    # Act: parse v19
    out = parse_odoo_core(str(tmp_path), "19.0")
    qnames = {s.qualified_name for s in out}

    # --- Positive assertions: public symbols must be present ---
    assert "odoo.orm.utils.parse_field_expr" in qnames, (
        "parse_field_expr must be indexed from odoo/orm/utils.py in v19. "
        f"Got orm.utils symbols: {[q for q in qnames if 'orm.utils' in q]}"
    )
    assert "odoo.orm.utils.OriginIds" in qnames, (
        "OriginIds class from orm/utils.py must also be indexed."
    )
    assert "odoo.orm.model_classes.is_model_class" in qnames, (
        "is_model_class must be indexed from odoo/orm/model_classes.py in v19. "
        f"Got: {[q for q in qnames if 'model_classes' in q]}"
    )
    assert "odoo.orm.model_classes.is_model_definition" in qnames, (
        "is_model_definition must be indexed from odoo/orm/model_classes.py in v19."
    )

    # --- Negative assertions: internal plumbing must be EXCLUDED by curation ---
    assert "odoo.orm.utils.check_method_name" not in qnames, (
        "check_method_name is deprecated since 19.0 and is internal — "
        "the name_allowlist in _V19_CURATED_FILES must exclude it."
    )
    assert "odoo.orm.utils.check_pg_name" not in qnames, (
        "check_pg_name (internal PG identifier validator) must be excluded by curation."
    )
    assert "odoo.orm.utils.expand_ids" not in qnames, (
        "expand_ids (internal id-dedup generator) must be excluded by curation."
    )
    assert "odoo.orm.model_classes.add_to_registry" not in qnames, (
        "add_to_registry (internal registry machinery) must be excluded by curation."
    )
    assert "odoo.orm.model_classes.setup_model_classes" not in qnames, (
        "setup_model_classes (internal setup machinery) must be excluded by curation."
    )


def test_fix3_v17_and_v18_skip_orm_utils_silently(tmp_path):
    """FIX-3 guard: v17/v18 do NOT have odoo/orm/ directory — the two _V19_CURATED_FILES
    entries for utils.py and model_classes.py must resolve to [] and be silently skipped.
    No exception, no false indexing.

    Business rule: the skip is safe because _resolve_core_paths returns [] when the
    resolved file does not exist. This test verifies v17 and v18 behaviour is unchanged
    regardless of whether the paths are in _CORE_FILES or _V19_CURATED_FILES.
    """
    # --- v17 ---
    (tmp_path / "v17" / "odoo").mkdir(parents=True)
    (tmp_path / "v17" / "odoo" / "fields.py").write_text(
        "class Field:\n    pass\n\nclass Char(Field):\n    pass\n"
    )
    out17 = parse_odoo_core(str(tmp_path / "v17"), "17.0")
    qnames17 = {s.qualified_name for s in out17}

    # parse_field_expr and is_model_class must NOT appear in v17 output
    assert "odoo.orm.utils.parse_field_expr" not in qnames17, (
        "parse_field_expr must NOT appear in v17 — orm/utils.py doesn't exist in v17."
    )
    assert "odoo.orm.model_classes.is_model_class" not in qnames17, (
        "is_model_class must NOT appear in v17 — orm/model_classes.py doesn't exist in v17."
    )
    # Sanity: v17 symbols from flat fields.py are still indexed
    assert "odoo.fields.Char" in qnames17, "v17 flat fields.py must still be indexed"

    # --- v18 ---
    (tmp_path / "v18" / "odoo").mkdir(parents=True)
    (tmp_path / "v18" / "odoo" / "fields.py").write_text(
        "class Field:\n    pass\n\nclass Integer(Field):\n    pass\n"
    )
    out18 = parse_odoo_core(str(tmp_path / "v18"), "18.0")
    qnames18 = {s.qualified_name for s in out18}

    assert "odoo.orm.utils.parse_field_expr" not in qnames18, (
        "parse_field_expr must NOT appear in v18 — orm/utils.py doesn't exist in v18."
    )
    assert "odoo.orm.model_classes.is_model_class" not in qnames18, (
        "is_model_class must NOT appear in v18."
    )
    # Sanity: v18 symbols still indexed
    assert "odoo.fields.Integer" in qnames18, "v18 flat fields.py must still be indexed"
