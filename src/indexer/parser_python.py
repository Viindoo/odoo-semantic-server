# src/indexer/parser_python.py
import ast
import io
import re
import tokenize
from pathlib import Path

from src.constants import LEGACY_ERA_MAX_MAJOR

from .models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult

# v10+ class-level field declarations: name = fields.Char(...)
FIELD_TYPES = {
    'Char', 'Text', 'Html', 'Integer', 'Float', 'Monetary', 'Boolean',
    'Date', 'Datetime', 'Binary', 'Selection', 'Many2one', 'One2many',
    'Many2many', 'Reference', 'Json', 'Properties', 'Image',
}

# v8/v9 _columns dict declarations: 'name': fields.<lowercase_type>(...)
# Includes legacy-only types (function, related, dummy, sparse) that disappeared in v10+.
FIELD_TYPES_LEGACY = {
    'function', 'related', 'dummy', 'sparse',
    'float', 'integer', 'char', 'text', 'html', 'boolean', 'monetary',
    'date', 'datetime', 'binary', 'selection',
    'many2one', 'one2many', 'many2many', 'reference', 'image',
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
# Cap at 15 entries to keep false-positive surface manageable (ADR-0002 §3).
_DEPRECATED_API_SYMBOLS = frozenset({
    # --- Removed (no in-place replacement, full rewrite required) ---
    "name_get",          # 18: removed → use display_name computed field
    "oldname",           # 15: field option removed → use rename + migration script
    # --- Signature-changed (kwarg/semantics breaking caller) ---
    "name_search",       # 18: operator + count semantics changed
    "safe_eval",         # 19: signature change in odoo.tools
    "fields_get",        # 18: 'attributes' kwarg semantics changed
    "_search",           # 18: keyword-only args + access_rights_uid removed
    "read_group",        # 19: deprecated → _read_group / formatted_read_group
    "default_get",       # 17: fields_list arg semantics clarified + changed
    # --- Renamed field option / attribute (declaration-site or attribute access) ---
    "group_operator",    # 18: field option → aggregator
    "track_visibility",  # 17: field option → tracking
    # --- Moved module / changed qualified path ---
    "float_compare",     # 19: odoo.tools.float_utils → odoo.tools (re-exported)
    "float_round",       # 19: same module move as float_compare
    "get_modules",       # 18: odoo.modules.get_modules path changed
    "html_escape",       # 17: markupsafe.escape preferred over odoo.tools.html_escape
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
    """Return True if `name` resolves to an `odoo.*` qualified name via scope_map."""
    resolved = scope_map.get(name)
    return resolved is not None and (
        resolved == "odoo" or resolved.startswith("odoo.")
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
# lookup surfaces EE confusion via EE_CONFUSION dict in src/data/ee_modules.py
# per ADR-0003).


def _detect_module_edition(
    manifest: dict, module_name: str, module_path: str,
) -> str:
    """Detect edition of a module from manifest + name + path heuristics.

    Returns one of: 'viindoo' | 'oca' | 'community' | 'custom' | 'enterprise'.
    Order matters — earlier rules win (Viindoo > Enterprise > OCA > CE path > custom).
    """
    # Viindoo: name prefix or path
    if module_name.startswith(("viin_", "to_")):
        return "viindoo"
    if any(seg in module_path for seg in ("acme_addons", "acme_enterprise")):
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
    """Lookup EE_CONFUSION dict for the Viindoo equivalent of an EE-only module."""
    from src.data.ee_modules import EE_CONFUSION
    return EE_CONFUSION.get(module_name)


def _detect_era(odoo_version: str) -> str:
    """era1: Odoo v8/v9 (Python 2, _columns dict). era2: v10+ (modern AST)."""
    try:
        major = int(odoo_version.split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return "era2"
    return "era1" if major <= LEGACY_ERA_MAX_MAJOR else "era2"


def _extract_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


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
        fields_out.append(FieldInfo(
            name=field_name, ttype=ttype,
            related=None, compute=None,
            stored=True, required=False,
        ))
    return fields_out


def _parse_class(
    cls_node: ast.ClassDef,
    module_info: ModuleInfo,
    source: str = "",
    scope_map: dict[str, str] | None = None,
    local_defs: set[str] | None = None,
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
            if (isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and isinstance(node.value.func.value, ast.Name)
                    and node.value.func.value.id == 'fields'
                    and node.value.func.attr in FIELD_TYPES
                    and node.targets
                    and isinstance(node.targets[0], ast.Name)):
                call = node.value
                field_name = node.targets[0].id
                field_type = call.func.attr.lower()
                kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}

                related = _extract_string(kwargs['related']) if 'related' in kwargs else None
                compute = _extract_string(kwargs['compute']) if 'compute' in kwargs else None
                required = bool(getattr(kwargs.get('required'), 'value', False))
                # store kwarg: computed and related fields default to store=False
                if 'store' in kwargs:
                    stored = bool(getattr(kwargs['store'], 'value', True))
                else:
                    stored = (compute is None and related is None)

                src_def = (
                    ast.get_source_segment(source, node)
                    if source else None
                )
                fields_list.append(FieldInfo(
                    name=field_name, ttype=field_type,
                    related=related, compute=compute,
                    stored=stored, required=required,
                    source_definition=src_def,
                ))

        elif isinstance(node, ast.FunctionDef) and not node.name.startswith('__'):
            decorators = []
            for dec in node.decorator_list:
                if isinstance(dec, ast.Attribute):
                    decorators.append(f'api.{dec.attr}')
                elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    decorators.append(f'api.{dec.func.attr}')
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
            ))

    # _inherit without _name → name = inherit[0] (Odoo convention)
    if not name and inherit:
        name = inherit[0]

    # Not an Odoo model if no _name and not a Model subclass + no _columns dict
    if not name:
        return None
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


def _parse_era2_ast(source: str, module_info: ModuleInfo) -> list[ModelInfo]:
    """Modern AST parser (v10+ and v8/v9 when source happens to be Py3-compatible).

    Builds a per-file import scope map and module-level local def set once, then
    passes them into each _parse_class call so that _extract_core_symbol_refs (V0.5)
    can filter false-positive USES_CORE_SYMBOL refs caused by local name collisions.
    """
    tree = ast.parse(source)
    scope_map = _build_import_scope_map(tree)
    local_defs = _collect_module_local_defs(tree)
    models = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            model = _parse_class(
                node, module_info, source=source,
                scope_map=scope_map, local_defs=local_defs,
            )
            if model:
                models.append(model)
    return models


# --- era1 text-regex fallback (Python 2 v8/v9 source that fails ast.parse) -

_RE_CLASS_HEAD = re.compile(r"^class\s+(\w+)\s*\(([^)]*)\)\s*:", re.MULTILINE)
_RE_NAME_ASSIGN = re.compile(r"^[ \t]*_name\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_RE_INHERIT_STR = re.compile(r"^[ \t]*_inherit\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_RE_INHERIT_LIST = re.compile(
    r"^[ \t]*_inherit\s*=\s*\[([^\]]*)\]", re.MULTILINE | re.DOTALL,
)
_RE_COLUMNS_HEAD = re.compile(r"^[ \t]*_columns\s*=\s*\{", re.MULTILINE)
_RE_COLUMNS_UPDATE = re.compile(r"_columns\.update\s*\(\s*\{", re.MULTILINE)
# Era1 WI-5: Detect `_columns = X._columns.copy()` — parent fields come via
# INHERITS; copying via copy() is a Python-level convenience that doesn't change
# the model relationship. Do NOT extract fields from this line.
_RE_COLUMNS_COPY = re.compile(r"_columns\s*=\s*(\w+)\._columns\.copy\s*\(\s*\)")
_RE_COLUMN_ENTRY = re.compile(
    r"['\"](\w+)['\"]\s*:\s*fields\.(\w+)\s*\(",
)
# Era1 method extraction: optional decorator line + def <name>(self, ...)
# Group 1 = decorator (e.g. 'api.multi'); Group 2 = method name.
_RE_ERA1_METHOD = re.compile(
    r"(?:^[ \t]*@([\w.]+)\s*\n)?^[ \t]+def\s+(\w+)\s*\(\s*self\b",
    re.MULTILINE,
)


def _slice_class_body(source: str, start_pos: int, next_pos: int | None) -> str:
    return source[start_pos:next_pos] if next_pos else source[start_pos:]


def _extract_balanced_braces(text: str, start_pos: int) -> str:
    """Extract the content of a balanced `{...}` block starting at `start_pos`.

    `start_pos` must point to the character RIGHT AFTER the opening `{` in `text`.
    Returns the substring from `start_pos` up to (not including) the matching
    closing `}`, or '' if the block is not properly closed.

    Uses the same tokenizer-aware approach as `_extract_columns_block` to handle
    braces inside string literals correctly, with a naive char-scan fallback for
    Python 2 syntax that causes tokenize to fail.
    """
    fragment = text[start_pos:]

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(fragment).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Fallback: naive char scan
        depth = 1
        i = 0
        while i < len(fragment) and depth > 0:
            ch = fragment[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return fragment[:i]
            i += 1
        return ""

    lines = fragment.splitlines(keepends=True)
    line_starts: list[int] = [0]
    for ln in lines:
        line_starts.append(line_starts[-1] + len(ln))

    def tok_start_offset(tok) -> int:
        row, col = tok.start
        return line_starts[row - 1] + col

    depth = 1
    for tok in tokens:
        if tok.type == tokenize.OP:
            if tok.string == "{":
                depth += 1
            elif tok.string == "}":
                depth -= 1
                if depth == 0:
                    return fragment[:tok_start_offset(tok)]
    return ""


def _extract_columns_block(body: str) -> str:
    """Return the raw text inside `_columns = { ... }` via tokenizer-aware brace counting.

    Uses Python `tokenize` module to count only OP `{`/`}` tokens, skipping braces
    that appear inside STRING tokens (e.g. help strings with format placeholders like
    'Use {curly}' or 'closed} only'). Falls back to naive char-scan on TokenizeError
    (Python 2 syntax) with a warning log.

    Returns '' if `_columns` dict not found or block not closed.
    """
    m = _RE_COLUMNS_HEAD.search(body)
    if not m:
        return ""
    start = m.end()  # char position right after the opening '{'
    fragment = body[start:]  # everything after the initial '{'

    # Try tokenizer-based approach first.
    # We tokenize the fragment (which is the content AFTER the opening '{').
    # depth starts at 1 (we already consumed the first '{').
    #
    # Python 3.12 C tokenizer raises `tokenize.TokenError` (NOT `TokenizeError`)
    # AND can also raise `IndentationError` / `SyntaxError` when fed Python 2
    # source mid-file (Era1 path). Catch all three to fall through to the naive
    # char scan — this is the v8/v9 Phase-0 promise.
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(fragment).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Fallback: naive char scan (Python 2 syntax, balanced braces in strings
        # are allowed by convention — acceptable false-positive risk)
        depth = 1
        i = 0
        while i < len(fragment) and depth > 0:
            ch = fragment[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return fragment[:i]
            i += 1
        return ""

    # Reconstruct char offset from token positions by tracking line/col
    # tokenize gives (row, col) — we need char offset in `fragment`.
    lines = fragment.splitlines(keepends=True)
    # Pre-compute cumulative line starts for fast offset lookup
    line_starts: list[int] = [0]
    for ln in lines:
        line_starts.append(line_starts[-1] + len(ln))

    def tok_start_offset(tok) -> int:
        row, col = tok.start  # 1-based row, 0-based col
        return line_starts[row - 1] + col

    depth = 1
    for tok in tokens:
        if tok.type == tokenize.OP:
            if tok.string == "{":
                depth += 1
            elif tok.string == "}":
                depth -= 1
                if depth == 0:
                    end_offset = tok_start_offset(tok)
                    return fragment[:end_offset]
    return ""


def _parse_era1_text(source: str, module_info: ModuleInfo) -> list[ModelInfo]:
    """Best-effort regex extract for v8/v9 modules that fail ast.parse.

    Splits source by top-level `class X(...):` headers, then for each class block
    pulls out _name / _inherit / fields-from-_columns. Methods are NOT extracted
    in fallback mode — defer to era2 AST when source is Py3-parseable.
    """
    classes = list(_RE_CLASS_HEAD.finditer(source))
    if not classes:
        return []

    models: list[ModelInfo] = []
    for idx, head in enumerate(classes):
        body_start = head.end()
        body_end = classes[idx + 1].start() if idx + 1 < len(classes) else len(source)
        body = source[body_start:body_end]

        name_match = _RE_NAME_ASSIGN.search(body)
        name = name_match.group(1) if name_match else None
        had_explicit_name = name is not None  # True when _name = "..." regex matched

        inherit: list[str] = []
        if (m := _RE_INHERIT_STR.search(body)):
            inherit = [m.group(1)]
        elif (m := _RE_INHERIT_LIST.search(body)):
            items = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
            inherit = items

        if not name and inherit:
            name = inherit[0]
            # had_explicit_name stays False — name was auto-derived from _inherit

        # Fields from _columns dict
        cols_block = _extract_columns_block(body)
        fields_list: list[FieldInfo] = []
        if cols_block:
            for fm in _RE_COLUMN_ENTRY.finditer(cols_block):
                field_name = fm.group(1)
                ttype = fm.group(2).lower()
                if ttype not in FIELD_TYPES_LEGACY:
                    continue
                fields_list.append(FieldInfo(
                    name=field_name, ttype=ttype,
                    related=None, compute=None,
                    stored=True, required=False,
                ))

        # Fields from _columns.update({...}) calls — may appear with or without
        # a prior `_columns = {...}` assignment (WI-4).
        for upd_match in _RE_COLUMNS_UPDATE.finditer(body):
            # upd_match.end() points to char right after the opening '{'
            upd_block = _extract_balanced_braces(body, upd_match.end())
            for fm in _RE_COLUMN_ENTRY.finditer(upd_block):
                field_name = fm.group(1)
                ttype = fm.group(2).lower()
                if ttype not in FIELD_TYPES_LEGACY:
                    continue
                fields_list.append(FieldInfo(
                    name=field_name, ttype=ttype,
                    related=None, compute=None,
                    stored=True, required=False,
                ))

        # Detect `_columns = X._columns.copy()` pattern (WI-5).
        # Parent fields already represented via INHERITS; copying via copy() is
        # Python-level convenience. Do NOT extract fields — they're duplicates.
        for copy_match in _RE_COLUMNS_COPY.finditer(body):
            # copy_match.group(1) would be the parent class name (e.g. 'ParentCls')
            # We detect and skip — no field extraction from this line.
            pass

        if not name:
            continue

        # Extract methods via regex — only def <name>(self, ...) indented in class
        methods_list: list[MethodInfo] = []
        for mm in _RE_ERA1_METHOD.finditer(body):
            decorator = mm.group(1)  # may be None if no decorator
            method_name = mm.group(2)
            ck, ss, rr = _classify_method_convention(method_name)
            methods_list.append(MethodInfo(
                name=method_name,
                has_super_call=False,
                decorators=[decorator] if decorator else [],
                core_symbol_refs=[],
                convention_kind=ck,
                super_safety=ss,
                return_required=rr,
            ))

        models.append(ModelInfo(
            name=name,
            module=module_info.name,
            odoo_version=module_info.odoo_version,
            inherit=inherit,
            inherits={},
            fields=fields_list,
            methods=methods_list,
            had_explicit_name=had_explicit_name,
        ))
    return models


def parse_file(filepath: str, module_info: ModuleInfo) -> list[ModelInfo]:
    """Parse a Python file → list[ModelInfo]. Era-aware dispatch (M4.5 WI1.2):

    - era2 (v10+): AST only. SyntaxError → return [].
    - era1 (v8/v9): try AST first; fall back to text-regex on SyntaxError
      (Python 2-only syntax like `print 'x'`, `except E, e:`).
    """
    try:
        source = Path(filepath).read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return []

    era = _detect_era(module_info.odoo_version)

    if era == "era1":
        try:
            return _parse_era2_ast(source, module_info)
        except SyntaxError:
            return _parse_era1_text(source, module_info)

    # era2: AST-only
    try:
        return _parse_era2_ast(source, module_info)
    except SyntaxError:
        return []


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
