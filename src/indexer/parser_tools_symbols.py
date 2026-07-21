# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_tools_symbols.py
"""odoo.tools.* symbol data: curated loader + independent AST/text-regex oracle.

TWO INDEPENDENT HALVES (issue #364)
------------------------------------
1. ``_load_static_tools_symbols`` / ``load_tools_symbols`` (ADR-0033) — loads
   the curated ``tools_symbols_<version>.json`` files. Unchanged by this
   module's Part 2 below; this is what the pipeline actually writes to the
   graph today.
2. ``parse_tools_symbols`` (this issue's deliverable, mirrors
   ``framework_bases.parse_framework_bases``'s shape, ADR-0054) — an
   INDEPENDENT AST/text-regex oracle that recovers the real, live
   ``odoo.tools.*`` public symbol surface directly from a checkout, so
   ``tests/test_tools_symbols_content_parity.py`` can diff the curated JSON
   against real source as a drift alarm. It does NOT feed the pipeline (not
   wired into ``load_tools_symbols`` or ``pipeline.py`` — that wiring, if ever
   wanted, is a separate, deliberately out-of-scope decision, matching how
   ``parse_framework_bases`` existed for a full issue before #362 wired it
   into ``framework_bases()`` for enrichment).

WHY A NAIVE ``tree.body`` WALK UNDER-RECOVERS (phase2-C audit, section 2)
--------------------------------------------------------------------------
``odoo/tools/__init__.py`` does not define most of its own public surface —
it re-exports it from sibling submodule files via ``from .X import *`` /
``from .X import a, b`` / ``from . import X``. Two concrete shapes a
``tree.body``-only walk of ``__init__.py`` alone would miss entirely:

1. **Module-scope ``if``/``else`` function definitions.** ``html_escape`` is
   bound this way in ``misc.py`` at v8, v9, v10, v11, v13 (real source,
   byte-identical shape at v8 and v13):

       if parse_version(getattr(werkzeug, '__version__', '0.0')) < parse_version('0.9.0'):
           def html_escape(text):
               return werkzeug.utils.escape(text, quote=True)
       else:
           def html_escape(text):
               return werkzeug.utils.escape(text)

   A walker that only inspects top-level ``tree.body`` sees the ``If`` node
   and nothing inside it. The fix: descend exactly ONE level into a
   module-scope ``If``'s ``body``/``orelse`` (never recursively — this
   oracle's inclusion rule is bounded, not an unbounded walk). At v15-19
   ``misc.py`` instead does a plain top-level ``html_escape = markupsafe.escape``
   — no ``if``/``else`` needed there, but a walker that only tracks
   SCREAMING_CASE ``Assign`` targets (a common convention for "this file's
   constants") would *still* miss it, because ``html_escape`` is lowercase.
   This oracle tracks every non-underscore top-level ``Assign``/``AnnAssign``
   target, not just uppercase ones.

2. **Import-based re-exports with no ``__all__``.** ``ustr`` is never itself
   a ``def``/``class``/assignment anywhere in ``misc.py`` — it arrives via
   ``from odoo.loglevels import get_encodings, ustr, exception_to_unicode``
   (``openerp.loglevels`` pre-v10) inside ``misc.py``. Because (through v17)
   ``misc.py`` defines no ``__all__``, real Python's ``from .misc import *``
   in ``__init__.py`` re-exports *every* non-underscore name bound in
   ``misc.py``'s namespace — including names ``misc.py`` itself only
   imported, not defined. A walker that only inspects
   ``FunctionDef``/``ClassDef``/``Assign`` misses this category entirely.
   This oracle treats every ``ImportFrom``'s bound names as contributing to
   the owning module's own public surface too (subject to that module's own
   ``__all__``, when it defines one — v18/v19 ``misc.py`` explicitly lists
   ``'ustr'`` in ``__all__``, so the direct-``__all__`` path also covers it
   there).

SUBMODULE SELF-BINDING (a THIRD real-Python effect, needed for ``pycompat``,
``date_utils``, ``js_transpiler``, ``config`` — all curated already)
------------------------------------------------------------------------------
``from .X import ...`` (star OR explicit — e.g. ``from . import pycompat``,
``from .date_utils import *``, ``from .js_transpiler import transpile_javascript``)
has a second, independent effect beyond whatever names it pulls in: Python's
import machinery sets the submodule ``X`` itself as an attribute of the
importing package as a side effect of ANY relative import reaching into it —
so ``odoo.tools.pycompat`` (the module), ``odoo.tools.date_utils`` (the
module), and ``odoo.tools.js_transpiler`` (the module) are all real,
independently resolvable attributes, regardless of whether ``X``'s own
members are separately flattened. This oracle binds the submodule's own stem
name (kind: "module", ``signature=None``) alongside whatever specific names
the same import statement pulls in — this is exactly how
``tools_symbols_11.0.json``'s ``odoo.tools.pycompat`` (Part A test:
``test_pycompat_correctly_present_from_v11``) and
``tools_symbols_1{2..9}.0.json``'s ``odoo.tools.date_utils`` are modeled.
One case where this self-binding is the *only* legitimate resolving path:
``from .config import config`` binds the local name ``config`` to
``config.py``'s ``config = configmanager()`` *instance* — that explicit
binding is what curated data means by ``odoo.tools.config`` (see its curated
``signature``: ``"odoo.tools.config.configmanager"``, an instance, not the
module) — so an explicit binding of the SAME name as the submodule always
wins over (never gets clobbered by) that submodule's own self-binding.

A submodule's bare name is ALSO exposed independently of ``__init__.py``'s
own import graph — see "BARE SUBMODULE EXPOSURE" below, which generalizes
this same fact one step further for a submodule ``__init__.py`` has stopped
referencing entirely (found live at v19: ``date_utils``/``js_transpiler``).

DOTTED ATTRIBUTE-PATH SYMBOLS (issue #364 Problem 2 — ``image_process`` at v19)
--------------------------------------------------------------------------------
``odoo/tools/image.py`` exists at every version v13-v19 and always resolves
as ``odoo.tools.image.image_process`` via ``from odoo.tools.image import
image_process`` — a fact that holds regardless of whatever
``odoo/tools/__init__.py`` chooses to re-export flatly. Through v18,
``__init__.py`` ALSO does ``from .image import image_process`` (a flat
re-export, so ``odoo.tools.image_process`` resolves too); at v19 that flat
re-export line was dropped (v19's own ``test_image.py`` imports via
``from odoo.tools import image as tools`` instead — the qualified-path form).
Rather than special-casing "image" by name, this oracle generally exposes
EVERY direct ``.py`` submodule file under ``<era>/tools/`` at its dotted
attribute path (``odoo.tools.<submodule>.<name>``) for every public
top-level name it defines — independent of, and in addition to, whatever the
flat-namespace resolution above finds. This is not a fallback invented to
pass one test: it is a real, always-valid Python import path for any
submodule file that exists, and the general mechanism also means a FUTURE
similar re-export drop at some other symbol is caught the same way, without
another hand-tuned special case. Scope is deliberately narrow: only direct
``.py`` files (not subdirectories like ``_vendor/``, ``pdf/``, ``zeep/``,
``data/``, ``arabic_reshaper/``, ``babel/`` — vendored/data payloads, never
curated as ``odoo.tools.*`` symbols) — and ``__all__`` does NOT gate this
step (``__all__`` only governs ``import *`` semantics, never an explicit
dotted/named import, so using the raw, unfiltered per-file scan here is the
semantically correct choice, not merely the simpler one).

BARE SUBMODULE EXPOSURE (live finding beyond the phase2-C audit's two named
problems — ``date_utils``/``js_transpiler`` at v19)
-------------------------------------------------------------------------------
The same reasoning behind DOTTED ATTRIBUTE-PATH SYMBOLS applies one notch
further: a submodule file's OWN bare name (not just its members) is ALSO
always a valid explicit import target (``from odoo.tools import <stem>``),
regardless of whether ``__init__.py`` currently references it. This oracle
therefore also exposes every direct submodule's bare stem as its own flat
symbol (``signature="module"``), deferring to a flat-namespace resolution
result of the same name when one exists (the ``config`` case above).

This is what makes ``odoo.tools.date_utils`` and ``odoo.tools.js_transpiler``
— both curated ``"stable"`` at v19 — actually recoverable there, and it
surfaced a THIRD live instance of the exact failure class Problem 1
(``pycompat``) and Problem 2 (``image_process``) already named: verified
directly against ``/home/tuan/git/odoo19``, ``odoo/tools/__init__.py`` no
longer references ``date_utils`` or ``js_transpiler`` in ANY form (no
``from . import``, no ``from .X import *``, no explicit name) even though
both files still exist on disk and remain genuinely imported elsewhere in
v19 core via their fully-qualified path (``odoo/orm/domains.py``:
``from odoo.tools.date_utils import parse_date, parse_iso_date``;
``odoo/orm/fields_temporal.py``: ``from odoo.tools import SQL, date_utils``;
``odoo/addons/base/models/assetsbundle.py``:
``from odoo.tools.js_transpiler import is_odoo_module`` /
``transpile_javascript``). The phase2-C audit named only ``image_process``
for this pattern; this oracle's own "curated ⊆ recovered" parity check is
what surfaced the other two — see this issue's final report rather than a
code comment for the recommended curated-data follow-up (this module does
not, and per the task brief must not, edit ``spec_data/*.json`` itself).

TWO-TIER PARSE (AST FIRST, TEXT-REGEX ON ``SyntaxError`` — v8/v9 ARE PYTHON 2)
---------------------------------------------------------------------------------
Reuses the SHAPE ``framework_bases.py``'s ``_scan_source`` / that module's own
docstring (and ``parser_python.py:993-1013``) already established for the
same v8/v9 hazard (bare ``except X, e:`` syntax, ``print`` statements, octal
literals — all fatal to a Python-3 ``ast.parse`` of the WHOLE file even when
the offending construct has nothing to do with the symbols this oracle
cares about). ``misc.py`` and ``image.py`` happen to parse cleanly under
Python 3.12 at every v8-v19 checkout on this machine (verified directly), so
neither ``html_escape`` nor ``ustr`` actually needs the fallback in practice
— but several OTHER star-imported v8/v9 submodules genuinely do
(``convert.py``, ``translate.py``, ``mail.py``, ``safe_eval.py``,
``float_utils.py``, ``yaml_import.py``, ``amount_to_text.py``,
``amount_to_text_en.py``, ``config.py``, ``test_reports.py``,
``parse_version.py`` all raise a real ``SyntaxError`` under Python 3.12 at
v8/v9), so the fallback path is genuinely exercised on this machine, not
just theoretical. Per this issue's brief: unlike ``framework_bases.py``'s
DEBUG-for-the-expected-case convention, EVERY fallback taken here — and
every version that ends up recovering zero symbols — logs at WARNING. A
silent zero is exactly the failure mode issue #364 exists to catch, so this
oracle never buries that signal at DEBUG.

Imports are intentionally minimal (``ast``, ``json``, ``logging``, ``re``,
``dataclasses``, ``pathlib``, ``.models``, ``..constants``, ``.parser_util``,
``.version_registry``) — this module sits in the parser layer of the one-way
pipeline (``scanner -> registry -> resolver -> parser -> ...``) and must
never import the writer, ``src.mcp``, or a Neo4j driver (enforced by
``tests/test_pipeline_import_discipline.py``).
"""
from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR
from .models import CoreSymbolInfo
from .parser_util import parse_external_source
from .version_registry import VersionRegistry

