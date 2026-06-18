# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_test.py
"""Extract test-surface info from Odoo test Python files (WI-1).

Responsibilities:
  1. Guard: only files matching test_*.py or *_test.py under a tests/ dir.
  2. era2 (v10+): full AST walk of tree.body for ClassDef nodes.
  3. era1 (v8/v9): regex degraded path - emits nodes with test_type='unknown',
     never crashes, never silently drops. The degradation is observable directly
     on the data (every era1 TestClass carries test_type='unknown'); the parser
     does not emit a DB sentinel (that is the writer's layer, and the parser holds
     no writer handle - import discipline).
  4. Extracts: base_classes_ordered (MRO order, HIGH-1), test_type, @tagged args,
     @standalone -> commit_allowed, setUp/setUpClass refs, def-use field resolution.
  5. EVERY ClassDef in a test file emits a TestClassInfo node (HIGH-1):
     TEST_BASE_CLASSES CLASSIFIES, never GATES emission.

Import discipline: imports only models, parser_util, ast (never src.mcp / writer_* /
resolver / registry). This satisfies tests/test_pipeline_import_discipline.py.

Reuse citations:
  - AST parse: parser_util.parse_external_source (parser_util.py:65)
  - era2 ClassDef walk via tree.body: pattern at parser_python.py:836
  - base-name extraction (set; we write a NEW ordered variant):
    parser_python._get_base_class_names (:523)
  - string-literal extract: parser_python._extract_string (:447) - reimplemented locally
  - decorator walk: node.decorator_list loop (:734)
  - docstring: ast.get_docstring (:760)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from .models import ModuleInfo, TestClassInfo, TestHelperInfo, TestMethodInfo, TestParseResult
from .parser_util import parse_external_source

# ---------------------------------------------------------------------------
# Framework base classification (classifies, never gates emission - HIGH-1)
# ---------------------------------------------------------------------------

TEST_BASE_CLASSES: frozenset[str] = frozenset({
    "TestCase",                                         # unittest (all eras)
    "BaseCase", "TreeCase",                             # odoo abstract (v10+/v14+)
    "TransactionCase", "SingleTransactionCase",         # all eras
    "SavepointCase",                                    # v8-v15 (merged into TransactionCase v16+)
    "HttpCase", "HttpCaseCommon", "HttpSavepointCase",  # v8+/v14
    "Form", "O2MForm",                                  # form helpers v14+
})

TEST_TYPE_MAP: dict[str, str] = {
    "TransactionCase": "transaction",
    "SavepointCase": "savepoint",
    "SingleTransactionCase": "single_transaction",
    "HttpCase": "http",
    "HttpCaseCommon": "http",
    "HttpSavepointCase": "http",
    "Form": "form",
    "O2MForm": "form",
    "TestCase": "unittest",
}

# Known framework bases that carry commit_allowed=True semantics.
# Note: @standalone decorator -> commit_allowed=True regardless of base class.
# SingleTransactionCase stays False (forbidden, must open new cursor if needed).
_FRAMEWORK_BASES: dict[str, dict] = {
    "TransactionCase": {
        "test_type": "transaction",
        "commit_allowed": False,
        "setup_summary": ["savepoint per method, auto-rollback on teardown"],
    },
    "SavepointCase": {
        "test_type": "savepoint",
        "commit_allowed": False,
        "setup_summary": ["savepoint wrapper (deprecated alias, v8-v15)"],
    },
    "SingleTransactionCase": {
        "test_type": "single_transaction",
        "commit_allowed": False,
        "setup_summary": [
            "one transaction for all methods, no savepoint; open new cursor if commit needed",
        ],
    },
    "HttpCase": {
        "test_type": "http",
        "commit_allowed": False,
        "setup_summary": [
            "TransactionCase + Chrome headless + start_tour(); tag @tagged('post_install')",
        ],
    },
    "HttpCaseCommon": {
        "test_type": "http",
        "commit_allowed": False,
        "setup_summary": ["mixin providing HTTP test utilities"],
    },
    "HttpSavepointCase": {
        "test_type": "http",
        "commit_allowed": False,
        "setup_summary": ["HTTP + savepoint variant"],
    },
    "Form": {
        "test_type": "form",
        "commit_allowed": False,
        "setup_summary": ["server-side onchange/default simulation via Form(env['model'])"],
    },
    "O2MForm": {
        "test_type": "form",
        "commit_allowed": False,
        "setup_summary": ["o2m field form simulation"],
    },
    "TestCase": {
        "test_type": "unittest",
        "commit_allowed": False,
        "setup_summary": ["stdlib unittest.TestCase, no Odoo ORM"],
    },
    "BaseCase": {
        "test_type": "transaction",
        "commit_allowed": False,
        "setup_summary": ["abstract Odoo base test class (v10+)"],
    },
    "TreeCase": {
        "test_type": "transaction",
        "commit_allowed": False,
        "setup_summary": ["abstract tree/hierarchy test case (v14+)"],
    },
}

# ---------------------------------------------------------------------------
# era1 regex (reuse class-head pattern from parser_python.py:30 region)
# ---------------------------------------------------------------------------

_RE_CLASS_HEAD_ERA1 = re.compile(
    r"^class\s+(\w+)\s*\(([^)]+)\)\s*:",
    re.MULTILINE,
)
_RE_SELF_ENV_ERA1 = re.compile(r"self\.env\['([^']+)'\]")
_RE_SELF_ENV_ERA1_DQ = re.compile(r'self\.env\["([^"]+)"\]')


# ---------------------------------------------------------------------------
# era2 AST helpers
# ---------------------------------------------------------------------------

def _extract_string(node: ast.expr) -> str | None:
    """Extract string constant from AST node (mirrors parser_python._extract_string :447)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _build_import_alias_map(tree: ast.Module) -> dict[str, str]:
    """Map a locally-bound alias -> the real imported NAME (LOW-3 accept-and-resolve).

    Covers ``from x.y import TestProjectProfitabilityCommon as Common`` (the real
    odoo17 sale_project case) -> {'Common': 'TestProjectProfitabilityCommon'} and
    ``import a.b.Common as C`` -> {'C': 'Common'} (last dotted segment). Without this
    a ``class Foo(Common)`` base resolves to the non-existent node 'Common' and the
    INHERITS_TEST edge silently dangles. Resolving through the alias recovers the
    real base name so reconcile_test_inherits can match it.
    """
    alias_map: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:  # `import X as Y`
                    alias_map[alias.asname] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    # `import a.b.c as Y` -> bind Y to last dotted segment
                    alias_map[alias.asname] = alias.name.split(".")[-1]
    return alias_map


