# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_odoo_core.py
"""Extract CoreSymbol entries from Odoo upstream Python source (M4.5 WI2.2).

Boil-the-Lake principle: complete top-down inventory across the stable v8-v20
core API surface - but bounded by a small curated allow-list of well-known files
(_CORE_FILES). We do NOT walk the full Odoo source (1000+ files), nor parse
third-party addons here. Files that moved between versions are mapped by
_resolve_core_paths / _resolve_core_targets, keeping one qualified name per
logical module.
For v19+, a curated supplement (_V19_CURATED_FILES) indexes split-ORM public APIs
(Domain, table_objects, utils, model_classes) via name allowlist — 4 small files,
v19-only, emitting only public symbols.

Per ADR-0002 §6 - the curated allow-list paths cover the entire surface area
surveyed across v17→v19 changes (~80 unique symbol changes). Allow-list is
intentional: keeping it small and stable lets the indexer run in
O(len(_CORE_FILES) × file_size_avg) per Odoo version, typically <1s/version on
typical hardware.

Public API:
    parse_odoo_core(odoo_source_root, odoo_version) -> list[CoreSymbolInfo]

Private helpers:
    _extract_from_source(source, module_qname, odoo_version, file_path=None)
"""
import ast
from pathlib import Path

from src.constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR

from .models import CoreSymbolInfo
from .parser_util import parse_external_source
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
    # issue #117 bug#3: res.users / res.groups public API (has_group / has_groups)
    # lives in addons/base, NOT under odoo/ - without it, lookup_core_api("has_group")
    # returned "not found" on every version. has_groups (plural) is v18+; it simply
    # is absent from pre-v18 source so nothing extra is emitted there.
    "odoo/addons/base/models/res_users.py",
)
# _CORE_FILES is version-AGNOSTIC (no odoo/orm/ paths here).
# The resolver fan-out (_resolve_core_paths) handles v19 package-dir splits for
# fields/models/api and the v20 move of query.py into odoo/orm/ automatically;
# a logical module that became a package (v20: odoo/tools/safe_eval/) is read
# through its __init__.py re-exports (_resolve_core_targets). Per ADR-0002 §6,
# adding to _CORE_FILES is a deliberate curated decision - NOT automatic for
# every new file.

# v19-only curated public APIs from the split ORM package (boil-the-lake, curated).
# These files do not exist before v19, so _resolve_core_paths returns [] and they
# are silently skipped on v8–v18. Only the listed PUBLIC symbols are emitted via
# name_allowlist, per the "curated, not maximal" scope decision.
#   Domain: v19's first-class domain builder (replaces raw list-of-tuples exprs).
#   table_objects: v19's declarative Constraint/Index API (replaces _sql_constraints).
#   utils: parse_field_expr + OriginIds (new public ORM utility API in v19).
#   model_classes: is_model_class / is_model_definition (new public ORM introspection API).
# (The `Command` enum is NOT here — it kept its v18 qname `odoo.fields.Command`
#  and is resolved via the odoo/fields.py entry; see _resolve_core_paths.)
_V19_CURATED_FILES: dict[str, frozenset[str]] = {
    # FIX-2 (v19): expanded Domain allowlist from 3 → 8 public classes.
    # Evidence: scanned odoo19/odoo/orm/domains.py — public top-level classes:
    #   Domain (base), DomainBool, DomainNot, DomainNary, DomainAnd(DomainNary),
    #   DomainOr(DomainNary), DomainCustom, DomainCondition.
    # DomainNary: concrete (not abstract) base for n-ary AND/OR — included because
    #   AI clients writing custom domain builders need to know it exists.
    # DomainBool: represents constant True/False domain — genuine public builder.
    # DomainCustom: custom-SQL domain — genuine public builder.
    # DomainCondition: field condition leaf (added in FIX-2; missing was gap DD-1).
    # Excluded: OptimizationLevel (IntEnum, internal optimization level enum, not a
    #   domain builder) + ~48 internal helpers (_optimize_*, etc.) via allowlist.
    "odoo/orm/domains.py": frozenset({
        "Domain",
        "DomainAnd",
        "DomainOr",
        "DomainCondition",
        "DomainNot",
        "DomainBool",
        "DomainNary",
        "DomainCustom",
    }),
    "odoo/orm/table_objects.py": frozenset(
        {"TableObject", "Constraint", "Index", "UniqueIndex"}
    ),
    # FIX-3 (v19): curated public API from two new split-ORM files.
    # Correct mechanism: these files belong here (name_allowlist = curated set),
    # NOT in _CORE_FILES (which uses name_allowlist=None = emit ALL top-level symbols).
    # Placing them in _CORE_FILES would have indexed internal plumbing such as
    #   check_pg_name, check_method_name, add_to_registry, setup_model_classes, etc.
    #
    # utils.py — curated public API (scanned odoo19/odoo/orm/utils.py):
    #   parse_field_expr: parse a dotted field path — genuine public utility for
    #     ORM expressions; used by query builders and AI clients.
    #   OriginIds: reversible iterable for origin ids — public helper class.
    #   EXCLUDED: check_method_name (deprecated since 19.0 with DeprecationWarning),
    #     check_object_name (internal model-name validator),
    #     check_pg_name (internal PostgreSQL identifier validator),
    #     expand_ids (internal id-dedup generator, no direct user-facing use).
    "odoo/orm/utils.py": frozenset({
        "parse_field_expr",
        "OriginIds",
    }),
    # model_classes.py — curated public API (scanned odoo19/odoo/orm/model_classes.py):
    #   is_model_class: return True if cls is a model registry class — public introspection.
    #   is_model_definition: return True if cls is a model definition — public introspection.
    #   EXCLUDED: add_to_registry (internal registry machinery that creates/extends model
    #     classes), setup_model_classes, add_field, pop_field (all internal setup helpers),
    #     and all _private helpers (_check_model_extension, _init_model_class_attributes, etc.).
    "odoo/orm/model_classes.py": frozenset({
        "is_model_class",
        "is_model_definition",
    }),
}

