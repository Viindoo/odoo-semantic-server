# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_python.py
import ast
import logging
import re
from pathlib import Path

from .models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from .parser_util import parse_external_source

_logger = logging.getLogger("src.indexer.parser")

# v10+ class-level field declarations: name = fields.Char(...)
FIELD_TYPES = {
    'Char', 'Text', 'Html', 'Integer', 'Float', 'Monetary', 'Boolean',
    'Date', 'Datetime', 'Binary', 'Selection', 'Many2one', 'One2many',
    'Many2many', 'Reference', 'Json', 'Properties', 'Image',
    # v13+: generic many2one without FK constraint (odoo/fields.py:2659 in v13)
    'Many2oneReference',
    # v16+: stores property definitions on a model (odoo/fields.py:3794 in v16)
    'PropertiesDefinition',
}

# M10.5 P1 — relational field types whose first positional arg (or comodel_name kwarg)
# names the target model. Used in both era2 (AST) and era1 (text-regex) extraction.
RELATIONAL_FIELD_TYPES = {"many2one", "one2many", "many2many"}

# v8/v9 _columns dict declarations: 'name': fields.<lowercase_type>(...)
# Includes legacy-only types (function, related, dummy, sparse) that disappeared in v10+.
FIELD_TYPES_LEGACY = {
    'function', 'related', 'dummy', 'sparse',
    'float', 'integer', 'char', 'text', 'html', 'boolean', 'monetary',
    'date', 'datetime', 'binary', 'selection',
    'many2one', 'one2many', 'many2many', 'reference', 'image',
    # v8/v9: specialized field backed by ir.property table (openerp/osv/fields.py:1704 in v8)
    'property',
}

MODEL_BASE_CLASSES = {
    'Model', 'TransientModel', 'AbstractModel', 'BaseModel',
    # Era1 (v8/v9) bases
    'osv', 'osv_memory', 'Model_memory', 'AbstractModel_memory',
}

# --- M7 final-D: USES_CORE_SYMBOL V1 scope (expanded from V0 per ADR-0002 §3) ---
# Hot list of Odoo core API symbols whose usage warrants migration attention.
# When user code calls any of these (via `self.X()` or direct `X(...)`), parser
# records the ref so writer_neo4j can MERGE a USES_CORE_SYMBOL edge to the
# matching CoreSymbol.
#
# V1 covers 3 categories per ADR-0002 §3:
#   - Removed: symbol gone in newer version (no in-place replacement)
#   - Signature-changed: kwarg added/removed/reordered breaking caller
#   - Moved-module / renamed-attribute: qualified_name path or option name changed
#
# False-positive suppression: _collect_module_local_defs() drops refs when user
# code defines a local symbol with the same short name in the same file (V0.5
# scope-resolver, M7 W13). V1 entries are covered by the same mechanism.
#
# Numbers in trailing comment = first-affected Odoo major version.
# Currently 19 entries; keep this list focused to limit false-positive surface (ADR-0002 §3).
_DEPRECATED_API_SYMBOLS = frozenset({
    # --- Removed (no in-place replacement, full rewrite required) ---
    "name_get",              # 18: removed → use display_name computed field
    "oldname",               # 15: field option removed → use rename + migration script
    # --- Signature-changed (kwarg/semantics breaking caller) ---
    "name_search",           # 18: operator + count semantics changed
    "safe_eval",             # 19: signature change in odoo.tools
    "fields_get",            # 18: 'attributes' kwarg semantics changed
    "_search",               # 18: keyword-only args + access_rights_uid removed
    "read_group",            # 19: deprecated → _read_group / formatted_read_group
    "default_get",           # 17: fields_list arg semantics clarified + changed
    # --- Renamed field option / attribute (declaration-site or attribute access) ---
    "group_operator",        # 18: field option → aggregator
    "track_visibility",      # 17: field option → tracking
    # --- Moved module / changed qualified path ---
    "float_compare",         # 19: odoo.tools.float_utils → odoo.tools (re-exported)
    "float_round",           # 19: same module move as float_compare
    "get_modules",           # 18: odoo.modules.get_modules path changed
    "html_escape",           # 17: markupsafe.escape preferred over odoo.tools.html_escape
    # --- odoo.tools image API — removed v13, frequent AI misuse ---
    "image_resize_image",       # 13: removed → use odoo.tools.image_process
    "image_resize_image_big",   # 13: removed → use odoo.tools.image_process
    "image_resize_image_medium",  # 13: removed → use odoo.tools.image_process
    "image_resize_image_small",   # 13: removed → use odoo.tools.image_process
    # --- odoo.tools pycompat — removed from __init__ v19 ---
    "pycompat",              # 19: dropped from odoo.tools.__init__
})