def _get_base_classes_ordered(
    cls_node: ast.ClassDef,
    alias_map: dict[str, str] | None = None,
) -> list[str]:
    """Return ORDERED list of simple base class names, preserving Python MRO declaration order.

    This is a NEW helper (LOW-1) - NOT the existing set-returning _get_base_class_names
    from parser_python.py:523 which loses ordering. We need MRO order to classify
    test_type from the FIRST matching base (HIGH-1).

    Handles ast.Name (simple name) and ast.Attribute (qualified like common.TransactionCase).
    For ast.Attribute, uses the short name (attr) for resolution, as per HIGH-1 recommendation.

    LOW-3: a base bound via ``import X as Common`` is resolved back to its real name
    (X) through ``alias_map`` so the INHERITS_TEST edge resolves instead of dangling.
    """
    _aliases = alias_map or {}
    result: list[str] = []
    seen: set[str] = set()
    for base in cls_node.bases:
        if isinstance(base, ast.Name):
            name = base.id
        elif isinstance(base, ast.Attribute):
            # e.g. common.TransactionCase -> use 'TransactionCase'
            name = base.attr
        else:
            continue
        # LOW-3: rewrite aliased base to its real imported name when known.
        name = _aliases.get(name, name)
        if name not in seen:
            result.append(name)
            seen.add(name)
    return result


def _classify_test_type(base_classes_ordered: list[str]) -> str:
    """Return test_type from first base in MRO order that maps in TEST_TYPE_MAP (HIGH-1).

    Returns 'unknown' when no framework base is recognized. The reconcile pass
    resolves inherited test_type in a post-pass for classes that only inherit
    indirectly through addon common bases.
    """
    for base in base_classes_ordered:
        t = TEST_TYPE_MAP.get(base)
        if t is not None:
            return t
    return "unknown"