# Class-name heuristics for `kind` classification.
# Includes direct abstract field bases AND concrete intermediate field bases so that
# ALL field subclasses (direct or one level deep) classify as 'field_type' without a
# recursive AST climb (per ADR-0002 KISS policy).
# v18+ split hierarchy: _Relational, _String (v18 fields.py); BaseString, BaseDate,
# _RelationalMulti (v19 orm/fields*.py).
#
# FIX-1: also include concrete intermediate bases to cover 2nd-level field subclasses:
#   Image(Binary), Reference(Selection), Many2oneReference(Integer)
# Scanned v17/v18 odoo/fields.py + v19 odoo/orm/fields*.py — Binary/Selection/Integer
# are the ONLY concrete field classes that are themselves subclassed in core.
# No 3rd-level field subclasses exist. Binary/Selection/Integer do not appear as class
# bases in any other allow-listed file (models.py, api.py, exceptions.py, sql_db.py,
# tools/*), so adding them here causes zero false positives.
_FIELD_BASE_NAMES = {
    "Field",
    "_Relational",
    "_RelationalMulti",
    "_String",
    "BaseString",
    "BaseDate",
    # 2nd-level concrete bases (FIX-1)
    "Binary",
    "Selection",
    "Integer",
}
# Direct stdlib + Odoo's primary user-facing base. The hierarchy is shallow in
# practice (one indirection through UserError); deeper trees would need a
# recursive AST climb, deferred per ADR-0002.
_EXCEPTION_BASE_NAMES = {"Exception", "Warning", "BaseException", "UserError"}
_ORM_BASE_NAMES = {"BaseModel", "Model", "TransientModel", "AbstractModel", "CachedModel"}
_CURSOR_HINT_FILES = {"odoo.sql_db"}  # methods inside any class in this module = cursor_method

# Curated allow-list of UNDERSCORE-prefixed methods that must be indexed despite
# the general "skip private" rule (issue #117 bug#2). These are public-by-contract
# ORM methods that happen to follow Odoo's leading-underscore convention AND are
# rename targets a migration consumer looks up by name. Without them,
# lookup_core_api("_has_cycle"/"_filtered_access"/"_compute_display_name", 18.0)
# returns "not found" even though they exist on disk.
#   _compute_display_name : since v12, replaces name_get (REMOVED v18)
#   _filtered_access      : since v18, replaces _filter_access_rules (deprecated v18)
#   _has_cycle            : since v18, replaces _check_recursion (deprecated v18)
# The DEPRECATED old underscore aliases (_filter_access_rules / _check_recursion /
# _flush_search ...) are caught automatically by the deprecation-marker check in
# _extract_class_methods, so they do NOT need to be listed here. Keep this set
# minimal and curated (ADR-0002 §6) - every entry is a documented rename target,
# not a blanket "index all private methods" escape hatch.
_INDEXED_PRIVATE_METHODS: frozenset[str] = frozenset({
    "_compute_display_name",
    "_filtered_access",
    "_has_cycle",
})


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