def _build_import_scope_map(tree: ast.Module) -> dict[str, str]:
    """Build a short-name → qualified-name map from top-level import statements.

    Walks only `tree.body` (top-level statements per AST Parsing Gotcha in CLAUDE.md)
    to avoid false matches from imports inside function bodies.

    Examples of what is captured:
      `import odoo`                       → {'odoo': 'odoo'}
      `import odoo as o`                  → {'o': 'odoo'}
      `from odoo import models`           → {'models': 'odoo.models'}
      `from odoo.tools import safe_eval`  → {'safe_eval': 'odoo.tools.safe_eval'}
      `from odoo.tools import safe_eval as se` → {'se': 'odoo.tools.safe_eval'}

    Only top-level `import` / `from … import` statements are processed.
    Era1 (v8/v9) text-regex path does NOT call this function — it has no AST.
    """
    scope: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                local = alias.asname if alias.asname else alias.name
                scope[local] = alias.name
        elif isinstance(stmt, ast.ImportFrom):
            module = stmt.module or ""
            for alias in stmt.names:
                local = alias.asname if alias.asname else alias.name
                qualified = f"{module}.{alias.name}" if module else alias.name
                scope[local] = qualified
    return scope


def _collect_module_local_defs(tree: ast.Module) -> set[str]:
    """Collect names defined at module level (top-level `def` and `class` names).

    Used by V0.5 filter: a bare call to `name_get(...)` where `name_get` is
    defined as a top-level function in the same file is clearly NOT a call to
    the Odoo ORM method — drop the ref.

    Only inspects `tree.body` (no nested walk) to avoid spurious matches from
    inner functions and class methods.
    """
    local: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local.add(stmt.name)
        elif isinstance(stmt, ast.ClassDef):
            local.add(stmt.name)
    return local


def _is_odoo_qualified(name: str, scope_map: dict[str, str]) -> bool:
    """Return True if `name` resolves to an Odoo-core qualified name via scope_map.

    Accepts BOTH namespaces:
      - `odoo` / `odoo.*`      → v10+ (current).
      - `openerp` / `openerp.*` → v8/v9 (the core package was named `openerp`
        before the v10 rename). In v8/v9, refs to core symbols qualified via the
        aliased-module-attribute pattern (`import openerp.tools as t; t.safe_eval()`)
        resolve through scope_map to `openerp.*`; without this branch those refs
        were silently dropped, losing USES_CORE_SYMBOL edges for v8/v9 (V9-G5).

    The exact-or-dotted check (`== ns` OR `startswith(ns + ".")`) is deliberate:
    it matches the bare package name and any dotted descendant but NOT lookalike
    tokens such as `odoox`, `openerpx`, or `openerp_foo` (no false positives).
    """
    resolved = scope_map.get(name)
    if resolved is None:
        return False
    return any(
        resolved == ns or resolved.startswith(ns + ".")
        for ns in ("odoo", "openerp")
    )


def _obj_is_odoo_alias(obj_node: ast.expr, scope_map: dict[str, str]) -> bool:
    """Return True if `obj_node` (the object in `obj.method()`) is a known odoo alias.

    Handles:
      - `Name` node: bare name like `o` where scope says `o → odoo` or `o → odoo.*`
      - `Attribute` node: chained like `odoo.models` where root is `odoo`
    """
    if isinstance(obj_node, ast.Name):
        return _is_odoo_qualified(obj_node.id, scope_map)
    if isinstance(obj_node, ast.Attribute):
        # Recursively check the root of the chain
        return _obj_is_odoo_alias(obj_node.value, scope_map)
    return False


