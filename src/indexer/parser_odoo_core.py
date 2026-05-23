# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_odoo_core.py
"""Extract CoreSymbol entries from Odoo upstream Python source (M4.5 WI2.2).

Boil-the-Lake principle: complete top-down inventory across stable v17/v18/v19+
core API surface — but bounded by an allow-list of 8 well-known files. We do NOT
walk the full Odoo source (1000+ files), nor parse third-party addons here.

Per ADR-0002 §6 — the 8 allow-list paths cover the entire surface area surveyed
across v17→v19 changes (~80 unique symbol changes). Allow-list is intentional:
keeping it stable lets the indexer run in O(8 × file_size_avg) per Odoo version,
typically <1s/version on typical hardware.

Public API:
    parse_odoo_core(odoo_source_root, odoo_version) -> list[CoreSymbolInfo]

Private helpers:
    _extract_from_source(source, module_qname, odoo_version, file_path=None)
"""
import ast
from pathlib import Path

from src.constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR

from .models import CoreSymbolInfo
from .version_registry import VersionRegistry

# --- Allow-list (ADR-0002 §6) -----------------------------------------------
_CORE_FILES: tuple[str, ...] = (
    "odoo/tools/safe_eval.py",
    "odoo/tools/query.py",
    "odoo/tools/sql.py",
    "odoo/fields.py",
    "odoo/models.py",
    "odoo/api.py",
    "odoo/sql_db.py",
    "odoo/exceptions.py",
)

# v19-only curated public APIs from the split ORM package (boil-the-lake, curated).
# These files do not exist before v19, so _resolve_core_paths returns [] and they
# are silently skipped on v8–v18. Only the listed PUBLIC symbols are emitted —
# the ~48 internal helpers in domains.py (e.g. _optimize_*) are excluded via the
# name allow-list, per the "curated, not maximal" scope decision.
#   Domain: v19's first-class domain builder (replaces raw list-of-tuples exprs).
#   table_objects: v19's declarative Constraint/Index API (replaces _sql_constraints).
# (The `Command` enum is NOT here — it kept its v18 qname `odoo.fields.Command`
#  and is resolved via the odoo/fields.py entry; see _resolve_core_paths.)
_V19_CURATED_FILES: dict[str, frozenset[str]] = {
    "odoo/orm/domains.py": frozenset({"Domain", "DomainAnd", "DomainOr"}),
    "odoo/orm/table_objects.py": frozenset(
        {"TableObject", "Constraint", "Index", "UniqueIndex"}
    ),
}

# Class-name heuristics for `kind` classification.
# Includes one level of well-known intermediate field bases so that classes like
# Many2one(_Relational) and Char(BaseString) classify as 'field_type' without a
# recursive AST climb (per ADR-0002 KISS policy).
# v18+ split hierarchy: _Relational, _String (v18 fields.py); BaseString, BaseDate,
# _RelationalMulti (v19 orm/fields*.py).
_FIELD_BASE_NAMES = {
    "Field",
    "_Relational",
    "_RelationalMulti",
    "_String",
    "BaseString",
    "BaseDate",
}
# Direct stdlib + Odoo's primary user-facing base. The hierarchy is shallow in
# practice (one indirection through UserError); deeper trees would need a
# recursive AST climb, deferred per ADR-0002.
_EXCEPTION_BASE_NAMES = {"Exception", "Warning", "BaseException", "UserError"}
_ORM_BASE_NAMES = {"BaseModel", "Model", "TransientModel", "AbstractModel"}
_CURSOR_HINT_FILES = {"odoo.sql_db"}  # methods inside any class in this module = cursor_method


# --- AST helpers ------------------------------------------------------------