_logger = logging.getLogger(__name__)

_SPEC_DATA_DIR_DEFAULT = Path(__file__).parent / "spec_data"


def _load_static_tools_symbols(
    odoo_version: str,
    static_data_dir: str | Path | None = None,
) -> list[CoreSymbolInfo]:
    """Load curated tool_export CoreSymbolInfo from tools_symbols_<version>.json.

    Returns an empty list when the file is absent or unparseable — callers
    should treat absence as "no curated data for this version" (not an error).

    Args:
        odoo_version:    Odoo version label, e.g. "17.0".
        static_data_dir: Override directory for static spec_data JSON files.
                         Defaults to src/indexer/spec_data/.
    """
    base = Path(static_data_dir) if static_data_dir else _SPEC_DATA_DIR_DEFAULT
    path = base / f"tools_symbols_{odoo_version}.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    out: list[CoreSymbolInfo] = []
    for entry in data.get("symbols", []):
        if not isinstance(entry, dict):
            continue
        qname = entry.get("qualified_name", "").strip()
        if not qname:
            continue
        kind = entry.get("kind", "tool_export")
        status = entry.get("status", "stable")
        out.append(CoreSymbolInfo(
            qualified_name=qname,
            kind=kind,
            odoo_version=odoo_version,
            signature=entry.get("signature"),
            file_path=None,
            line=None,
            status=status,
            replacement_qname=entry.get("replacement_qname"),
        ))
    return out


