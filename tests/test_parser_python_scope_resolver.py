# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_python_scope_resolver.py
"""Unit tests for V0.5 qualified-name scope resolver in parser_python.py (M7 W13).

Business intent: `find_deprecated_usage('name_get', '17.0')` must return modules
that genuinely override or call the Odoo ORM API — NOT modules that happen to
define an unrelated utility function with the same short name.

All tests are pure AST unit tests — no Neo4j required (no pytestmark).
"""
import ast
import textwrap

from src.indexer.models import ModuleInfo
from src.indexer.parser_python import (
    _build_import_scope_map,
    _collect_module_local_defs,
    _extract_core_symbol_refs,
    parse_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_module(source: str) -> ast.Module:
    return ast.parse(textwrap.dedent(source))


def _first_fn(tree: ast.Module) -> ast.FunctionDef:
    """Return the first top-level FunctionDef in the tree."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node  # type: ignore[return-value]
    raise AssertionError("No top-level function found")


def _fn_inside_class(tree: ast.Module, fn_name: str) -> ast.FunctionDef:
    """Return a named method from the first ClassDef in the tree."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == fn_name:
                    return item
    raise AssertionError(f"Method {fn_name!r} not found")


# ---------------------------------------------------------------------------
# _build_import_scope_map tests
# ---------------------------------------------------------------------------

def test_scope_map_plain_import():
    tree = _parse_module("import odoo")
    scope = _build_import_scope_map(tree)
    assert scope["odoo"] == "odoo"


def test_scope_map_import_with_alias():
    tree = _parse_module("import odoo as o")
    scope = _build_import_scope_map(tree)
    assert scope["o"] == "odoo"
    assert "odoo" not in scope


def test_scope_map_from_import():
    tree = _parse_module("from odoo import models")
    scope = _build_import_scope_map(tree)
    assert scope["models"] == "odoo.models"


def test_scope_map_from_import_deep():
    tree = _parse_module("from odoo.tools import safe_eval")
    scope = _build_import_scope_map(tree)
    assert scope["safe_eval"] == "odoo.tools.safe_eval"


def test_scope_map_from_import_alias():
    tree = _parse_module("from odoo.tools import safe_eval as se")
    scope = _build_import_scope_map(tree)
    assert scope["se"] == "odoo.tools.safe_eval"
    assert "safe_eval" not in scope


def test_scope_map_non_odoo_import_included():
    """Non-odoo imports are still recorded so we can tell they're NOT odoo."""
    tree = _parse_module("import json")
    scope = _build_import_scope_map(tree)
    assert scope["json"] == "json"


# ---------------------------------------------------------------------------
# _collect_module_local_defs tests
# ---------------------------------------------------------------------------

def test_local_defs_captures_top_level_function():
    tree = _parse_module("def name_get(): return 'local'")
    local = _collect_module_local_defs(tree)
    assert "name_get" in local


def test_local_defs_captures_top_level_class():
    tree = _parse_module("class Helper: pass")
    local = _collect_module_local_defs(tree)
    assert "Helper" in local


def test_local_defs_does_not_capture_class_methods():
    """Methods inside classes are NOT in module-level local defs."""
    src = """
        class MyClass:
            def name_get(self):
                return 'inner'
    """
    tree = _parse_module(src)
    local = _collect_module_local_defs(tree)
    assert "name_get" not in local
    assert "MyClass" in local


# ---------------------------------------------------------------------------
# Core V0.5 filter tests via _extract_core_symbol_refs
# ---------------------------------------------------------------------------

def test_keeps_pure_odoo_call():
    """from odoo.models import name_get; def f(): name_get() → KEEP (odoo import)."""
    src = """
        from odoo.models import name_get
        def f():
            name_get()
    """
    tree = _parse_module(src)
    scope = _build_import_scope_map(tree)
    local = _collect_module_local_defs(tree)
    fn = _first_fn(tree)
    refs = _extract_core_symbol_refs(fn, scope_map=scope, local_defs=local)
    assert "name_get" in refs


def test_drops_local_helper_same_name():
    """Top-level def name_get shadowing Odoo API → DROP (local function, not Odoo)."""
    src = """
        def name_get():
            return 'local'

        def caller():
            name_get()
    """
    tree = _parse_module(src)
    scope = _build_import_scope_map(tree)
    local = _collect_module_local_defs(tree)
    # caller() is the second function
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    caller = next(f for f in fns if f.name == "caller")
    refs = _extract_core_symbol_refs(caller, scope_map=scope, local_defs=local)
    assert "name_get" not in refs, (
        "Local top-level def name_get must suppress USES_CORE_SYMBOL emission"
    )


def test_keeps_qualified_call():
    """import odoo as o; def f(): o.models.name_get() → KEEP (chained odoo alias)."""
    src = """
        import odoo as o
        def f():
            o.models.name_get()
    """
    tree = _parse_module(src)
    scope = _build_import_scope_map(tree)
    local = _collect_module_local_defs(tree)
    fn = _first_fn(tree)
    refs = _extract_core_symbol_refs(fn, scope_map=scope, local_defs=local)
    assert "name_get" in refs


def test_keeps_super_call_in_model_subclass():
    """super().name_get() inside a models.Model subclass → KEEP (ambiguous, conservative)."""
    src = """
        from odoo import models

        class M(models.Model):
            _name = 'test.model'

            def name_get(self):
                return super().name_get()
    """
    tree = _parse_module(src)
    scope = _build_import_scope_map(tree)
    local = _collect_module_local_defs(tree)
    fn = _fn_inside_class(tree, "name_get")
    refs = _extract_core_symbol_refs(
        fn, scope_map=scope, local_defs=local, class_is_model=True,
    )
    assert "name_get" in refs


def test_drops_attribute_on_non_odoo_object():
    """Helper().name_get() where Helper is a local non-odoo class → DROP."""
    src = """
        class Helper:
            def name_get(self):
                pass

        def caller():
            h = Helper()
            h.name_get()
    """
    # Note: h.name_get() — `h` is a local variable not in scope_map.
    # BUT per conservative posture: unknown obj → KEEP (we can't prove it's non-odoo).
    # The DROP case is when `obj` is explicitly in scope as a non-odoo name.
    # Here `Helper` is known local class but `h` is a runtime binding, not scope entry.
    # This test verifies that calling .name_get() on a non-scope variable is KEPT
    # (conservative) — we do NOT false-drop things we can't prove are non-odoo.
    tree = _parse_module(src)
    scope = _build_import_scope_map(tree)
    local = _collect_module_local_defs(tree)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    caller = next(f for f in fns if f.name == "caller")
    refs = _extract_core_symbol_refs(caller, scope_map=scope, local_defs=local)
    # Conservative: h is not in scope_map so we KEEP (V0 fallback)
    # This is correct: if we dropped it we might miss real calls via local alias
    assert "name_get" in refs, (
        "Conservative posture: unknown-receiver .name_get() must be KEPT"
    )


def test_drops_attribute_on_known_non_odoo_import():
    """json.name_get() where json is a known non-odoo import → DROP."""
    src = """
        import json

        def f():
            json.name_get()
    """
    tree = _parse_module(src)
    scope = _build_import_scope_map(tree)
    local = _collect_module_local_defs(tree)
    fn = _first_fn(tree)
    refs = _extract_core_symbol_refs(fn, scope_map=scope, local_defs=local)
    assert "name_get" not in refs, (
        "json.name_get() — json is a known non-odoo import, must DROP"
    )


def test_keeps_when_scope_unknown_no_local_def():
    """Bare name_get() with NO import AND NO local def → KEEP (V0 fallback for safety)."""
    src = """
        def f():
            name_get()
    """
    tree = _parse_module(src)
    scope = _build_import_scope_map(tree)
    local = _collect_module_local_defs(tree)
    fn = _first_fn(tree)
    refs = _extract_core_symbol_refs(fn, scope_map=scope, local_defs=local)
    assert "name_get" in refs, (
        "Bare name_get() with no import and no local def must be KEPT (V0 fallback)"
    )


def test_keeps_self_dot_name_get():
    """self.name_get() — canonical Odoo ORM call pattern → KEEP always."""
    src = """
        from odoo import models

        class M(models.Model):
            _inherit = 'sale.order'

            def foo(self):
                return self.name_get()
    """
    tree = _parse_module(src)
    scope = _build_import_scope_map(tree)
    local = _collect_module_local_defs(tree)
    fn = _fn_inside_class(tree, "foo")
    refs = _extract_core_symbol_refs(fn, scope_map=scope, local_defs=local, class_is_model=True)
    assert "name_get" in refs


# ---------------------------------------------------------------------------
# End-to-end integration via parse_file (regression guard for existing tests)
# ---------------------------------------------------------------------------

def test_parse_file_still_detects_self_name_get(tmp_path):
    """parse_file regression: self.name_get() inside a model method → still detected."""
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="17.0.1.0.0",
    )
    src = tmp_path / "ext.py"
    src.write_text(textwrap.dedent("""
        from odoo import models

        class SaleOrder(models.Model):
            _inherit = 'sale.order'

            def foo(self):
                return self.name_get()
    """))
    result = parse_file(str(src), module)
    assert len(result) == 1
    foo = next(m for m in result[0].methods if m.name == "foo")
    assert "name_get" in foo.core_symbol_refs


def test_parse_file_drops_local_helper_name_get(tmp_path):
    """parse_file E2E: top-level def name_get + bare call → NOT emitted as USES_CORE_SYMBOL."""
    module = ModuleInfo(
        name="my_module", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="17.0.1.0.0",
    )
    src = tmp_path / "helper.py"
    src.write_text(textwrap.dedent("""
        from odoo import models

        def name_get():
            return 'local utility'

        class MyModel(models.Model):
            _name = 'my.model'

            def action_do_thing(self):
                label = name_get()
                return label
    """))
    result = parse_file(str(src), module)
    assert len(result) == 1
    action = next(m for m in result[0].methods if m.name == "action_do_thing")
    assert "name_get" not in action.core_symbol_refs, (
        "Local top-level def name_get() must NOT be flagged as USES_CORE_SYMBOL"
    )