def _extract_core_symbol_refs(
    fn_node: ast.FunctionDef,
    scope_map: dict[str, str] | None = None,
    local_defs: set[str] | None = None,
    class_is_model: bool = False,
) -> list[str]:
    """Walk a method body and return deprecated-API call names found (V0.5).

    Detection scope:
      - Attribute calls: `self.name_get()`, `self.<chain>.name_get()` → record 'name_get'
      - Direct calls: `safe_eval(...)` (after `from odoo.tools import safe_eval`)
                                                                       → record 'safe_eval'
    Only names in `_DEPRECATED_API_SYMBOLS` are surfaced. Order is insertion;
    duplicates are deduplicated to keep the list short.

    V0.5 qualified-name filter (M7 W13):
    When `scope_map` and `local_defs` are provided, each candidate ref is evaluated:

    KEEP if any of:
      (a) The bare name resolves to an `odoo.*` qualified name via scope_map.
      (b) The call is `<obj>.<name>()` where `<obj>` is a known odoo-qualified alias.
      (c) The call is `super().<name>()` inside a class that subclasses models.Model
          (ambiguous — conservative posture keeps it).
      (d) Scope is unknown: bare call with NO matching import AND NO local def in same
          file → V0 fallback (keep) for safety.

    DROP if:
      - Bare call `name(...)` where `name` is defined as a top-level function/class in
        the same file (local shadowing — clearly not the Odoo ORM method).
      - Attribute call `<obj>.<name>()` where `<obj>` is a known non-odoo alias.

    Era1 (v8/v9) text-regex path: scope_map is None → V0 behavior unchanged.
    Documented limitation: Era1 has no import scope info (CLAUDE.md v8/v9 section).
    """
    if scope_map is None:
        scope_map = {}
    if local_defs is None:
        local_defs = set()

    refs: list[str] = []
    seen: set[str] = set()

    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call):
            continue

        target: str | None = None
        call_kind: str = "unknown"  # 'bare', 'attr', 'super_attr'

        if isinstance(node.func, ast.Attribute):
            target = node.func.attr
            obj = node.func.value
            # Detect super().<name>() pattern
            if (
                isinstance(obj, ast.Call)
                and isinstance(obj.func, ast.Name)
                and obj.func.id == "super"
            ):
                call_kind = "super_attr"
            else:
                call_kind = "attr"
        elif isinstance(node.func, ast.Name):
            target = node.func.id
            call_kind = "bare"

        if not target or target not in _DEPRECATED_API_SYMBOLS or target in seen:
            continue

        # --- V0.5 filter ---
        keep = _should_keep_ref(
            target=target,
            call_kind=call_kind,
            node=node,
            scope_map=scope_map,
            local_defs=local_defs,
            class_is_model=class_is_model,
        )
        if keep:
            seen.add(target)
            refs.append(target)

    return refs


def _should_keep_ref(
    target: str,
    call_kind: str,
    node: ast.Call,
    scope_map: dict[str, str],
    local_defs: set[str],
    class_is_model: bool,
) -> bool:
    """Apply V0.5 keep/drop rules for a single candidate ref.

    Returns True (keep) or False (drop).
    """
    if call_kind == "super_attr":
        # Rule (c): super().<name>() inside a models.Model subclass → ambiguous, keep.
        # Outside model context (class_is_model=False) → still keep (conservative).
        return True

    if call_kind == "attr":
        obj = node.func.value  # type: ignore[union-attr]
        # Rule (b): obj is known odoo-qualified alias → keep.
        if _obj_is_odoo_alias(obj, scope_map):
            return True
        # obj is a known non-odoo name → drop only if we CAN identify it clearly.
        # If obj is `self` or unknown → keep (V0 fallback).
        if isinstance(obj, ast.Name):
            obj_name = obj.id
            if obj_name == "self":
                # self.<name>() — standard Odoo call pattern, keep.
                return True
            # If we know obj_name from scope → check if odoo
            if obj_name in scope_map:
                return _is_odoo_qualified(obj_name, scope_map)
            # obj_name not in scope → could be a local variable, keep (conservative).
            return True
        # Chained attribute `a.b.name()` — walk to root
        if isinstance(obj, ast.Attribute):
            return _obj_is_odoo_alias(obj, scope_map) or True  # conservative keep
        return True  # other expression types → keep

    # call_kind == "bare" (direct call)
    # Rule (a): name resolves to odoo.* via import → keep.
    if _is_odoo_qualified(target, scope_map):
        return True
    # Rule (local shadow): name defined as local top-level def/class → DROP.
    if target in local_defs:
        return False
    # Rule (d): name not in scope AND no local def → V0 fallback, keep.
    return True


# --- M4.6 WI2: Method convention classification ----------------------------
# Pure name-based regex map. Order matters — first match wins. Each entry is
# (pattern, (convention_kind, super_safety, return_required)). super_safety:
#   'always'  → MUST call super() and return its result (action_/CRUD)
#   'usually' → SHOULD call super() in most cases (helpers, builders)
#   'never'   → MUST NOT call super() (compute/inverse/search/default — Odoo
#                rebinds these via decorators, super() chain is meaningless).

_CONVENTION_MAP: list[tuple[re.Pattern, tuple[str, str, bool]]] = [
    (re.compile(r"^_compute_"),                       ("compute",  "never",   False)),
    (re.compile(r"^_inverse_"),                       ("inverse",  "never",   False)),
    (re.compile(r"^_search_"),                        ("search",   "never",   False)),
    (re.compile(r"^_get_default_|^_default_"),        ("default",  "never",   False)),
    (re.compile(r"^_get_"),                           ("builder",  "usually", False)),
    (re.compile(r"^_prepare_"),                       ("prepare",  "usually", False)),
    (re.compile(r"^_check_"),                         ("check",    "usually", False)),
    (re.compile(r"^action_"),                         ("action",   "always",  True)),
    (re.compile(r"^(create|write|unlink|copy|read)$"),
                                                      ("crud",     "always",  True)),
    (re.compile(r"^_"),                               ("private",  "usually", False)),
]
_DEFAULT_CONVENTION: tuple[str, str, bool] = ("public", "usually", False)