def _base_names(cls_node: ast.ClassDef) -> set[str]:
    """Collect simple base class names (handles `Field`, `tools.SomeBase`, etc.).

    v18+ generic syntax: class Integer(Field[int]) → Subscript(value=Name('Field')).
    v18+ with module prefix: class Foo(fields.Field[int]) →
    Subscript(value=Attribute(attr='Field')).
    Unwrap Subscript by recursing on .value so generic field classes still classify
    as 'field_type' (e.g. Field[int] → 'Field').
    """
    names: set[str] = set()
    for base in cls_node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
        elif isinstance(base, ast.Subscript):
            # PEP-695-style generics: Field[int], _Relational[M], BaseDate[date], etc.
            # Unwrap .value — it is Name or Attribute — and extract the short name.
            inner = base.value
            if isinstance(inner, ast.Name):
                names.add(inner.id)
            elif isinstance(inner, ast.Attribute):
                names.add(inner.attr)
    return names


def _classify_class(cls_node: ast.ClassDef) -> str:
    """Return CoreSymbol kind for a top-level class definition."""
    bases = _base_names(cls_node)
    if bases & _FIELD_BASE_NAMES:
        return "field_type"
    if bases & _EXCEPTION_BASE_NAMES:
        return "exception"
    return "class"


def _has_deprecated_decorator(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Detect `@api.deprecated(...)` or `@deprecated(...)` on a function/method."""
    for dec in fn_node.decorator_list:
        # `@api.deprecated('msg')` → ast.Call(func=ast.Attribute(attr='deprecated'))
        if isinstance(dec, ast.Call):
            target = dec.func
        else:
            target = dec
        if isinstance(target, ast.Attribute) and target.attr == "deprecated":
            return True
        if isinstance(target, ast.Name) and target.id == "deprecated":
            return True
    return False


def _has_body_level_deprecation_warning(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Detect body-level `warnings.warn(..., DeprecationWarning)` inside a method.

    Odoo v17 deprecates `name_get` via a `warnings.warn(...)` call in the method
    body rather than a decorator.  This helper catches that pattern so the symbol
    is classified as status='deprecated' even when no decorator is present.

    Matches any `ast.Call` where:
    - the callable is `warnings.warn` (ast.Attribute attr=='warn'), AND
    - `DeprecationWarning` appears as a positional arg or as keyword `category=`.
    """
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Must be exactly `warnings.warn(...)` — not logger.warn or any other .warn call.
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "warn"
            and isinstance(func.value, ast.Name)
            and func.value.id == "warnings"
        ):
            continue
        # Collect all candidate nodes: positional args + keyword 'category' value
        candidates: list[ast.expr] = list(node.args)
        for kw in node.keywords:
            if kw.arg == "category":
                candidates.append(kw.value)
        for cand in candidates:
            if isinstance(cand, ast.Name) and cand.id == "DeprecationWarning":
                return True
            if isinstance(cand, ast.Attribute) and cand.attr == "DeprecationWarning":
                return True
    return False


def _build_function_symbol(
    fn_node: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified_name: str,
    odoo_version: str,
    kind: str,
    file_path: str | None,
) -> CoreSymbolInfo:
    status = (
        "deprecated"
        if (_has_deprecated_decorator(fn_node) or _has_body_level_deprecation_warning(fn_node))
        else "stable"
    )
    # Compact signature: name(args). Default values omitted in V0.
    args = [a.arg for a in fn_node.args.args]
    if fn_node.args.vararg:
        args.append(f"*{fn_node.args.vararg.arg}")
    if fn_node.args.kwarg:
        args.append(f"**{fn_node.args.kwarg.arg}")
    sig = f"{fn_node.name}({', '.join(args)})"

    return CoreSymbolInfo(
        qualified_name=qualified_name,
        kind=kind,
        odoo_version=odoo_version,
        signature=sig,
        file_path=file_path,
        line=fn_node.lineno,
        status=status,
    )


def _method_kind(class_node: ast.ClassDef, module_qname: str) -> str:
    """Classify methods inside a class: orm_method / cursor_method / function."""
    if module_qname in _CURSOR_HINT_FILES:
        return "cursor_method"
    if _base_names(class_node) & _ORM_BASE_NAMES or class_node.name in _ORM_BASE_NAMES:
        return "orm_method"
    return "function"