# Public alias with cleaner name for external callers (e.g. pipeline.py).
load_tools_symbols = _load_static_tools_symbols


# ---------------------------------------------------------------------------
# Part 2 — parse_tools_symbols: the AST/text-regex oracle (issue #364).
# Independent of the curated loader above; this is what
# tests/test_tools_symbols_content_parity.py diffs the curated data against.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedToolSymbol:
    """One real ``odoo.tools.*`` symbol, as found by the AST/text-regex oracle.

    ``name`` is the bare name after ``odoo.tools.`` — either a flat name
    (``"html_escape"``, ``"ustr"``, ``"pycompat"``) or a dotted
    submodule-attribute path (``"image.image_process"``, see module
    docstring "DOTTED ATTRIBUTE-PATH SYMBOLS"). ``qualified_name`` is always
    ``f"odoo.tools.{name}"`` — never ``openerp.tools.*`` even at v8/v9,
    matching the curated data's own convention (curated qualified_names all
    start with ``odoo.tools`` regardless of era — see
    ``tests/test_parser_tools_symbols.py``'s qname-prefix contract).
    """

    name: str
    qualified_name: str
    signature: str | None
    file_path: str
    line: int | None


# Era-prefix resolution: this feature's OWN tiny one-boundary registry
# (ADR-0052: every feature-specific boundary gets its own registry, never a
# scattered ``if major <= N``), reusing ODOO_NAMESPACE_LEGACY_MAX_MAJOR
# rather than a second hardcoded "9" — mirrors framework_bases.py's
# ``_ERA_PREFIX_REGISTRY`` / parser_odoo_core.py's ``_PREFIX_REGISTRY`` shape.
_ERA_PREFIX_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8, ODOO_NAMESPACE_LEGACY_MAX_MAJOR, "openerp"),
    (ODOO_NAMESPACE_LEGACY_MAX_MAJOR + 1, None, "odoo"),
])