def _signature_params(args: ast.arguments) -> list[str]:
    """Parameter names in call order, with the ``/`` and ``*`` markers.

    Compact form: default values and annotations are omitted. Positional-only
    parameters are followed by ``/``; keyword-only parameters follow ``*name``
    or a bare ``*``. Example: ``safe_eval(expr, /, context, *, mode, filename)``.
    """
    params = [a.arg for a in args.posonlyargs]
    if args.posonlyargs:
        params.append("/")
    params += [a.arg for a in args.args]
    if args.vararg:
        params.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        params.append("*")
    params += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        params.append(f"**{args.kwarg.arg}")
    return params


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
    sig = f"{fn_node.name}({', '.join(_signature_params(fn_node.args))})"

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
    """Walk class body - emit one symbol per public method.

    Skips `__dunder__` and `_private` methods, with two curated exceptions
    (issue #117 bug#2) so the v18 ORM rename family resolves by name:
      - a method in `_INDEXED_PRIVATE_METHODS` (documented rename target), OR
      - a method carrying a deprecation marker (`@deprecated` decorator or a
        body-level `warnings.warn(..., DeprecationWarning)`), so deprecated
        underscore aliases (e.g. `_check_recursion`, `_filter_access_rules`)
        get a CoreSymbol node with status='deprecated' - the prerequisite for
        find_deprecated_usage to flag their usage.
    `__dunder__` methods are ALWAYS skipped (never a public API surface).
    """
    out: list[CoreSymbolInfo] = []
    method_kind = _method_kind(cls_node, module_qname)
    for node in cls_node.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        name = node.name
        if name.startswith("_"):
            if name.startswith("__"):
                # Dunder / name-mangled - never a public API surface.
                continue
            keep_private = (
                name in _INDEXED_PRIVATE_METHODS
                or _has_deprecated_decorator(node)
                or _has_body_level_deprecation_warning(node)
            )
            if not keep_private:
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
        # External Odoo upstream source — scope away SyntaxWarning noise, pass the
        # real path so any diagnostic is attributable (not <unknown>). See parser_util.
        tree = parse_external_source(source, filename=file_path)
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
    - v20+: ``odoo/tools/query.py`` moved to ``odoo/orm/query.py``.
    A logical module that became a package of its own (v20
    ``odoo/tools/safe_eval/``) resolves to no file here; ``_resolve_core_targets``
    reads it through the package's re-exports.

    Returns:
        List of real file paths to parse.  Empty list if no equivalent files exist.
    """
    # --- v8/v9: substitute openerp/ prefix BEFORE all other checks -----------
    prefix_old = "odoo/"
    prefix_new = _version_prefix(version)
    if prefix_new != prefix_old and logical_path.startswith(prefix_old):
        logical_path = prefix_new + logical_path[len(prefix_old):]

    # T4 — CORE-Q: query.py moved across directories between versions.
    # After prefix substitution above:
    #   v8-v9:  logical_path == "openerp/tools/query.py"  → real file: openerp/osv/query.py
    #   v10-v15: logical_path == "odoo/tools/query.py"    → real file: odoo/osv/query.py
    #   v16-v19: logical_path == "odoo/tools/query.py"    → real file: odoo/tools/query.py
    #   v20+:   logical_path == "odoo/tools/query.py"     → real file: odoo/orm/query.py
    # v20 left no module at odoo/tools/query.py (the ORM imports `.query`
    # from odoo/orm/). The qname stays `odoo.tools.query.*` across every era,
    # like the osv/ era before it, so api_version_diff sees one Query module
    # (moved, not removed + added); the Source line carries the real file.
    major = int(version.split(".")[0])
    if logical_path in ("odoo/tools/query.py", "openerp/tools/query.py"):
        if major <= 9:
            candidates = [odoo_root / "openerp" / "osv" / "query.py"]
        elif major <= 15:
            candidates = [odoo_root / "odoo" / "osv" / "query.py"]
        else:
            candidates = [
                odoo_root / "odoo" / "tools" / "query.py",
                odoo_root / "odoo" / "orm" / "query.py",
            ]
        return next(([p] for p in candidates if p.is_file()), [])

    # issue #117 bug#3 - res_users.py moved across directories between versions.
    # After prefix substitution above (openerp/ for v8-v9, odoo/ for v10+):
    #   v8-v11:  addons/base/res/res_users.py
    #   v12+:    addons/base/models/res_users.py
    # Try the modern `models/` location first, then the legacy `res/` one, so
    # has_group / has_groups resolve on every indexed version (D9).
    if logical_path.endswith("/res_users.py"):
        base = odoo_root / (prefix_new + "addons/base")
        for sub in ("models", "res"):
            p = base / sub / "res_users.py"
            if p.is_file():
                return [p]
        return []

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
            # v20+: CachedModel, re-exported via odoo/models/__init__.py
            # (from odoo.orm.models_cached import CachedModel).
            odoo_root / "odoo" / "orm" / "models_cached.py",
            odoo_root / "odoo" / "orm" / "models_transient.py",
        ]
        return [p for p in candidates if p.is_file()]
    elif logical_path == "odoo/api.py":
        candidates = [
            # v20+: ormcache moved from odoo/tools/cache.py to odoo/orm/cache.py
            # and is re-exported as odoo.api.ormcache (odoo.tools.ormcache is a
            # deprecated alias since 20.0). Only `ormcache` is re-exported -
            # see _FANOUT_NAME_ALLOWLISTS.
            odoo_root / "odoo" / "orm" / "cache.py",
            odoo_root / "odoo" / "orm" / "decorators.py",
            odoo_root / "odoo" / "orm" / "environments.py",
            # T5 — V19-G5: NewId moved from odoo/api.py (v18) to odoo/orm/identifiers.py (v19).
            # Routing through the api.py entry keeps the qname "odoo.api.NewId" so that
            # api_version_diff("NewId", "18.0", "19.0") sees continuity (moved, not removed).
            # re-exported via odoo/api/__init__.py: from odoo.orm.identifiers import NewId.
            odoo_root / "odoo" / "orm" / "identifiers.py",
        ]
        return [p for p in candidates if p.is_file()]

    return []


# Fan-out files of which the logical module re-exports only some names
# (root-relative path -> the re-exported names). A file listed here is parsed
# with this allow-list; every other fan-out file emits all its top-level symbols.
_FANOUT_NAME_ALLOWLISTS: dict[str, frozenset[str]] = {
    # odoo/api/__init__.py (v20+): `from odoo.orm.cache import ormcache` only.
    "odoo/orm/cache.py": frozenset({"ormcache"}),
}


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _narrow(
    allow: frozenset[str] | None, extra: frozenset[str] | None,
) -> frozenset[str] | None:
    """Intersection of two optional allow-lists (None = no restriction)."""
    if allow is None:
        return extra
    if extra is None:
        return allow
    return allow & extra


def _star_export_names(module_file: Path) -> frozenset[str]:
    """Names a ``from .module import *`` binds: the literal ``__all__`` when the
    module declares one, else every top-level def/class not starting with ``_``
    (Python's own star-import rule, restricted to what CoreSymbol indexes)."""
    try:
        tree = parse_external_source(
            module_file.read_text(encoding="utf-8", errors="ignore"),
            filename=str(module_file),
        )
    except (OSError, SyntaxError):
        return frozenset()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                break
            if isinstance(value, list | tuple) and all(isinstance(v, str) for v in value):
                return frozenset(value)
            break
    return frozenset(
        node.name for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and not node.name.startswith("_")
    )


def _package_reexports(package_dir: Path) -> list[tuple[Path, frozenset[str]]]:
    """``(submodule file, names)`` that ``package_dir/__init__.py`` re-exports.

    Reads the package's relative imports: ``from .sub import *`` re-exports
    the submodule's star-import names (``_star_export_names``);
    ``from .sub import a, b`` re-exports ``a`` and ``b``. An aliased name
    (``import a as b``) is skipped - CoreSymbol keys a symbol by its own
    def/class name. Submodules are returned in sorted order (deterministic).
    """
    init = package_dir / "__init__.py"
    try:
        tree = parse_external_source(
            init.read_text(encoding="utf-8", errors="ignore"), filename=str(init),
        )
    except (OSError, SyntaxError):
        return []
    names_by_file: dict[Path, set[str]] = {}
    for node in tree.body:
        if not (isinstance(node, ast.ImportFrom) and node.level == 1 and node.module):
            continue
        sub = package_dir.joinpath(*node.module.split(".")).with_suffix(".py")
        if not sub.is_file():
            continue
        if any(alias.name == "*" for alias in node.names):
            names = set(_star_export_names(sub))
        else:
            names = {a.name for a in node.names if a.asname in (None, a.name)}
        names_by_file.setdefault(sub, set()).update(names)
    return [
        (path, frozenset(names))
        for path, names in sorted(names_by_file.items())
        if names
    ]


def _resolve_core_targets(
    odoo_root: Path, logical_path: str, version: str,
    name_allowlist: frozenset[str] | None,
) -> list[tuple[Path, frozenset[str] | None]]:
    """``(real file, name allow-list)`` pairs to parse for one allow-list entry.

    The files of ``_resolve_core_paths`` keep the entry's own allow-list. When
    the logical module resolves to no file but exists as a package directory
    (v20: ``odoo/tools/safe_eval.py`` became ``odoo/tools/safe_eval/``), its
    public surface is what the package ``__init__.py`` re-exports, so each
    re-exporting submodule is parsed with the re-exported names as its
    allow-list. Symbols keep the logical module's qname
    (``odoo.tools.safe_eval.safe_eval``), which is also their import path.
    """
    paths = _resolve_core_paths(odoo_root, logical_path, version)
    if paths:
        return [
            (p, _narrow(name_allowlist, _FANOUT_NAME_ALLOWLISTS.get(_rel(p, odoo_root))))
            for p in paths
        ]
    prefix_old = "odoo/"
    prefix_new = _version_prefix(version)
    if prefix_new != prefix_old and logical_path.startswith(prefix_old):
        logical_path = prefix_new + logical_path[len(prefix_old):]
    package_dir = odoo_root / logical_path.removesuffix(".py")
    if not (package_dir / "__init__.py").is_file():
        return []
    return [
        (path, _narrow(name_allowlist, names))
        for path, names in _package_reexports(package_dir)
    ]


def parse_odoo_core(odoo_source_root: str, odoo_version: str) -> list[CoreSymbolInfo]:
    """Extract CoreSymbol from the `_CORE_FILES` allow-list. Missing files are silently skipped.

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
    # (the stable `_CORE_FILES`); a frozenset = curated v19 split-ORM files.
    targets: list[tuple[str, frozenset[str] | None]] = [(p, None) for p in _CORE_FILES]
    targets += list(_V19_CURATED_FILES.items())
    for relpath, allow in targets:
        resolved = _resolve_core_targets(root, relpath, odoo_version, allow)
        # "odoo/tools/safe_eval.py" → module_qname "odoo.tools.safe_eval"
        module_qname = relpath.removesuffix(".py").replace("/", ".")
        for full, file_allow in resolved:
            try:
                source = full.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # ADR-0037: store the path relative to the Odoo source root
            # (e.g. "odoo/orm/models.py") — never the server-absolute path.
            # CoreSymbol has no repos.local_path anchor, so the source root is
            # the only meaningful relativization base.
            try:
                rel_fp = str(full.relative_to(root))
            except ValueError:
                rel_fp = str(full)
            out.extend(_extract_from_source(
                source, module_qname, odoo_version, file_path=rel_fp,
                name_allowlist=file_allow,
            ))
    return out


def seed_framework_test_helpers(
    odoo_version: str, odoo_source_root: str | Path | None = None,
) -> list:
    """Return TestHelperInfo nodes for Odoo framework test base classes.

    Delegates to ``parser_test.seed_framework_helpers()``, itself a thin adapter
    over the version-gated ``src.indexer.framework_bases`` SSOT (issue #362) -
    not a static, version-blind set. ``odoo_source_root`` is optional (backward
    compatible with existing single-argument call sites) and, when given, is
    forwarded so the seeded menu is additionally enriched with real
    ``file_path``/``line`` from an AST parse of the checkout.

    Uses module='@framework' (MED-3) so these nodes are clearly distinct from
    addon-sourced helpers and from the '__unresolved__' GC placeholder.
    Framework helpers do NOT get a DEFINED_IN edge to any Module node.

    Returns a list of TestHelperInfo (from models.py). The writer writes them
    as TestHelper nodes with no DEFINED_IN edge.
    """
    from .parser_test import seed_framework_helpers  # noqa: PLC0415
    return seed_framework_helpers(odoo_version, odoo_source_root)
