# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_js_test.py
"""Extract JsTestSuite info from Odoo frontend test files (WI-3).

Covers three framework families:
  - Hoot (v18+): identified by `import ... from '@odoo/hoot'` or `from "@odoo/hoot"`.
  - QUnit (pre-v18): identified by `QUnit.` usage without a hoot import.
  - Tour (both eras): identified by `registry.category("web_tour.tours")` or
    `web_tour.tour` usage (MISSED-4 fix: target is web_tour.tours registry, not OWLComp).

Scope: file-level extraction (one JsTestSuite node per file, §4.4).
  - describe_blocks: describe()/QUnit.module() title strings.
  - test_names: test()/QUnit.test() title strings.
  - tags: describe.current.tags(...) or test.tags(...) values (Hoot); empty for tour.
  - mounts: resModel= values from mountView({resModel: '...'}) calls.
  - mock_models: _name values from `class X extends models.Model { _name = '...' }` +
    defineModels([...]) arg class names.

MED-1 contract: NO JsTestSuite-[:COVERS_MODEL]->Model edge is emitted from mock_models.
  The mock_models field captures test-double model names only — they are hand-rolled
  fixtures, not real Odoo models. Writer must NOT emit a COVERS_MODEL edge for them.

Import discipline: imports only models (never src.mcp / writer_* / resolver / registry).
This satisfies tests/test_pipeline_import_discipline.py.

Parsing strategy: lightweight regex over JS source (no JS AST available in Python).
  Mirrors how the existing parser_js.py handles JS. All regex patterns are compiled
  once at module load (loop-invariant constants).
"""
from __future__ import annotations

import re
from pathlib import Path

from .models import JsTestSuiteInfo, ModuleInfo

# ---------------------------------------------------------------------------
# Compiled regex patterns (loop-invariant constants, mirrors parser_js.py style)
# ---------------------------------------------------------------------------

# Framework disambiguation (priority order: hoot > qunit > tour)
_RE_HOOT_IMPORT = re.compile(
    r"""(?:import\s+[^;]+from\s+['"]@odoo/hoot['"])""",
    re.MULTILINE,
)
_RE_QUNIT_USAGE = re.compile(r"\bQUnit\s*\.", re.MULTILINE)
_RE_TOUR_REGISTRY = re.compile(
    r"""registry\.category\(\s*['"]web_tour\.tours['"]\s*\)""",
    re.MULTILINE,
)
_RE_TOUR_LEGACY = re.compile(r"\bweb_tour\.tour\b", re.MULTILINE)

# describe block titles: describe("title", ...) or describe('title', ...)
_RE_DESCRIBE = re.compile(
    r"""\bdescribe\s*\(\s*(?:"([^"\\]*)"|'([^'\\]*)')\s*,""",
    re.MULTILINE,
)

# test names: test("title") or test('title') or test.tags(...)("title")
_RE_TEST_CALL = re.compile(
    r"""\btest\s*(?:\.tags\([^)]*\))?\s*\(\s*(?:"([^"\\]*)"|'([^'\\]*)')\s*,""",
    re.MULTILINE,
)

# QUnit module: QUnit.module("title", ...) or QUnit.module('title', ...)
_RE_QUNIT_MODULE = re.compile(
    r"""\bQUnit\.module\s*\(\s*(?:"([^"\\]*)"|'([^'\\]*)')\s*""",
    re.MULTILINE,
)

# QUnit test: QUnit.test("title", ...) or QUnit.test('title', ...)
_RE_QUNIT_TEST = re.compile(
    r"""\bQUnit\.test\s*\(\s*(?:"([^"\\]*)"|'([^'\\]*)')\s*,""",
    re.MULTILINE,
)

# tags: describe.current.tags("tag1", "tag2") or test.tags("tag1")
# Captures the entire argument string between the parens
_RE_TAGS_BLOCK = re.compile(
    r"""(?:describe\.current\.tags|test\.tags)\s*\(\s*(.*?)\s*\)""",
    re.MULTILINE | re.DOTALL,
)

# Individual string inside a tags(...) argument
_RE_STRING_LITERAL = re.compile(r"""["']([^"'\\]*)["']""")

# mountView({..., resModel: 'model.name', ...})
_RE_MOUNT_VIEW = re.compile(
    r"""resModel\s*:\s*(?:"([^"\\]*)"|'([^'\\]*)')""",
    re.MULTILINE,
)