def _era_prefix(odoo_version: str) -> str:
    return _ERA_PREFIX_REGISTRY.resolve_version(odoo_version, default="odoo")  # type: ignore[return-value]


@dataclass(frozen=True)
class _Binding:
    """One name bound at module scope of a single file (def/class/assign, an
    explicit import target, or a submodule self-binding — see module
    docstring). ``signature`` is only ever set for a FunctionDef/AsyncFunctionDef
    found via the AST path; ``"module"`` is a special sentinel signature used
    for submodule self-bindings (mirrors curated data's own
    ``signature: "module"`` convention for ``date_utils``/``js_transpiler``).
    """

    name: str
    line: int | None
    signature: str | None
    file_path: str


@dataclass(frozen=True)
class _ImportStmt:
    """One ``ImportFrom`` statement, origin-agnostic (AST or text-regex).

    ``module_ref`` is the raw dotted-plus-leading-dots module string exactly
    as written (``".misc"``, ``"misc"`` (v8/9 implicit-relative), ``"."``
    (bare ``from . import X``), ``"odoo.loglevels"`` (absolute, external)).
    ``items`` is a list of ``(original_name_or_star, bound_name, line)`` —
    ``original_name_or_star`` is ``"*"`` for a star import, otherwise the
    name as defined in the SOURCE module (before any ``as`` alias); for the
    bare ``from . import X[, Y]`` shape, ``original_name_or_star`` is itself
    the SUBMODULE name being imported (there is no "module" to look inside —
    see ``_flat_public_api``'s dispatch on ``module_ref`` stripped-to-empty).
    """

    module_ref: str
    items: list[tuple[str, str, int]]


@dataclass(frozen=True)
class _RawScan:
    """One file's own module-scope bindings and import statements — no
    recursion, no ``__all__`` filtering, no cross-file resolution. Pure
    per-file syntax extraction (AST-first, text-regex fallback on
    ``SyntaxError``). ``_flat_public_api`` is the layer that resolves
    imports across files and applies ``__all__``.
    """

    bindings: dict[str, _Binding]
    imports: list[_ImportStmt]
    dunder_all: list[str] | None


def _iter_module_level_stmts(body: list[ast.stmt]) -> list[ast.stmt]:
    """Every top-level statement, PLUS (one level only) the body/orelse of any
    module-scope ``if`` among them — recovery mode 1 (module docstring). Only
    descends ONE level: an if/else nested inside a further if/else at module
    scope is not expanded again, matching the audit's confirmed real-source
    shape (a single if/else pair around a def) and keeping this oracle's
    inclusion rule bounded rather than an unbounded walk.
    """
    out: list[ast.stmt] = []
    for stmt in body:
        out.append(stmt)
        if isinstance(stmt, ast.If):
            out.extend(stmt.body)
            out.extend(stmt.orelse)
    return out