def _classify_method_convention(method_name: str) -> tuple[str, str, bool]:
    """Return (convention_kind, super_safety, return_required) for a method name.

    Default for any non-matching public name is ('public', 'usually', False).
    """
    for pattern, result in _CONVENTION_MAP:
        if pattern.match(method_name):
            return result
    return _DEFAULT_CONVENTION


# --- M4.6 WI1: Module edition detection ------------------------------------
# Detect ∈ {viindoo, oca, community, custom, enterprise} (enterprise = upstream
# Odoo EE labeled as OEEL-1; Viindoo stack does not ship it, so we never label
# as 'enterprise' from path alone — only via OEEL-1 license or viindoo_equivalent
# lookup surfaces EE confusion via get_ee_modules() in src/data/ee_modules.py
# per ADR-0003; WI-R F-007: wired to live DB so admin CRUD takes effect).


def _detect_module_edition(
    manifest: dict, module_name: str, module_path: str,
) -> str:
    """Detect edition of a module from manifest + name + path heuristics.

    Returns one of: 'viindoo' | 'oca' | 'community' | 'custom' | 'enterprise'.
    Order matters — earlier rules win (Viindoo > Enterprise > OCA > CE path > custom).
    """
    # Viindoo: name prefix convention (viin_* and to_* are public product names)
    if module_name.startswith(("viin_", "to_")):
        return "viindoo"
    # Enterprise: OEEL-1 license (Odoo EE, path-independent)
    license_v = (manifest.get("license") or "").upper()
    if license_v == "OEEL-1":
        return "enterprise"
    # OCA license string
    if "OCA" in license_v:
        return "oca"
    # Community: Odoo CE addons path + LGPL/GPL/AGPL
    ce_licenses = {"LGPL-3", "LGPL-3.0", "GPL-3", "GPL-3.0", "AGPL-3", "AGPL-3.0"}
    if license_v in ce_licenses:
        if "/odoo/addons/" in module_path or "/addons/" in module_path:
            return "community"
    return "custom"


def _detect_viindoo_equivalent(module_name: str) -> str | None:
    """Return Viindoo equivalent for an EE-only module from the live DB guard list.

    Uses get_ee_modules() (60 s in-process cache) so admin CRUD changes propagate
    within one cache window (WI-R F-007 fix).  Falls back to static list when
    the DB is unreachable — identical behaviour to the previous EE_CONFUSION lookup.
    """
    from src.data.ee_modules import get_ee_modules
    for entry in get_ee_modules():
        if entry["name"] == module_name:
            return entry["vt_equivalent"]
    return None


def _resolve_effective_license(manifest: dict, major: int) -> str:
    """Return the effective SPDX license identifier for a module.

    Missing 'license' key is filled by era default (ADR-0036 D1):
      - major <= 8: AGPL-3  (v8 repo base)
      - major >= 9: LGPL-3  (v9+ repo base)

    Returns the raw manifest value when present, so OEEL-1 / OPL-1 / etc.
    pass through unmodified for policy evaluation.
    """
    from src.constants import default_license_for_missing
    raw = manifest.get("license")
    if raw:
        return str(raw).strip()
    return default_license_for_missing(major)


def _derive_copyright_owner(manifest: dict, license_value: str) -> str | None:
    """Derive a copyright_owner string from manifest + license (ADR-0036 D1).

    Derivation order (first match wins):
    1. OEEL-1 → 'Odoo S.A.' (always — contractually Odoo S.A. owns OEEL)
    2. Manifest 'author' contains 'Odoo S.A.' → 'Odoo S.A.'
    3. Manifest 'author' contains 'Viindoo' or 'TVTMA' → 'Viindoo'
    4. Manifest 'author' present otherwise → author[:100]
    5. CE copyleft (LGPL/AGPL/GPL) with no author → 'Odoo S.A.' (CE default)
    6. Otherwise → None
    """
    if license_value == "OEEL-1":
        return "Odoo S.A."
    raw_author = manifest.get("author") or ""
    # Odoo manifests allow `author` as str OR list[str] (e.g. CE l10n_* modules:
    # ['Odoo S.A.', 'Vauxoo']). literal_eval preserves the native type, so coerce.
    if isinstance(raw_author, (list, tuple)):
        author = ", ".join(str(a) for a in raw_author).strip()
    else:
        author = str(raw_author).strip()
    if "Odoo S.A." in author:
        return "Odoo S.A."
    if "Viindoo" in author or "TVTMA" in author:
        return "Viindoo"
    if author:
        return author[:100]
    # No author — CE copyleft licenses default to Odoo S.A. as upstream owner
    ce_copyleft = {"LGPL-3", "LGPL-3.0", "AGPL-3", "AGPL-3.0", "GPL-3", "GPL-3.0"}
    if license_value in ce_copyleft:
        return "Odoo S.A."
    return None