# class X extends models.Model { _name = "model.name" }
# Matches the class body block for _name assignment (simplified)
_RE_MODEL_CLASS_NAME = re.compile(
    r"""class\s+\w+\s+extends\s+models\.Model\s*\{[^}]*?_name\s*=\s*["']([^"'\\]+)["']""",
    re.MULTILINE | re.DOTALL,
)

# Tour name: .add("tour_name", { ... }) from registry.category("web_tour.tours").add(...)
_RE_TOUR_ADD = re.compile(
    r"""\.add\s*\(\s*(?:"([^"\\]*)"|'([^'\\]*)')\s*,""",
    re.MULTILINE,
)

# Files we consider JS test files (mirrors Odoo convention)
_TEST_SUFFIXES = frozenset({".test.js", "_tests.js"})
# Directories that contain frontend tests
_TEST_DIRS = frozenset({"tests", "test"})
# Tour files live in tests/tours/ subdirectory
_TOUR_SUBDIR = "tours"


def _is_js_test_file(path: str) -> bool:
    """Return True if path matches a frontend test file convention.

    Hoot (v18+): *.test.js under static/tests/
    QUnit (pre-v18): *_tests.js under static/tests/
    Tour: any .js file under static/tests/tours/
    """
    p = Path(path)
    # Must have .js suffix
    if p.suffix != ".js":
        return False

    parts = p.parts
    # Must be under static/tests/ somewhere
    has_static = "static" in parts
    has_tests = any(part in _TEST_DIRS for part in parts)
    if not (has_static and has_tests):
        return False

    # Tour: under .../tours/
    if _TOUR_SUBDIR in parts:
        return True

    # Hoot or QUnit by suffix
    name = p.name
    return name.endswith(".test.js") or name.endswith("_tests.js")


def _detect_framework(source: str) -> str:
    """Detect JS test framework from source content.

    Priority: hoot > qunit > tour.
    Returns 'hoot', 'qunit', or 'tour'.
    Fallback: 'unknown' when none detected (should not happen for recognized test files).
    """
    # Hoot: import from '@odoo/hoot' or "@odoo/hoot"
    if _RE_HOOT_IMPORT.search(source):
        return "hoot"

    # Tour: registry.category("web_tour.tours") or web_tour.tour
    if _RE_TOUR_REGISTRY.search(source) or _RE_TOUR_LEGACY.search(source):
        return "tour"

    # QUnit: QUnit. usage
    if _RE_QUNIT_USAGE.search(source):
        return "qunit"

    return "unknown"


def _extract_describe_blocks(source: str, framework: str) -> list[str]:
    """Extract describe block / module title strings."""
    titles: list[str] = []
    if framework in ("hoot", "unknown"):
        for m in _RE_DESCRIBE.finditer(source):
            title = m.group(1) if m.group(1) is not None else m.group(2)
            if title:
                titles.append(title)
    if framework in ("qunit", "unknown"):
        for m in _RE_QUNIT_MODULE.finditer(source):
            title = m.group(1) if m.group(1) is not None else m.group(2)
            if title:
                titles.append(title)
    return titles


def _extract_test_names(source: str, framework: str) -> list[str]:
    """Extract test() / QUnit.test() title strings."""
    names: list[str] = []
    if framework in ("hoot", "unknown"):
        for m in _RE_TEST_CALL.finditer(source):
            name = m.group(1) if m.group(1) is not None else m.group(2)
            if name:
                names.append(name)
    if framework in ("qunit", "unknown"):
        for m in _RE_QUNIT_TEST.finditer(source):
            name = m.group(1) if m.group(1) is not None else m.group(2)
            if name:
                names.append(name)
    return names


def _extract_tags(source: str) -> list[str]:
    """Extract tag strings from describe.current.tags(...) and test.tags(...)."""
    tags: list[str] = []
    seen: set[str] = set()
    for block_m in _RE_TAGS_BLOCK.finditer(source):
        arg_str = block_m.group(1)
        for tag_m in _RE_STRING_LITERAL.finditer(arg_str):
            tag = tag_m.group(1)
            if tag and tag not in seen:
                tags.append(tag)
                seen.add(tag)
    return tags


def _extract_mounts(source: str) -> list[str]:
    """Extract resModel values from mountView({resModel: '...'}) calls."""
    mounts: list[str] = []
    seen: set[str] = set()
    for m in _RE_MOUNT_VIEW.finditer(source):
        model = m.group(1) if m.group(1) is not None else m.group(2)
        if model and model not in seen:
            mounts.append(model)
            seen.add(model)
    return mounts