def _extract_tagged_args(decorator_list: list[ast.expr]) -> list[str]:
    """Extract @tagged(...) args as raw strings including '-tag' negative entries (MISSED).

    Also detects @standalone -> signals commit_allowed=True (PP3).
    Returns (tagged_list, is_standalone).
    """
    tagged: list[str] = []
    is_standalone = False
    for dec in decorator_list:
        # @standalone (bare name)
        if isinstance(dec, ast.Name) and dec.id == "standalone":
            is_standalone = True
        # @tagged(...) or @api.standalone etc.
        if isinstance(dec, ast.Call):
            func = dec.func
            func_name = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name == "tagged":
                for arg in dec.args:
                    s = _extract_string(arg)
                    if s is not None:
                        tagged.append(s)
            if func_name == "standalone":
                is_standalone = True
    return tagged, is_standalone


def _extract_model_refs_from_env(stmts: list[ast.stmt]) -> list[str]:
    """Walk AST statements for self.env['model.name'] or cls.env['model.name'] patterns.

    Accepts both `self.env` (instance methods) and `cls.env` (classmethod setUp).
    Returns a deduplicated list of model name strings. This is the primary
    coverage signal used for COVERS_MODEL edges.
    """
    refs: set[str] = set()
    for node in ast.walk(ast.Module(body=stmts, type_ignores=[])):
        # Pattern: self.env['model'] / cls.env['model'] / self.env["model"] / cls.env["model"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in ("self", "cls")
            and node.value.attr == "env"
        ):
            s = _extract_string(node.slice)
            if s and "." in s:
                refs.add(s)
    return sorted(refs)


# L1: framework/test-harness attrs that are never Odoo Field names. self.env,
# self.cr, self.assertEqual(...) receivers etc. flooded field_refs with noise;
# COVERS_FIELD reconcile filters them out anyway (no dangling edges) but the stored
# list diluted the def-use precision signal. Filter at source instead.
_NON_FIELD_SELF_ATTRS: frozenset[str] = frozenset({
    "env", "cr", "uid", "pool", "registry", "browse", "ref", "user", "users",
    "company", "partner_id_dummy", "maxDiff", "longMessage",
})


def _extract_self_attr_refs(stmts: list[ast.stmt]) -> list[str]:
    """Extract self.<attr> attribute access names (mirrors parser_python.py:764-774).

    L1: filters out dunder attrs, ``assert*``/``expect`` test-harness method names,
    and known framework attrs (``self.env``, ``self.cr`` ...). These are never Odoo
    Field names; including them only added noise (the COVERS_FIELD reconcile already
    drops non-Field refs, but a clean list keeps the precision signal honest).

    Returns sorted list of candidate field attribute names.
    """
    refs: set[str] = set()
    for node in ast.walk(ast.Module(body=stmts, type_ignores=[])):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            attr = node.attr
            if attr.startswith("_"):
                continue  # dunder / private harness attrs
            if attr.startswith("assert") or attr == "expect":
                continue  # test-harness method receivers, not fields
            if attr in _NON_FIELD_SELF_ATTRS:
                continue  # known framework attrs
            refs.add(attr)
    return sorted(refs)


def _count_asserts(stmts: list[ast.stmt]) -> int:
    """Count self.assert*/assertEqual/assertIn/assertFalse/assertTrue/expect calls.

    L2: this is a documented LOWER BOUND used only for RANKING (more asserts ->
    higher in tests_covering), not an exact metric. It counts direct
    ``self.assert*`` / ``self.expect`` calls (incl. ``with self.assertRaises(...)``,
    which is a Call on self). It deliberately does NOT chase aliased receivers
    (``ae = self.assertEqual; ae(...)``) - rare and not worth the false-precision.
    """
    count = 0
    for node in ast.walk(ast.Module(body=stmts, type_ignores=[])):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
                if name.startswith("assert") or name == "expect":
                    # Only count when receiver is self or self.something
                    val = func.value
                    if isinstance(val, ast.Name) and val.id == "self":
                        count += 1
    return count