def _scan_ast(tree: ast.Module, relpath: str) -> _RawScan:
    """Primary path — tried first for every file (module docstring "TWO-TIER
    PARSE"). Discovers module-scope (+ one level into ``If``) FunctionDef/
    AsyncFunctionDef/ClassDef/Assign/AnnAssign/ImportFrom, never via
    ``ast.walk`` (CLAUDE.md AST discipline: a nested def/class must never be
    mistaken for a module-scope one).
    """
    bindings: dict[str, _Binding] = {}
    imports: list[_ImportStmt] = []
    dunder_all: list[str] | None = None

    def add(stmt: ast.stmt) -> None:
        nonlocal dunder_all
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not stmt.name.startswith("_"):
                sig = f"{stmt.name}({ast.unparse(stmt.args)})"
                bindings[stmt.name] = _Binding(stmt.name, stmt.lineno, sig, relpath)
        elif isinstance(stmt, ast.ClassDef):
            if not stmt.name.startswith("_"):
                bindings[stmt.name] = _Binding(stmt.name, stmt.lineno, None, relpath)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if not isinstance(t, ast.Name):
                    continue
                if t.id == "__all__":
                    try:
                        val = ast.literal_eval(stmt.value)
                    except (ValueError, SyntaxError, TypeError):
                        val = None
                    if isinstance(val, (list, tuple, set)):
                        dunder_all = [str(v) for v in val]
                elif not t.id.startswith("_"):
                    bindings[t.id] = _Binding(t.id, stmt.lineno, None, relpath)
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and not stmt.target.id.startswith("_"):
                bindings[stmt.target.id] = _Binding(stmt.target.id, stmt.lineno, None, relpath)
        elif isinstance(stmt, ast.ImportFrom):
            module_ref = ("." * stmt.level) + (stmt.module or "")
            items = [
                (alias.name, alias.asname or alias.name, stmt.lineno)
                for alias in stmt.names
            ]
            imports.append(_ImportStmt(module_ref=module_ref, items=items))

    for stmt in _iter_module_level_stmts(tree.body):
        add(stmt)

    return _RawScan(bindings=bindings, imports=imports, dunder_all=dunder_all)