def _extract_class_methods(
    cls_node: ast.ClassDef,
    module_qname: str,
    odoo_version: str,
    file_path: str | None,
) -> list[CoreSymbolInfo]:
    """Walk class body — emit one symbol per public method (not __dunder__ / _private)."""
    out: list[CoreSymbolInfo] = []
    method_kind = _method_kind(cls_node, module_qname)
    for node in cls_node.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name.startswith("_"):
            # Skip both `_private` and `__dunder__`.
            continue
        qname = f"{module_qname}.{cls_node.name}.{node.name}"
        out.append(_build_function_symbol(
            node, qname, odoo_version, kind=method_kind, file_path=file_path,
        ))
    return out


# --- Public API -------------------------------------------------------------

def _extract_from_source(
    source: str,
    module_qname: str,
    odoo_version: str,
    file_path: str | None = None,
    name_allowlist: frozenset[str] | None = None,
) -> list[CoreSymbolInfo]:
    """Extract CoreSymbol from a single Python source string.

    Top-level only — Boil-the-Lake but bounded. Module-level functions →
    kind='function'. Top-level classes → kind ∈ {class, field_type, exception},
    plus their public methods → kind ∈ {orm_method, cursor_method, function}.

    When ``name_allowlist`` is given, only top-level symbols whose name is in the
    set are emitted (their public methods still follow). Used for the v19 curated
    split-ORM files where the file holds many internal helpers but only a few
    public classes should be indexed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    symbols: list[CoreSymbolInfo] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if name_allowlist is not None and node.name not in name_allowlist:
                continue
            qname = f"{module_qname}.{node.name}"
            symbols.append(_build_function_symbol(
                node, qname, odoo_version, kind="function", file_path=file_path,
            ))
        elif isinstance(node, ast.ClassDef):
            if name_allowlist is not None and node.name not in name_allowlist:
                continue
            kind = _classify_class(node)
            class_qname = f"{module_qname}.{node.name}"
            # Detect class-level @deprecated decorator → mark class status='deprecated'.
            class_status = (
                "deprecated"
                if any(
                    (isinstance(d, ast.Attribute) and d.attr == "deprecated")
                    or (isinstance(d, ast.Name) and d.id == "deprecated")
                    or (
                        isinstance(d, ast.Call)
                        and isinstance(d.func, ast.Attribute)
                        and d.func.attr == "deprecated"
                    )
                    for d in node.decorator_list
                )
                else "stable"
            )
            symbols.append(CoreSymbolInfo(
                qualified_name=class_qname,
                kind=kind,
                odoo_version=odoo_version,
                signature=f"class {node.name}",
                file_path=file_path,
                line=node.lineno,
                status=class_status,
            ))
            # Public methods inside the class
            symbols.extend(_extract_class_methods(
                node, module_qname, odoo_version, file_path,
            ))
    return symbols


# Version-dispatch registry for namespace-prefix selection (ADR-0032).
# v8/v9: openerp/ (pre-rename era).
# v10+:  odoo/ (modern namespace, open-ended).
# To add v20 with a hypothetical new namespace: append one entry here.
_PREFIX_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8,  ODOO_NAMESPACE_LEGACY_MAX_MAJOR, "openerp/"),  # v8–v9
    (10, None,                            "odoo/"),      # v10+, open-ended
])


def _version_prefix(version: str) -> str:
    """Return framework directory prefix for a given Odoo version.

    v8/v9 use ``openerp/`` (the pre-rename era); v10+ use ``odoo/``.
    """
    return _PREFIX_REGISTRY.resolve_version(version, default="odoo/")  # type: ignore[return-value]


def _resolve_core_paths(odoo_root: Path, logical_path: str, version: str) -> list[Path]:
    """Map an allow-list logical path to one or more real file paths for `version`.

    Handles:
    - v8/v9: ``odoo/`` prefix is substituted to ``openerp/`` before file lookup.
      v8/v9 do not have the v19 package-dir split, so once the prefix is swapped
      the normal ``is_file()`` check passes directly.  Allow-list paths that do
      not exist in a given v8 source tree (e.g. ``openerp/tools/query.py``) cause
      an empty list to be returned and are silently skipped by the caller.
    - v19+: ``odoo/fields.py``, ``odoo/models.py``, and ``odoo/api.py`` became
      package directories that re-export from ``odoo/orm/*.py`` split files.

    Returns:
        List of real file paths to parse.  Empty list if no equivalent files exist.
    """
    # --- v8/v9: substitute openerp/ prefix BEFORE all other checks -----------
    prefix_old = "odoo/"
    prefix_new = _version_prefix(version)
    if prefix_new != prefix_old and logical_path.startswith(prefix_old):
        logical_path = prefix_new + logical_path[len(prefix_old):]

    candidate = odoo_root / logical_path
    if candidate.is_file():
        return [candidate]

    # v19+ — odoo/{fields,models,api}.py became package directories.
    # (Only reached when prefix_new == "odoo/", i.e. v10+.)
    if logical_path == "odoo/fields.py":
        orm_dir = odoo_root / "odoo" / "orm"
        if orm_dir.is_dir():
            # v19 split: fields*.py (Char/Integer/Many2one/...) PLUS commands.py.
            # `Command` lived in odoo/fields.py through v18; keep its qname
            # `odoo.fields.Command` here so the v18→v19 diff sees continuity
            # (a moved-file, not a remove+add) and `lookup_core_api("Command")`
            # still resolves on v19.
            files = sorted(p for p in orm_dir.glob("fields*.py") if p.is_file())
            commands = orm_dir / "commands.py"
            if commands.is_file():
                files.append(commands)
            return files
    elif logical_path == "odoo/models.py":
        candidates = [
            odoo_root / "odoo" / "orm" / "models.py",
            odoo_root / "odoo" / "orm" / "models_transient.py",
        ]
        return [p for p in candidates if p.is_file()]
    elif logical_path == "odoo/api.py":
        candidates = [
            odoo_root / "odoo" / "orm" / "decorators.py",
            odoo_root / "odoo" / "orm" / "environments.py",
        ]
        return [p for p in candidates if p.is_file()]

    return []


def parse_odoo_core(odoo_source_root: str, odoo_version: str) -> list[CoreSymbolInfo]:
    """Extract CoreSymbol from the 8 allow-list files. Missing files are silently skipped.

    Args:
        odoo_source_root: Path to the Odoo upstream checkout root (parent of ``odoo/``).
        odoo_version:     Version label for all extracted symbols (e.g. ``"18.0"``).

    Returns:
        Flat list of CoreSymbolInfo. Order: file order in `_CORE_FILES`, then
        document order within each file (top-level def/class, then class methods).
        For v19+ split-file paths, files within a logical entry are iterated in
        sorted order so output is deterministic.
    """
    root = Path(odoo_source_root)
    if not root.is_dir():
        return []

    out: list[CoreSymbolInfo] = []
    # (logical_path, name_allowlist). None allow-list = emit all top-level symbols
    # (the 8 stable allow-list files); a frozenset = curated v19 split-ORM files.
    targets: list[tuple[str, frozenset[str] | None]] = [(p, None) for p in _CORE_FILES]
    targets += list(_V19_CURATED_FILES.items())
    for relpath, allow in targets:
        resolved = _resolve_core_paths(root, relpath, odoo_version)
        # "odoo/tools/safe_eval.py" → module_qname "odoo.tools.safe_eval"
        module_qname = relpath.removesuffix(".py").replace("/", ".")
        for full in resolved:
            try:
                source = full.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            out.extend(_extract_from_source(
                source, module_qname, odoo_version, file_path=str(full),
                name_allowlist=allow,
            ))
    return out
