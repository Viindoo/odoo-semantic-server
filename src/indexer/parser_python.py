# src/indexer/parser_python.py
import ast
import io
import re
import tokenize
from pathlib import Path

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

# --- M4.5 WI6: USES_CORE_SYMBOL V0 scope -----------------------------------
# Hot list of deprecated/removed Odoo core API symbols. When user code calls
# any of these (via `self.X()` or direct `X(...)`), parser records the ref so
# writer_neo4j can MERGE a USES_CORE_SYMBOL edge to the matching CoreSymbol.
# V0 stays small to keep noise low; M6 expands per audit data + ADR-0002 §3.
_DEPRECATED_API_SYMBOLS = frozenset({
    "name_get",         # removed v18 (use display_name)
    "name_search",      # signature changed v17 → v18
    "read_group",       # deprecated v19 (use _read_group / formatted_read_group)
    "group_operator",   # field option renamed → aggregator v18
    "safe_eval",        # signature change v19
})


def _extract_core_symbol_refs(fn_node: ast.FunctionDef) -> list[str]:
    """Walk a method body and return deprecated-API call names found.

    Detection scope:
      - Attribute calls: `self.name_get()`, `self.<chain>.name_get()` → record 'name_get'
      - Direct calls: `safe_eval(...)` (after `from odoo.tools import safe_eval`)
                                                                       → record 'safe_eval'
    Only names in `_DEPRECATED_API_SYMBOLS` are surfaced. Order is insertion;
    duplicates are deduplicated to keep the list short.

    V0 false-positive scope (per ADR-0002 §3):
    - This function emits *candidate* refs — short names like 'name_get' or 'safe_eval'.
    - The writer side (writer_neo4j.py write_results) creates USES_CORE_SYMBOL edges
      ONLY when a matching CoreSymbol exists in the DB with status IN ('deprecated',
      'removed'). This means:
        1. If CoreSymbol not indexed → silent skip (no ghost node).
        2. If CoreSymbol exists but status='stable' → skip (V0 scope, noise reduction).
        3. Method named 'name_get' that is NOT calling the Odoo ORM method (e.g. a
           local helper named identically) → false-positive. The writer WHERE clause
           `qualified_name ENDS WITH '.' + $ref` narrows the match but cannot eliminate
           all false positives from short-name collisions.
    Full symbol-resolution (qualified_name from import chain tracking) is deferred to
    M6. V0 provides actionable signal with acceptable false-positive rate for
    deprecated/removed APIs.
    """
    refs: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call):
            continue
        target = None
        if isinstance(node.func, ast.Attribute):
            target = node.func.attr
        elif isinstance(node.func, ast.Name):
            target = node.func.id
        if target and target in _DEPRECATED_API_SYMBOLS and target not in seen:
            seen.add(target)
            refs.append(target)
    return refs


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
    if any(seg in module_path for seg in ("tvtmaaddons", "erponline-enterprise")):
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
    return "era1" if major <= 9 else "era2"


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
    cls_node: ast.ClassDef, module_info: ModuleInfo, source: str = ""
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
                core_symbol_refs=_extract_core_symbol_refs(node),
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
    """Modern AST parser (v10+ and v8/v9 when source happens to be Py3-compatible)."""
    tree = ast.parse(source)
    models = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            model = _parse_class(node, module_info, source=source)
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