def _build_def_use_map(setup_body: list[ast.stmt]) -> dict[str, str]:
    """Build a def-use map from setUp/setUpClass body for self.<attr> -> model assignments.

    Resolves patterns like:
        self.order = self.env['sale.order'].create(...)
        cls.partner = cls.env['res.partner'].browse(...)
    Returns mapping {attr_name: model_name}.
    This enables field-level coverage: self.order.amount_total -> COVERS_FIELD amount_total.
    (HIGH-2 def-use pass, owner-approved)
    """
    def_use: dict[str, str] = {}
    for stmt in setup_body:
        if not isinstance(stmt, ast.Assign | ast.AnnAssign):
            continue
        # Handle both Assign and AnnAssign
        targets = []
        value_node = None
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
            value_node = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = [stmt.target]
            value_node = stmt.value
        if value_node is None:
            continue
        for tgt in targets:
            # self.order = ... or cls.order = ...
            if not (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)):
                continue
            attr_name = tgt.attr
            # Walk value for self.env['model'].create/browse/search/... pattern
            model_name = _resolve_env_subscript_in_call(value_node)
            if model_name:
                def_use[attr_name] = model_name
    return def_use


def _resolve_env_subscript_in_call(node: ast.expr) -> str | None:
    """Find env['model.name'] subscript anywhere in a call chain like self.env['m'].create(...)."""
    # Direct subscript: self.env['model']
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.attr == "env"
    ):
        s = _extract_string(node.slice)
        if s and "." in s:
            return s
    # Call chain: self.env['model'].create(...)
    if isinstance(node, ast.Call):
        # Try func.value (the object being called on)
        if isinstance(node.func, ast.Attribute):
            return _resolve_env_subscript_in_call(node.func.value)
    # Chained attribute: foo.bar -> check foo
    if isinstance(node, ast.Attribute):
        return _resolve_env_subscript_in_call(node.value)
    return None


def _resolve_field_refs_with_def_use(
    stmts: list[ast.stmt],
    def_use: dict[str, str],
) -> list[tuple[str, str]]:
    """Return [(model_name, field_name)] by resolving self.<attr>.<field> chains via def-use map.

    Given def_use={'order': 'sale.order'} and a statement that accesses self.order.amount_total,
    produces [('sale.order', 'amount_total')].
    """
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for node in ast.walk(ast.Module(body=stmts, type_ignores=[])):
        # Pattern: self.<attr>.<field>
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        ):
            attr_name = node.value.attr
            field_name = node.attr
            model_name = def_use.get(attr_name)
            if model_name and not field_name.startswith("_"):
                pair = (model_name, field_name)
                if pair not in seen:
                    result.append(pair)
                    seen.add(pair)
    return result


def _is_test_file(file_path: str) -> bool:
    """Return True if file_path is a Python file we should parse for test classes.

    C3 fix: index EVERY ``.py`` file under a ``tests/`` directory - not just
    ``test_*.py`` / ``*_test.py``. The Odoo addon common base/helper classes
    (``SaleCommon``, ``MailCommon``, ``AccountTestInvoicingCommon``,
    ``TestSaleCommonBase``) live in ``tests/common.py`` (NOT a ``test_`` file) and
    ~90% of real test classes inherit them; excluding ``common.py`` left every
    INHERITS_TEST edge dangling on real source. HIGH-1's "every ClassDef emits a
    node" makes this safe (TEST_BASE_CLASSES classifies, never gates emission).

    The ``test_*`` / ``*_test.py`` name guard is RETAINED for ``.py`` files NOT
    under a ``tests/`` dir (rare modules that drop a ``test_foo.py`` at module
    root) so non-test production code is never mis-parsed.
    """
    p = Path(file_path)
    name = p.name
    if not name.endswith(".py") or name == "__init__.py":
        return False
    parts = p.parts
    under_tests_dir = any(part == "tests" for part in parts)
    if under_tests_dir:
        # Inside tests/ -> parse ALL .py (incl common.py, test_common.py, etc.)
        return True
    # Outside a tests/ dir -> keep the conservative name guard so production
    # source is never parsed as a test file.
    return name.startswith("test_") or name.endswith("_test.py")


# ---------------------------------------------------------------------------
# era2 (v10+) full AST extraction
# ---------------------------------------------------------------------------

