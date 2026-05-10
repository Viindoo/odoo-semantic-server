# tests/test_parser_odoo_core.py
"""Parser_odoo_core tests (M4.5 WI2.2 — extract CoreSymbol from Odoo upstream source).

Allow-list 8 file approach (per ADR-0002 §6 — KISS, no full-source walk):
    odoo/tools/safe_eval.py, query.py, sql.py
    odoo/fields.py, models.py, api.py, sql_db.py, exceptions.py
"""
from pathlib import Path

import pytest

from src.indexer.parser_odoo_core import (
    _CORE_FILES,
    _extract_from_source,
    _resolve_core_paths,
    _version_prefix,
    parse_odoo_core,
)


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


def test_core_files_allowlist_has_eight_paths():
    """ADR-0002 §6 — allow-list is exactly 8 stable Odoo core files."""
    assert len(_CORE_FILES) == 8
    # Sanity: paths look right (no walk-toàn-bộ-source escape)
    for path in _CORE_FILES:
        assert path.startswith("odoo/")
        assert path.endswith(".py")


@pytest.mark.skipif(
    not Path("/home/tuan/git/odoo17/odoo/tools/safe_eval.py").exists(),
    reason="Real Odoo 17 source not on disk (skipped in CI; runs locally)",
)
def test_parse_odoo_core_smoke_real_v17():
    """Smoke test against real Odoo 17 source on disk — extract sane number of symbols."""
    out = parse_odoo_core("/home/tuan/git/odoo17", "17.0")
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