def _extract_mock_models(source: str) -> list[str]:
    """Extract mock model _name strings (MED-1: these are test-doubles, not real models).

    M3 fix: capture ONLY the dotted ``_name`` string values from
    ``class X extends models.Model {{ _name = "model.name" }}`` - NOT the JS class
    identifier. Previously ``defineModels([Account, Partner])`` leaked the bare
    class names ``Account``/``Partner`` into ``mock_models`` alongside the real
    ``_name`` (``account.account``), polluting the property. The class identifier
    is never an Odoo model name; the authoritative signal is the ``_name`` literal.
    """
    mock_names: list[str] = []
    seen: set[str] = set()

    # Only pattern: inline class with `_name = "dotted.model"` assignment.
    # An Odoo model _name is always dotted; bare identifiers are class names (noise).
    for m in _RE_MODEL_CLASS_NAME.finditer(source):
        name = m.group(1)
        if name and "." in name and name not in seen:
            mock_names.append(name)
            seen.add(name)

    return mock_names


def _extract_tour_names(source: str) -> list[str]:
    """Extract tour name strings from registry.category('web_tour.tours').add('name', ...)."""
    names: list[str] = []
    seen: set[str] = set()
    for m in _RE_TOUR_ADD.finditer(source):
        name = m.group(1) if m.group(1) is not None else m.group(2)
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def parse_js_test_file(
    abs_path: str,
    module: str,
    odoo_version: str,
    repo_root: str | None = None,
) -> JsTestSuiteInfo | None:
    """Parse a single JS test file and return a JsTestSuiteInfo.

    Returns None if the file is not a recognized test file or cannot be read.

    `file_path` is stored repo-relative when repo_root is provided (ADR-0037).
    """
    p = Path(abs_path)
    if not _is_js_test_file(abs_path):
        return None

    try:
        source = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Relativize path (ADR-0037)
    file_path: str
    if repo_root is not None:
        try:
            file_path = str(p.relative_to(repo_root))
        except ValueError:
            file_path = abs_path
    else:
        file_path = abs_path

    framework = _detect_framework(source)

    if framework == "tour":
        # Tour files: describe_blocks/test_names hold tour names from .add(...)
        tour_names = _extract_tour_names(source)
        return JsTestSuiteInfo(
            file_path=file_path,
            module=module,
            odoo_version=odoo_version,
            framework="tour",
            describe_blocks=[],
            test_names=tour_names,  # store tour names in test_names for discoverability
            tags=[],
            mounts=[],
            mock_models=[],
            line=1,
        )

    describe_blocks = _extract_describe_blocks(source, framework)
    test_names = _extract_test_names(source, framework)
    tags = _extract_tags(source)
    mounts = _extract_mounts(source)
    mock_models = _extract_mock_models(source)

    return JsTestSuiteInfo(
        file_path=file_path,
        module=module,
        odoo_version=odoo_version,
        framework=framework,
        describe_blocks=describe_blocks,
        test_names=test_names,
        tags=tags,
        mounts=mounts,
        mock_models=mock_models,
        line=1,
    )


def parse_module_js_tests(info: ModuleInfo) -> list[JsTestSuiteInfo]:
    """Scan a module's static/tests/ directory for JS test files and return JsTestSuiteInfos.

    Returns an empty list when the module has no static/tests/ directory or no
    recognized JS test files. Never raises — a read error on one file is silently
    skipped (logged at DEBUG) so a single bad file does not abort the whole module.

    Called from pipeline_repo._index_repo alongside parser_test.parse_module.
    """
    import logging  # local import — keeps top-level clean
    _log = logging.getLogger(__name__)

    results: list[JsTestSuiteInfo] = []
    module_path = Path(info.path)
    static_tests = module_path / "static" / "tests"
    if not static_tests.is_dir():
        return results

    repo_root = str(info.repo_root) if info.repo_root else None
    version = info.odoo_version

    # Walk static/tests/ recursively — includes tours/ subdirectory
    for js_file in sorted(static_tests.rglob("*.js")):
        suite = parse_js_test_file(
            str(js_file),
            module=info.name,
            odoo_version=version,
            repo_root=repo_root,
        )
        if suite is not None:
            results.append(suite)
            _log.debug(
                "JS test: %s [%s] framework=%s tests=%d",
                suite.file_path, info.name, suite.framework, len(suite.test_names),
            )

    return results