def _extract_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extract_tristate_bool(node: ast.expr | None) -> bool | None:
    """Extract an explicit bool literal kwarg as tri-state (WI-1 #238).

    Returns the literal value when the kwarg is ``True``/``False``, else
    ``None`` (kwarg absent OR a non-literal expression we can't evaluate
    statically). Tri-state matters for ``readonly``: an explicit
    ``readonly=False`` must override the related/compute inference, so we
    cannot collapse "absent" and "False" into one value.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _compute_effective_readonly(
    readonly: bool | None,
    related: str | None,
    compute: str | None,
    inverse: str | None,
) -> bool:
    """Derive whether a field is effectively read-only (WI-1 #238).

    Precedence (first match wins):
      1. explicit ``readonly`` kwarg present  -> use it verbatim;
      2. ``related`` set, no compute, no inverse  -> True (stored-related is
         silently overwritten by the ORM on write);
      3. ``compute`` set, no inverse  -> True (computed-without-setter);
      4. otherwise  -> False (plain writable field, or compute/related WITH
         an inverse setter, which IS writable).
    """
    if readonly is not None:
        return readonly
    if related is not None and compute is None and inverse is None:
        return True
    if compute is not None and inverse is None:
        return True
    return False


def _extract_inherit(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.List):
        return [s for elt in node.elts if (s := _extract_string(elt))]
    return []


def _extract_inherits(node: ast.expr) -> dict[str, str]:
    result = {}
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            key = _extract_string(k)
            val = _extract_string(v)
            if key and val:
                result[key] = val
    return result


def _has_super_call(func_node: ast.FunctionDef) -> bool:
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            func = node.func
            # super().method(...)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
                inner = func.value
                if isinstance(inner.func, ast.Name) and inner.func.id == 'super':
                    return True
    return False


def _get_base_class_names(cls_node: ast.ClassDef) -> set[str]:
    names = set()
    for base in cls_node.bases:
        if isinstance(base, ast.Attribute):
            names.add(base.attr)
        elif isinstance(base, ast.Name):
            names.add(base.id)
    return names


def _extract_columns_dict_fields(dict_node: ast.Dict) -> list[FieldInfo]:
    """Extract fields from `_columns = {'name': fields.<type>(...)}` (era1 v8/v9)."""
    fields_out: list[FieldInfo] = []
    for k, v in zip(dict_node.keys, dict_node.values):
        field_name = _extract_string(k)
        if not field_name:
            continue
        if not (isinstance(v, ast.Call)
                and isinstance(v.func, ast.Attribute)
                and isinstance(v.func.value, ast.Name)
                and v.func.value.id == 'fields'):
            continue
        ttype = v.func.attr.lower()
        if ttype not in FIELD_TYPES_LEGACY:
            continue
        # A2-followup — era1 best-effort label/help. Old API: fields.char('Label',
        # help='...'); string= kwarg also seen. Positional[0] is the label only for
        # non-relational types (relational positional[0] is the comodel).
        legacy_kwargs = {kw.arg: kw.value for kw in v.keywords if kw.arg}
        legacy_string = (
            _extract_string(legacy_kwargs["string"]) if "string" in legacy_kwargs else None
        )
        if legacy_string is None and ttype not in RELATIONAL_FIELD_TYPES and v.args:
            legacy_string = _extract_string(v.args[0])
        legacy_help = (
            _extract_string(legacy_kwargs["help"]) if "help" in legacy_kwargs else None
        )
        # C2 fix — era1 comodel extraction for relational fields (AST path).
        # Positional[0] of many2one/one2many/many2many is the target model name.
        # Also honour explicit comodel_name kwarg (rare in v8/v9 but present in
        # some Odoo community modules).
        legacy_comodel: str | None = None
        if ttype in RELATIONAL_FIELD_TYPES:
            if v.args:
                legacy_comodel = _extract_string(v.args[0])
            elif "comodel_name" in legacy_kwargs:
                legacy_comodel = _extract_string(legacy_kwargs["comodel_name"])
        fields_out.append(FieldInfo(
            name=field_name, ttype=ttype,
            related=None, compute=None,
            stored=True, required=False,
            string=legacy_string,
            help=legacy_help,
            comodel_name=legacy_comodel,
        ))
    return fields_out


def _resolve_local_model_base(
    base_names: set[str],
    class_map: dict[str, "ast.ClassDef"],
    seen: set[str] | None = None,
) -> set[str] | None:
    """Walk same-file local base classes transitively until a framework base is reached.

    Returns the set of MODEL_BASE_CLASSES names ultimately inherited (e.g.
    {'TransientModel'}), or None if no framework base is reachable.  Cycle-safe
    via the ``seen`` set.  Same-file only — ``class_map`` contains only top-level
    ClassDef nodes from the current file (#285 follow-up review).
    """
    if seen is None:
        seen = set()
    for base in base_names:
        if base in seen:
            continue
        local_cls = class_map.get(base)
        if local_cls is None:
            continue
        seen.add(base)
        local_base_names = _get_base_class_names(local_cls)
        framework_bases = local_base_names & MODEL_BASE_CLASSES
        if framework_bases:
            return framework_bases
        # Recurse one level deeper into same-file local bases.
        result = _resolve_local_model_base(local_base_names, class_map, seen)
        if result is not None:
            return result
    return None


def _parse_class(
    cls_node: ast.ClassDef,
    module_info: ModuleInfo,
    source: str = "",
    scope_map: dict[str, str] | None = None,
    local_defs: set[str] | None = None,
    class_map: dict[str, ast.ClassDef] | None = None,
) -> ModelInfo | None:
    base_names = _get_base_class_names(cls_node)
    is_model_class = bool(base_names & MODEL_BASE_CLASSES)

    name = None
    inherit: list[str] = []
    inherits: dict[str, str] = {}
    is_abstract = 'AbstractModel' in base_names
    is_transient = 'TransientModel' in base_names
    fields_list: list[FieldInfo] = []
    methods_list: list[MethodInfo] = []
    has_columns_dict = False  # era1 marker — promotes class to model
    had_explicit_name = False  # set True when _name = "..." literal found

    for node in cls_node.body:
        # Normalize Assign and AnnAssign to a common (targets, value) pair so
        # the field-detection block below handles both declaration styles.
        # Meta attributes (_name, _inherit, ...) remain Assign-only - AnnAssign
        # meta is zero-occurrence across all Odoo versions (scope decision #2).
        if isinstance(node, ast.Assign):
            _targets = node.targets
            _value = node.value
        elif isinstance(node, ast.AnnAssign):
            _targets = [node.target] if isinstance(node.target, ast.Name) else []
            _value = node.value  # may be None for bare annotations (cr: T)
        else:
            _targets = []
            _value = None

        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                attr = target.id
                if attr == '_name':
                    name = _extract_string(node.value)
                    if name is not None:
                        had_explicit_name = True
                elif attr == '_inherit':
                    inherit = _extract_inherit(node.value)
                elif attr == '_inherits':
                    inherits = _extract_inherits(node.value)
                elif attr == '_abstract' and isinstance(node.value, ast.Constant):
                    is_abstract = bool(node.value.value)
                elif attr == '_transient' and isinstance(node.value, ast.Constant):
                    is_transient = bool(node.value.value)
                elif attr == '_columns' and isinstance(node.value, ast.Dict):
                    fields_list.extend(_extract_columns_dict_fields(node.value))
                    has_columns_dict = True

        # Field detection: field_name = fields.FieldType(...)  (era2 v10+)
        # Also handles: field_name: Annotation = fields.FieldType(...)  (v18+)
        if (_value is not None
                and isinstance(_value, ast.Call)
                and isinstance(_value.func, ast.Attribute)
                and isinstance(_value.func.value, ast.Name)
                and _value.func.value.id == 'fields'
                and _value.func.attr in FIELD_TYPES
                and _targets
                and isinstance(_targets[0], ast.Name)):
                call = _value
                field_name = _targets[0].id
                field_type = call.func.attr.lower()
                kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}

                related = _extract_string(kwargs['related']) if 'related' in kwargs else None
                compute = _extract_string(kwargs['compute']) if 'compute' in kwargs else None
                required = bool(getattr(kwargs.get('required'), 'value', False))
                # WI-1 (#238) — writability signals. readonly is tri-state
                # (explicit literal vs absent); inverse is the setter method name.
                readonly = _extract_tristate_bool(kwargs.get('readonly'))
                inverse = _extract_string(kwargs['inverse']) if 'inverse' in kwargs else None
                effective_readonly = _compute_effective_readonly(
                    readonly, related, compute, inverse
                )
                # store kwarg: computed and related fields default to store=False
                if 'store' in kwargs:
                    stored = bool(getattr(kwargs['store'], 'value', True))
                else:
                    stored = (compute is None and related is None)

                # M10.5 P1 — extract comodel for relational fields (best-effort)
                comodel: str | None = None
                if field_type in RELATIONAL_FIELD_TYPES:
                    if call.args:
                        comodel = _extract_string(call.args[0])
                    elif "comodel_name" in kwargs:
                        comodel = _extract_string(kwargs["comodel_name"])

                # A2-followup — field label + help text (intent for AI agents).
                # `string=` kwarg; else the first positional arg is the label for
                # NON-relational, NON-selection/reference fields only.
                # Exclusions (positional[0] is NOT a label):
                #   - Relational fields: positional[0] is the comodel name.
                #   - Selection: positional[0] is the selection list or a method name.
                #   - Reference: positional[0] is the selection list of (model,label) pairs.
                # F-14 fix: guard against mislabeling Selection/Reference positional args.
                _POSITIONAL_LABEL_EXCLUDED = RELATIONAL_FIELD_TYPES | {"selection", "reference"}
                field_string = (
                    _extract_string(kwargs["string"]) if "string" in kwargs else None
                )
                if (
                    field_string is None
                    and field_type not in _POSITIONAL_LABEL_EXCLUDED
                    and call.args
                ):
                    field_string = _extract_string(call.args[0])
                field_help = _extract_string(kwargs["help"]) if "help" in kwargs else None

                src_def = (
                    ast.get_source_segment(source, node)
                    if source else None
                )
                fields_list.append(FieldInfo(
                    name=field_name, ttype=field_type,
                    related=related, compute=compute,
                    stored=stored, required=required,
                    source_definition=src_def,
                    comodel_name=comodel,
                    line=node.lineno,  # A3: 1-based line of the field assignment (era2)
                    string=field_string,
                    help=field_help,
                    readonly=readonly,
                    inverse=inverse,
                    effective_readonly=effective_readonly,
                ))

        elif isinstance(node, ast.FunctionDef) and not node.name.startswith('__'):
            decorators = []
            depends: list[str] = []
            for dec in node.decorator_list:
                if isinstance(dec, ast.Attribute):
                    decorators.append(f'api.{dec.attr}')
                elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    decorators.append(f'api.{dec.func.attr}')
                    # M10.5 P2 — capture @api.depends('a.b', 'c') string args for
                    # validate_depends. _extract_string returns None for lambda/
                    # callable args, so dynamic depends are skipped (not resolvable).
                    if dec.func.attr == 'depends':
                        depends.extend(
                            s for arg in dec.args if (s := _extract_string(arg))
                        )
                elif isinstance(dec, ast.Name):
                    decorators.append(dec.id)

            method_src = (
                ast.get_source_segment(source, node)
                if source else None
            )
            try:
                sig = ast.unparse(node.args)
            except (AttributeError, ValueError):
                sig = None
            ck, ss, rr = _classify_method_convention(node.name)

            # A2a — capture docstring (era2 only)
            method_docstring = ast.get_docstring(node)

            # A2d — collect direct self.<x> attribute access names from method body.
            # Only captures top-level self.x: for self.partner_id.name, only
            # 'partner_id' is captured because .name has value=Attribute(value=Name('self'))
            # which satisfies the condition only for the first-level node.
            _field_ref_set: set[str] = set()
            for _attr_node in ast.walk(node):
                if (
                    isinstance(_attr_node, ast.Attribute)
                    and isinstance(_attr_node.value, ast.Name)
                    and _attr_node.value.id == "self"
                ):
                    _field_ref_set.add(_attr_node.attr)
            _field_refs = sorted(_field_ref_set)  # deterministic ordering

            methods_list.append(MethodInfo(
                name=node.name,
                has_super_call=_has_super_call(node),
                decorators=decorators,
                source_code=method_src,
                core_symbol_refs=_extract_core_symbol_refs(
                    node,
                    scope_map=scope_map,
                    local_defs=local_defs,
                    class_is_model=is_model_class,
                ),
                convention_kind=ck,
                super_safety=ss,
                return_required=rr,
                signature=sig,
                depends=depends,
                docstring=method_docstring,
                field_refs=_field_refs,
                line=node.lineno,  # A3: 1-based line of `def` statement (era2)
            ))

    # _inherit without _name → name = inherit[0] (Odoo convention)
    if not name and inherit:
        name = inherit[0]

    # Not an Odoo model if no _name and not a Model subclass + no _columns dict
    if not name:
        return None

    # Same-file local base-class widening (#285, review): when the class's Python bases
    # are not framework bases (e.g. ``CashBoxIn(CashBox)`` where ``CashBox`` is a
    # same-file ``TransientModel`` subclass), walk the same-file local base chain
    # transitively (cycle-safe) to reach a framework base.  Only promote when the
    # subclass has an explicit ``_name`` or ``_inherit`` so plain helper subclasses
    # are not falsely indexed.  Propagate is_abstract/is_transient from the resolved
    # framework base so Neo4j gets correct model-type flags.
    if not is_model_class and (had_explicit_name or inherit) and class_map:
        resolved = _resolve_local_model_base(base_names, class_map)
        if resolved:
            is_model_class = True
            is_abstract = is_abstract or ('AbstractModel' in resolved)
            is_transient = is_transient or ('TransientModel' in resolved)

    if not is_model_class and not inherit and not inherits and not has_columns_dict:
        return None

    return ModelInfo(
        name=name,
        module=module_info.name,
        odoo_version=module_info.odoo_version,
        is_abstract=is_abstract,
        is_transient=is_transient,
        inherit=inherit,
        inherits=inherits,
        fields=fields_list,
        methods=methods_list,
        had_explicit_name=had_explicit_name,
    )


def _parse_era2_ast(
    source: str, module_info: ModuleInfo, filename: str | None = None,
) -> list[ModelInfo]:
    """Modern AST parser (v10+ and v8/v9 when source happens to be Py3-compatible).

    Builds a per-file import scope map, module-level local def set, and a
    class-name-to-node map once, then passes them into each _parse_class call so
    that _extract_core_symbol_refs (V0.5) can filter false-positive
    USES_CORE_SYMBOL refs caused by local name collisions, and so that same-file
    local base classes can be resolved for model-class detection (#285).
    """
    # External third-party addon source — scope away SyntaxWarning noise, pass the
    # real path so any diagnostic is attributable (not <unknown>). See parser_util.
    tree = parse_external_source(source, filename=filename)
    scope_map = _build_import_scope_map(tree)
    local_defs = _collect_module_local_defs(tree)
    # class_map: short-name → ClassDef for all top-level classes in this file.
    # Used by _parse_class to resolve same-file local base classes (#285).
    class_map: dict[str, ast.ClassDef] = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    models = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            model = _parse_class(
                node, module_info, source=source,
                scope_map=scope_map, local_defs=local_defs,
                class_map=class_map,
            )
            if model:
                models.append(model)
    return models


def parse_file(filepath: str, module_info: ModuleInfo) -> list[ModelInfo]:
    """Parse a Python file → list[ModelInfo]. Era-aware dispatch (M4.5 WI1.2).

    Primary path is the AST parser for ALL eras. On ``SyntaxError`` (a file that
    Python 3's ``ast`` cannot parse — e.g. residual Python-2 idioms that survived
    a forked v10 checkout, or a stray bad file on the v19 dev branch), fall back
    to the era1 text-regex extractor so the file's model IDENTITY (``_name`` /
    ``_inherit`` / best-effort fields) is still recovered instead of silently
    dropping the whole file. The fallback is reached ONLY on SyntaxError, so
    syntactically-valid files are never affected (#285, ADR-0032 graceful
    degradation; era1=v8/v9 already did this — era2 now matches).
    """
    try:
        source = Path(filepath).read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return []

    try:
        models = _parse_era2_ast(source, module_info, filename=filepath)
    except SyntaxError as exc:
        # Graceful degradation: a SyntaxError means ast.parse rejected the file.
        # Recover model identity via the text-regex extractor rather than
        # dropping every model in the file (the #285 orphan bug). Logged so a
        # future straggler is visible, never silent.
        _logger.warning(
            "parse_file: ast.parse failed for %s (Odoo %s): %s — "
            "falling back to text-regex extraction",
            filepath, module_info.odoo_version, exc,
        )
        models = _parse_era1_text(source, module_info)
        if not models:
            _logger.error(
                "parse_file: text-regex fallback recovered 0 models from %s "
                "(Odoo %s) — models in this file are LOST",
                filepath, module_info.odoo_version,
            )

    # A3: stamp real source file path on every returned ModelInfo
    for m in models:
        m.file_path = filepath
    return models


def parse_module(module_info: ModuleInfo) -> ParseResult:
    """Parse all Python files in a module directory."""
    result = ParseResult(module=module_info)
    module_path = Path(module_info.path)

    SKIP_DIRS = {'.git', 'static', 'migrations', 'tests', '__pycache__'}

    for py_file in sorted(module_path.rglob('*.py')):
        if py_file.name == '__manifest__.py':
            continue
        if SKIP_DIRS & set(py_file.parts):
            continue
        models = parse_file(str(py_file), module_info)
        result.models.extend(models)

    return result


# --- Era1 (v8/v9 text-regex) dispatch hook ----------------------------------
# Era1 lives in parser_python_era1.py (B4 split). Imported at the BOTTOM (after
# all shared constants/_classify_method_convention are defined above) so the
# era1 module's `from .parser_python import FIELD_TYPES, ...` resolves against
# this partially-loaded module without a circular import. parse_file's era1
# dispatch above resolves `_parse_era1_text` through this module-level name, so
# this import is a genuine facade-internal dependency (NOT a re-export shim).
from .parser_python_era1 import _parse_era1_text  # noqa: E402  (bottom import breaks cycle)