# --- text-regex fallback (SyntaxError only) ---------------------------------
# Deliberately loose on indentation (matches ANY leading whitespace, not just
# exactly-one-level) — precision is not required here (a spurious extra name
# in `recovered` never fails a test that only checks curated subset-of-
# recovered), only RECALL is: a genuinely public module-scope name must never
# silently vanish just because its file failed ast.parse. Column-0-anchored
# via `^` + MULTILINE, same primitive shape as framework_bases.py's
# `_RE_CLASS_HEAD` / parser_python_era1.py's `_RE_CLASS_HEAD`.
_RE_TEXT_DEF = re.compile(r"^[ \t]*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)
_RE_TEXT_CLASS = re.compile(r"^[ \t]*class\s+(\w+)\s*[:(]", re.MULTILINE)
_RE_TEXT_ASSIGN = re.compile(r"^[ \t]*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)", re.MULTILINE)
_RE_TEXT_FROM_IMPORT = re.compile(r"^from\s+([.\w]*)\s+import\s+(.+)$", re.MULTILINE)


def _scan_text(source: str, relpath: str) -> _RawScan:
    """``SyntaxError`` fallback — text-regex, never mutates *source*, always
    succeeds (possibly with fewer bindings than a clean AST parse would
    yield). ``__all__`` detection is intentionally NOT attempted here (every
    real ``__all__`` on this machine's checkouts lives in a file that parses
    cleanly via AST — v18/v19 ``misc.py`` — so this path is never actually
    exercised for an ``__all__``-bearing file; keeping this fallback simple
    is correct, not a shortcut).
    """
    bindings: dict[str, _Binding] = {}
    for m in _RE_TEXT_DEF.finditer(source):
        name, params = m.group(1), m.group(2)
        if not name.startswith("_"):
            line = source.count("\n", 0, m.start()) + 1
            bindings[name] = _Binding(name, line, f"{name}({params.strip()})", relpath)
    for m in _RE_TEXT_CLASS.finditer(source):
        name = m.group(1)
        if not name.startswith("_"):
            line = source.count("\n", 0, m.start()) + 1
            bindings.setdefault(name, _Binding(name, line, None, relpath))
    for m in _RE_TEXT_ASSIGN.finditer(source):
        name = m.group(1)
        if name == "__all__" or name.startswith("_"):
            continue
        line = source.count("\n", 0, m.start()) + 1
        bindings.setdefault(name, _Binding(name, line, None, relpath))

    imports: list[_ImportStmt] = []
    for m in _RE_TEXT_FROM_IMPORT.finditer(source):
        module_ref, rhs = m.group(1), m.group(2).strip()
        line = source.count("\n", 0, m.start()) + 1
        if rhs == "*":
            imports.append(_ImportStmt(module_ref=module_ref, items=[("*", "*", line)]))
            continue
        items: list[tuple[str, str, int]] = []
        for part in rhs.split(","):
            part = part.strip().strip("()")
            if not part:
                continue
            if " as " in part:
                orig, bound = part.split(" as ", 1)
                orig, bound = orig.strip(), bound.strip()
            else:
                orig = bound = part.strip()
            if orig:
                items.append((orig, bound, line))
        if items:
            imports.append(_ImportStmt(module_ref=module_ref, items=items))

    return _RawScan(bindings=bindings, imports=imports, dunder_all=None)


def _raw_scan(file_path: Path, relpath: str, cache: dict[Path, _RawScan]) -> _RawScan:
    """Two-tier per-file scan (memoized — each file is read/parsed at most
    once per ``parse_tools_symbols`` call, shared between the flat-namespace
    resolution and the dotted-attribute exposure step).
    """
    cached = cache.get(file_path)
    if cached is not None:
        return cached
    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        _logger.warning(
            "parse_tools_symbols: could not read %s (%s) - skipping this file.",
            file_path, exc,
        )
        result = _RawScan(bindings={}, imports=[], dunder_all=None)
        cache[file_path] = result
        return result

    try:
        tree = parse_external_source(source, filename=str(file_path))
    except SyntaxError as exc:
        # Per this issue's brief: WARNING, not DEBUG - every fallback taken
        # must be visible, never buried (module docstring "TWO-TIER PARSE").
        _logger.warning(
            "parse_tools_symbols: ast.parse failed for %s (%s) - falling "
            "back to text-regex scan (v8/v9 Python-2 syntax hazard, issue "
            "#364).", file_path, exc,
        )
        result = _scan_text(source, relpath)
        if not result.bindings and not result.imports:
            _logger.warning(
                "parse_tools_symbols: text-regex fallback found ZERO "
                "bindings in %s after a SyntaxError (%s).", file_path, exc,
            )
    else:
        result = _scan_ast(tree, relpath)
    cache[file_path] = result
    return result


def _resolve_relative_submodule(tools_dir: Path, module_ref: str) -> Path | None:
    """Resolve a ``from X import ...`` module string to a real file WITHIN
    *tools_dir*, or ``None`` when it is not a local sibling (an absolute
    external reference like ``odoo.loglevels``/``werkzeug.utils``, or simply
    not found on disk). Handles every era's spelling seen in real
    ``odoo/tools/__init__.py`` across v8-v19:

      ``.misc``   - v10+ relative (``from .misc import *``)
      ``misc``    - v8-v10 implicit-relative (``from misc import *``,
                    level=0, no leading dot - Python 2 semantics)
      ``.``       - bare ``from . import X`` (module_ref after stripping
                    dots is empty - this function returns None for THAT
                    shape deliberately; the caller resolves each imported
                    NAME as its own submodule reference instead, see
                    ``_flat_public_api``)

    Only a bare (undotted) trailing component is treated as "within
    tools_dir" - a genuinely dotted absolute reference (``odoo.loglevels``,
    ``werkzeug.utils``) is external and out of this oracle's scope (the
    binding it introduces, if any, is already captured at the REFERENCING
    file's own level via that file's explicit ImportFrom item - see module
    docstring "recovery mode 2").
    """
    name = module_ref.lstrip(".")
    if not name or "." in name:
        return None
    candidate_file = tools_dir / f"{name}.py"
    if candidate_file.is_file():
        return candidate_file
    candidate_pkg = tools_dir / name / "__init__.py"
    if candidate_pkg.is_file():
        return candidate_pkg
    return None


def _relpath(file_path: Path, tools_dir: Path, tools_relpath_base: str) -> str:
    try:
        rel = file_path.relative_to(tools_dir)
    except ValueError:
        return str(file_path)
    return f"{tools_relpath_base}/{rel.as_posix()}"


def _submodule_stem(file_path: Path) -> str:
    """The name a resolved submodule path is bound under: the parent dir's
    name for a package's ``__init__.py``, otherwise the file's own stem.
    """
    return file_path.parent.name if file_path.name == "__init__.py" else file_path.stem


def _flat_public_api(
    file_path: Path,
    relpath: str,
    tools_dir: Path,
    tools_relpath_base: str,
    cache: dict[Path, _RawScan],
    visited: frozenset[Path],
) -> dict[str, _Binding]:
    """The real, ``import *``-accurate public API of one module file: its own
    direct bindings, PLUS (recursively) whatever its ``ImportFrom``
    statements pull in, filtered by ITS OWN ``__all__`` when it defines one.
    Cycle-guarded via *visited* (a pathological/hand-crafted or circular
    star-import chain could otherwise recurse forever - never expected in
    real Odoo source, but this oracle must not crash if it ever happened).
    """
    if file_path in visited:
        return {}
    visited = visited | {file_path}
    raw = _raw_scan(file_path, relpath, cache)
    merged: dict[str, _Binding] = dict(raw.bindings)

    for imp in raw.imports:
        stripped = imp.module_ref.lstrip(".")
        if not stripped:
            # Bare `from . import X[, Y]` - each name IS a submodule
            # reference (module docstring "SUBMODULE SELF-BINDING"), not a
            # symbol to look up inside some other module.
            for orig, bound, line in imp.items:
                if bound.startswith("_"):
                    continue
                target = _resolve_relative_submodule(tools_dir, orig)
                if target is not None:
                    target_relpath = _relpath(target, tools_dir, tools_relpath_base)
                    merged[bound] = _Binding(bound, line, "module", target_relpath)
                else:
                    merged.setdefault(bound, _Binding(bound, line, None, relpath))
            continue

        target = _resolve_relative_submodule(tools_dir, imp.module_ref)
        is_star = any(orig == "*" for orig, _, _ in imp.items)
        if target is None:
            # External (outside tools_dir) or unresolvable - what's directly
            # visible is only the explicit names themselves (e.g.
            # `from odoo.loglevels import ustr` binds `ustr` right here,
            # module docstring "recovery mode 2"); a star import from an
            # external module cannot be resolved further (out of scope -
            # only odoo.tools.* is indexed by this oracle).
            for orig, bound, line in imp.items:
                if orig == "*" or bound.startswith("_"):
                    continue
                merged.setdefault(bound, _Binding(bound, line, None, relpath))
            continue

        target_relpath = _relpath(target, tools_dir, tools_relpath_base)
        stem = _submodule_stem(target)
        if not stem.startswith("_"):
            first_line = imp.items[0][2] if imp.items else None
            merged.setdefault(stem, _Binding(stem, first_line, "module", target_relpath))

        if is_star:
            for name, binding in _flat_public_api(
                target, target_relpath, tools_dir, tools_relpath_base, cache, visited,
            ).items():
                if not name.startswith("_"):
                    merged[name] = binding
        else:
            target_bindings = _raw_scan(target, target_relpath, cache).bindings
            for orig, bound, line in imp.items:
                if bound.startswith("_"):
                    continue
                src = target_bindings.get(orig)
                if src is not None:
                    merged[bound] = _Binding(bound, src.line, src.signature, src.file_path)
                else:
                    merged.setdefault(bound, _Binding(bound, line, None, target_relpath))

    if raw.dunder_all is not None:
        filtered: dict[str, _Binding] = {}
        for name in raw.dunder_all:
            filtered[name] = merged.get(name) or _Binding(name, None, None, relpath)
        merged = filtered

    return merged


def parse_tools_symbols(
    odoo_source_root: str | Path,
    odoo_version: str,
) -> dict[str, ParsedToolSymbol] | None:
    """AST/text-regex oracle for the real ``odoo.tools.*`` symbol surface.

    ``None`` only when no readable ``<era-prefix>/tools/__init__.py`` FILE
    exists at *odoo_source_root* for *odoo_version* (mirrors
    ``parse_framework_bases``'s "no readable common.py" contract). Once a
    readable ``__init__.py`` is found, this ALWAYS returns a dict — possibly
    empty, in which case it is logged at WARNING (module docstring "TWO-TIER
    PARSE": a silent zero is exactly the failure mode issue #364 exists to
    catch).

    Keys are bare names after ``odoo.tools.`` — flat re-exported names
    (``"html_escape"``, ``"ustr"``, ``"pycompat"``, ``"SQL"``, ...) AND
    dotted submodule-attribute paths (``"image.image_process"`` at v19 — see
    module docstring "DOTTED ATTRIBUTE-PATH SYMBOLS"). A flat key always wins
    over the dotted-exposure step for the same key (impossible in practice —
    a flat key never contains a dot, so the two namespaces cannot collide).
    """
    root = Path(odoo_source_root)
    prefix = _era_prefix(odoo_version)
    tools_dir = root / prefix / "tools"
    tools_relpath_base = f"{prefix}/tools"
    init_path = tools_dir / "__init__.py"

    if not init_path.is_file():
        _logger.debug(
            "parse_tools_symbols: no readable __init__.py at %s (Odoo %s) - "
            "returning None.", init_path, odoo_version,
        )
        return None
    try:
        init_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        _logger.warning(
            "parse_tools_symbols: could not read %s (%s) - returning None.",
            init_path, exc,
        )
        return None

    cache: dict[Path, _RawScan] = {}
    init_relpath = f"{tools_relpath_base}/__init__.py"
    flat = _flat_public_api(
        init_path, init_relpath, tools_dir, tools_relpath_base, cache, frozenset(),
    )

    result: dict[str, ParsedToolSymbol] = {}
    for name, binding in flat.items():
        result[name] = ParsedToolSymbol(
            name=name,
            qualified_name=f"odoo.tools.{name}",
            signature=binding.signature,
            file_path=binding.file_path,
            line=binding.line,
        )

    # Dotted attribute-path exposure (module docstring "DOTTED
    # ATTRIBUTE-PATH SYMBOLS"): every direct .py submodule file under
    # tools_dir is ALWAYS import-attribute-accessible at odoo.tools.<stem>.
    # <name>, independent of __init__.py's own re-export choices.
    try:
        submodule_files = sorted(
            p for p in tools_dir.iterdir()
            if p.is_file()
            and p.suffix == ".py"
            and p.stem != "__init__"
            and not p.stem.startswith("_")
        )
    except OSError as exc:
        _logger.warning(
            "parse_tools_symbols: could not list %s (%s) - dotted attribute "
            "exposure skipped for this version.", tools_dir, exc,
        )
        submodule_files = []

    for sub in submodule_files:
        sub_relpath = f"{tools_relpath_base}/{sub.name}"
        # The submodule's OWN bare name is ALSO always a valid explicit
        # import target (`from odoo.tools import <stem>`), independent of
        # whether __init__.py's graph currently self-binds it - the exact
        # same "file exists -> always importable" fact that justifies the
        # dotted-member exposure below, applied to the submodule itself.
        # `setdefault` so a flat namespace resolution result (e.g. `config`,
        # resolved to the configmanager INSTANCE via an explicit `from
        # .config import config`) always takes priority over this generic
        # marker. This is what makes `odoo.tools.date_utils` /
        # `odoo.tools.js_transpiler` still recoverable at v19 even though
        # `odoo/tools/__init__.py` there no longer references either
        # submodule at all (verified: both files still exist on disk and are
        # still imported elsewhere in v19 core via
        # `from odoo.tools import date_utils` /
        # `from odoo.tools.js_transpiler import ...` - odoo/orm/fields_temporal.py,
        # odoo/orm/domains.py, odoo/addons/base/models/assetsbundle.py - the
        # SAME "re-export dropped from __init__.py, capability still present
        # under its real resolving path" shape as issue #364 Problem 2
        # (image_process), just not one of the two the phase2-C audit
        # individually named - see this task's final report).
        result.setdefault(sub.stem, ParsedToolSymbol(
            name=sub.stem,
            qualified_name=f"odoo.tools.{sub.stem}",
            signature="module",
            file_path=sub_relpath,
            line=None,
        ))
        raw = _raw_scan(sub, sub_relpath, cache)
        for name, binding in raw.bindings.items():
            dotted = f"{sub.stem}.{name}"
            result.setdefault(dotted, ParsedToolSymbol(
                name=dotted,
                qualified_name=f"odoo.tools.{dotted}",
                signature=binding.signature,
                file_path=binding.file_path,
                line=binding.line,
            ))

    if not result:
        _logger.warning(
            "parse_tools_symbols: recovered ZERO symbols for Odoo %s at %s - "
            "a silent zero here is exactly the failure mode issue #364 "
            "exists to catch; investigate the __init__.py import graph at "
            "this version.", odoo_version, tools_dir,
        )
    return result
