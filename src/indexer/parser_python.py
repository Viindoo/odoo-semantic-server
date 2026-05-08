# src/indexer/parser_python.py
import ast
import re
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

    for node in cls_node.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                attr = target.id
                if attr == '_name':
                    name = _extract_string(node.value)
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
            methods_list.append(MethodInfo(
                name=node.name,
                has_super_call=_has_super_call(node),
                decorators=decorators,
                source_code=method_src,
                core_symbol_refs=_extract_core_symbol_refs(node),
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
_RE_COLUMN_ENTRY = re.compile(
    r"['\"](\w+)['\"]\s*:\s*fields\.(\w+)\s*\(",
)


def _slice_class_body(source: str, start_pos: int, next_pos: int | None) -> str:
    return source[start_pos:next_pos] if next_pos else source[start_pos:]


def _extract_columns_block(body: str) -> str:
    """Return the raw text inside `_columns = { ... }` via brace counting, or ''."""
    m = _RE_COLUMNS_HEAD.search(body)
    if not m:
        return ""
    start = m.end()  # position right after '{'
    depth = 1
    i = start
    while i < len(body) and depth > 0:
        ch = body[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return body[start:i]
        i += 1
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

        inherit: list[str] = []
        if (m := _RE_INHERIT_STR.search(body)):
            inherit = [m.group(1)]
        elif (m := _RE_INHERIT_LIST.search(body)):
            items = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
            inherit = items

        if not name and inherit:
            name = inherit[0]

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

        if not name:
            continue

        models.append(ModelInfo(
            name=name,
            module=module_info.name,
            odoo_version=module_info.odoo_version,
            inherit=inherit,
            inherits={},
            fields=fields_list,
            methods=[],
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
