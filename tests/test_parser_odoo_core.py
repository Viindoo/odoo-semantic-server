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