def _parse_era2_test_file(
    source: str,
    file_path: str,
    module_info: ModuleInfo,
) -> list[TestClassInfo]:
    """Parse one test file using full AST (era2, v10+).

    Returns a list of TestClassInfo for EVERY ClassDef in the file (HIGH-1).
    TEST_BASE_CLASSES classifies, never gates emission.
    """
    try:
        tree = parse_external_source(source, filename=file_path)
    except SyntaxError:
        return []

    result: list[TestClassInfo] = []
    mod = module_info.name
    ver = module_info.odoo_version
    # Relativize file_path to repo root (ADR-0037)
    rel_fp = module_info.relative_path(file_path)

    # LOW-3: file-level import-alias map so `class Foo(Common)` where
    # `... import TestXCommon as Common` resolves to the real base name.
    alias_map = _build_import_alias_map(tree)

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue

        base_classes_ordered = _get_base_classes_ordered(node, alias_map)
        test_type = _classify_test_type(base_classes_ordered)
        docstring = ast.get_docstring(node)

        # Extract @tagged args and @standalone from class decorators (mirrors parser_python.py:734)
        tagged, is_standalone = _extract_tagged_args(node.decorator_list)
        commit_allowed = is_standalone

        # Build per-method info
        # First pass: find setUp/setUpClass bodies for class-scope ref extraction
        setup_bodies: list[ast.stmt] = []
        setup_def_use: dict[str, str] = {}
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and child.name in ("setUp", "setUpClass"):
                setup_bodies.extend(child.body)

        if setup_bodies:
            setup_def_use = _build_def_use_map(setup_bodies)

        # Compute class-scope model refs (from setUp)
        class_scope_model_refs = _extract_model_refs_from_env(setup_bodies) if setup_bodies else []

        methods: list[TestMethodInfo] = []
        has_test_methods = False

        for child in node.body:
            if not isinstance(child, ast.FunctionDef):
                continue
            method_name = child.name
            method_tagged, _method_standalone = _extract_tagged_args(child.decorator_list)

            method_stmts = child.body
            method_model_refs = _extract_model_refs_from_env(method_stmts)
            # Merge class-scope model refs (setUp fixture coverage propagated to all methods)
            all_model_refs = sorted(set(class_scope_model_refs) | set(method_model_refs))

            # field_refs from self.<attr> access in method body
            field_refs = _extract_self_attr_refs(method_stmts)

            # def-use field resolution (HIGH-2): self.<attr>.<field> from setUp def-use map
            field_model_pairs = _resolve_field_refs_with_def_use(method_stmts, setup_def_use)
            # flatten field names for field_refs (these are additional specific fields)
            for _model, fname in field_model_pairs:
                if fname not in field_refs:
                    field_refs.append(fname)
            field_refs = sorted(set(field_refs))

            # Determine via tag for coverage edges
            is_setup = method_name in ("setUp", "setUpClass", "tearDown", "tearDownClass")
            via = "setup" if is_setup else ("assert" if method_name.startswith("test_") else "body")

            asserts_count = _count_asserts(method_stmts)

            try:
                src = ast.get_source_segment(source, child)
            except Exception:  # noqa: BLE001
                src = None

            if method_name.startswith("test"):
                has_test_methods = True

            methods.append(TestMethodInfo(
                name=method_name,
                test_class=node.name,
                module=mod,
                file_path=rel_fp,
                odoo_version=ver,
                tagged=method_tagged,
                docstring=ast.get_docstring(child),
                field_refs=field_refs,
                model_refs=all_model_refs,
                method_refs=[],
                asserts_count=asserts_count,
                via=via,
                line=child.lineno,
                source_code=src,
            ))

        defines_no_test_methods = not has_test_methods

        result.append(TestClassInfo(
            name=node.name,
            module=mod,
            file_path=rel_fp,
            odoo_version=ver,
            test_type=test_type,
            base_classes_ordered=base_classes_ordered,
            tagged=tagged,
            commit_allowed=commit_allowed,
            defines_no_test_methods=defines_no_test_methods,
            is_helper=False,  # finalized in reconcile pass
            docstring=docstring,
            line=node.lineno,
            methods=methods,
        ))

    return result


# ---------------------------------------------------------------------------
# era1 (v8/v9) degraded regex path
# ---------------------------------------------------------------------------

def _parse_era1_test_file_degraded(
    source: str,
    file_path: str,
    module_info: ModuleInfo,
) -> list[TestClassInfo]:
    """Degraded regex path for v8/v9 source (era1).

    Emits TestClassInfo with test_type='unknown' for every class found.
    Never crashes, never silently drops. The 'unknown' test_type on every node IS
    the degradation marker - queryable directly, no separate DB sentinel needed
    (the parser holds no writer handle; emitting SpecMetadata is the writer's job).
    (Design §2.4 and plan "era1 no-crash" test PP)
    """
    result: list[TestClassInfo] = []
    mod = module_info.name
    ver = module_info.odoo_version
    rel_fp = module_info.relative_path(file_path)

    for m in _RE_CLASS_HEAD_ERA1.finditer(source):
        class_name = m.group(1)
        # M2: strip the qualifier so era1 base names match era2's short-name form
        # used by reconcile_test_inherits (which resolves by simple TestHelper/
        # TestClass.name). Without this, era1 'openerp.tests.HttpCase' never resolves
        # to the seeded 'HttpCase' TestHelper. Take the last dotted segment; drop
        # any generic/keyword args (e.g. metaclass=...) defensively.
        bases_raw: list[str] = []
        for b in m.group(2).split(","):
            b = b.strip()
            if not b or "=" in b:  # skip metaclass=... / keyword bases
                continue
            short = b.split(".")[-1].strip()
            if short and short not in bases_raw:
                bases_raw.append(short)

        result.append(TestClassInfo(
            name=class_name,
            module=mod,
            file_path=rel_fp,
            odoo_version=ver,
            test_type="unknown",
            base_classes_ordered=bases_raw,
            tagged=[],
            commit_allowed=False,
            defines_no_test_methods=True,
            is_helper=False,
            docstring=None,
            line=None,
            methods=[],
        ))

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_module(module_info: ModuleInfo) -> TestParseResult:
    """Parse all test files in a module directory, returning a TestParseResult.

    Era dispatch:
    - v8/v9 (major <= 9): degraded regex path for every test file.
    - v10+ (major >= 10): full AST path.

    Never crashes on individual file errors; logs and skips.
    """
    import logging
    _logger = logging.getLogger(__name__)

    major = _version_major(module_info.odoo_version)
    use_era2 = major >= 10

    mod_path = Path(module_info.path)
    test_classes: list[TestClassInfo] = []

    # Walk all Python files under tests/ directories of this module
    tests_dirs = list(mod_path.glob("tests"))
    if not tests_dirs:
        # Some modules put tests directly under the module root (less common)
        tests_dirs = [mod_path]

    for tests_dir in tests_dirs:
        if not tests_dir.is_dir():
            continue
        for py_file in sorted(tests_dir.rglob("*.py")):
            fp_str = str(py_file)
            if not _is_test_file(fp_str):
                continue
            try:
                source = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                _logger.warning("parser_test: cannot read %s: %s", fp_str, exc)
                continue

            try:
                if use_era2:
                    classes = _parse_era2_test_file(source, fp_str, module_info)
                else:
                    classes = _parse_era1_test_file_degraded(source, fp_str, module_info)
                test_classes.extend(classes)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "parser_test: error parsing %s (era=%s): %s — skipping file",
                    fp_str, "era2" if use_era2 else "era1", exc,
                )
                continue

    return TestParseResult(
        module=module_info,
        test_classes=test_classes,
        test_helpers=[],  # addon TestHelper nodes finalized in reconcile
        js_suites=[],     # WI-3 populates these
    )


def _version_major(odoo_version: str) -> int:
    """Extract major version number from version string like '17.0' or '99.0'."""
    try:
        return int(odoo_version.split(".")[0])
    except (ValueError, IndexError):
        return 10  # safe default: assume era2


# ---------------------------------------------------------------------------
# Framework base seeding (called from parser_odoo_core during odoo/tests/ walk)
# ---------------------------------------------------------------------------

def seed_framework_helpers(odoo_version: str) -> list[TestHelperInfo]:
    """Return TestHelperInfo nodes for Odoo framework test bases (per-version seeding).

    These are seeded from _FRAMEWORK_BASES (known across all versions) rather than
    parsed at runtime, because framework bases live in odoo/tests/common.py which
    may not always be accessible. Using module='@framework' (MED-3) avoids confusion
    with the '__unresolved__' GC placeholder.

    Called from parser_odoo_core._parse_odoo_tests_for_helpers() during the core walk.
    Returns a list; caller (writer_neo4j.write_test_results) persists them as
    TestHelper nodes with NO DEFINED_IN edge (MED-3).
    """
    return [
        TestHelperInfo(
            name=name,
            module="@framework",
            odoo_version=odoo_version,
            origin="framework",
            test_type=props["test_type"],
            setup_summary=props["setup_summary"],
            commit_allowed=props["commit_allowed"],
            file_path=None,
            line=None,
        )
        for name, props in _FRAMEWORK_BASES.items()
    ]
