# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_python_era1.py
"""Era1 (v8/v9) text-regex Python parser.

Extracted from parser_python.py (B4 structural split, no behaviour change).

Era1 covers Odoo v8/v9 source that fails ``ast.parse`` (Python 2-only syntax
like ``print 'x'`` or ``except E, e:``). This module is the text-regex fallback:
it splits source by top-level ``class X(...):`` headers and pulls out
``_name`` / ``_inherit`` / fields-from-``_columns`` via regex + balanced-brace
scanning.

Shared constants (``FIELD_TYPES``, ``FIELD_TYPES_LEGACY``,
``RELATIONAL_FIELD_TYPES``) and the shared ``_classify_method_convention``
helper are imported from ``parser_python`` (one-directional: this module never
imports the era2 AST machinery). ``parser_python.parse_file`` reaches the entry
point ``_parse_era1_text`` through a bottom-of-module re-export, so the era-aware
dispatch keeps working without a circular import.
"""
import io
import re
import tokenize

from .models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo

# NOTE: shared constants (FIELD_TYPES / FIELD_TYPES_LEGACY / RELATIONAL_FIELD_TYPES)
# and _classify_method_convention live in parser_python.py, which imports THIS
# module at the bottom of its body for re-export. Importing them at module level
# here forms an import cycle that breaks a cold `import parser_python_era1` (the
# bottom re-export sees this module only partially initialized). They are used
# only inside _parse_era1_text, so we import them lazily there instead.

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
# M10.5 P1 — era1 comodel extraction: matches the first string literal argument
# inside a relational field call, e.g. fields.many2one('res.partner', ...).
# Best-effort: dynamic comodel (variable reference) → no match → None (OK).
_RE_ERA1_COMODEL = re.compile(r"fields\.\w+\(\s*['\"]([^'\"]+)['\"]")

# V9-G2 safety-net: matches new-API class-attr field declarations in Python 2
# source that fails ast.parse. Runs ONLY inside the era1 text fallback when AST
# fails. Captures: group(1)=field_name, group(2)=FieldType (capitalized era2 name).
# Examples matched:  amount = fields.Float(...)
#                    partner_id = fields.Many2one('res.partner')
#                    amount_tax = fields.Monetary(compute='_compute_tax', store=True)
# Intentionally simple — does NOT handle complex multi-line calls or decorators.
# The AST path handles those; this regex is a best-effort fallback only.
_RE_ERA1_NEWAPI_FIELD = re.compile(
    r"^[ \t]+(\w+)\s*=\s*fields\.([A-Z][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)


def _slice_class_body(source: str, start_pos: int, next_pos: int | None) -> str:
    return source[start_pos:next_pos] if next_pos else source[start_pos:]


def _string_aware_brace_scan(fragment: str, open_depth: int = 1) -> str:
    """Balanced-paren scanner that correctly skips string literals and comments.

    Scans `fragment` starting with an assumed depth of `open_depth` (i.e. the
    opening brace that initiated this scan has already been consumed).  Returns
    the substring up to (but not including) the character that brings depth back
    to zero.  Returns '' if the block is not properly closed.

    Handles:
    - Single/double-quoted strings including escaped quotes (``\\'`` / ``\\\"``).
    - Triple-quoted strings (``\"\"\"`` / ``'''``).
    - Line comments (``#`` to end of line).
    - All brace/bracket pairs: ``{}`` ``[]`` ``()`` — depth tracks ``{`` only,
      but ``[]`` and ``()`` are parsed over correctly so a ``}`` inside a list
      argument does not confuse the counter.

    This is used as the fallback when Python's ``tokenize`` module cannot handle
    Python 2 source (raises ``IndentationError`` / ``SyntaxError``).
    """
    depth = open_depth
    i = 0
    n = len(fragment)
    while i < n:
        ch = fragment[i]

        # --- Skip comment to end of line ---
        if ch == '#':
            while i < n and fragment[i] != '\n':
                i += 1
            continue

        # --- Skip string literals (single, double, triple-quoted) ---
        if ch in ('"', "'"):
            # Detect triple-quote
            if fragment[i:i + 3] in ('"""', "'''"):
                q = fragment[i:i + 3]
                i += 3
            else:
                q = ch
                i += 1
            # Scan forward to end of string
            while i < n:
                if fragment[i] == '\\':
                    i += 2  # skip escaped character
                    continue
                if fragment[i:i + len(q)] == q:
                    i += len(q)
                    break
                i += 1
            continue

        # --- Track brace depth ---
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return fragment[:i]

        i += 1
    return ""


def _extract_balanced_braces(text: str, start_pos: int) -> str:
    """Extract the content of a balanced `{...}` block starting at `start_pos`.

    `start_pos` must point to the character RIGHT AFTER the opening `{` in `text`.
    Returns the substring from `start_pos` up to (not including) the matching
    closing `}`, or '' if the block is not properly closed.

    Uses the same tokenizer-aware approach as `_extract_columns_block` to handle
    braces inside string literals correctly, with a string-aware balanced-paren
    fallback for Python 2 syntax that causes tokenize to fail.
    """
    fragment = text[start_pos:]

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(fragment).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Fallback: string-aware balanced scanner (handles } inside help strings)
        return _string_aware_brace_scan(fragment)

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
    'Use {curly}' or 'closed} only'). Falls back to `_string_aware_brace_scan` on
    TokenizeError (Python 2 syntax) — string-aware so that ``}`` inside help strings
    does not truncate the block prematurely.

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
    # source mid-file (Era1 path). Catch all three to fall through to the
    # string-aware scanner — this is the v8/v9 Phase-0 promise.
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(fragment).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Fallback: string-aware balanced scanner.
        # Handles '}' inside string literals (e.g. domain="[('type','=','other')]"
        # or help='Use } as delimiter') that the naive char-scan would miscount.
        return _string_aware_brace_scan(fragment)

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


# Matches the full entry from the dict key to the end of the balanced call:
# 'field_name': fields.type(arg1, arg2, ...) — spanning multiple lines.
# Group 1 = field name, Group 2 = field type.
_RE_ERA1_ENTRY_START = re.compile(
    r"['\"](\w+)['\"]\s*:\s*fields\.(\w+)\s*\(",
)


def _extract_era1_field_entry(block: str, match_start: int) -> str:
    """Return the full source text of a _columns entry starting at `match_start`.

    `match_start` is the ``.start()`` of a ``_RE_ERA1_ENTRY_START`` match, which
    points to the opening quote of the field name (e.g. ``'amount_total': ...``).
    Extracts from the opening quote up to and including the matching closing ``)``
    of the ``fields.type(...)`` call, spanning multiple lines if needed.

    Returns the raw text slice (suitable for ``source_definition``), or '' on
    failure.
    """
    # Find the opening '(' of fields.type( — the first '(' after match_start
    paren_pos = block.find('(', match_start)
    if paren_pos == -1:
        return ""
    # Walk forward past the opening '(' with balanced-paren, string-aware scan
    depth = 1
    i = paren_pos + 1
    n = len(block)
    while i < n and depth > 0:
        ch = block[i]
        if ch == '#':
            while i < n and block[i] != '\n':
                i += 1
            continue
        if ch in ('"', "'"):
            if block[i:i + 3] in ('"""', "'''"):
                q = block[i:i + 3]
                i += 3
            else:
                q = ch
                i += 1
            while i < n:
                if block[i] == '\\':
                    i += 2
                    continue
                if block[i:i + len(q)] == q:
                    i += len(q)
                    break
                i += 1
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return block[match_start:i + 1]
        i += 1
    return ""


def _era1_base_short_names(bases_src: str) -> set[str]:
    """Short base-class names from a ``class X(<bases_src>):`` header.

    ``'models.TransientModel, SomeMixin'`` → ``{'TransientModel', 'SomeMixin'}``.
    Keyword bases (``metaclass=...``) and empty entries are ignored; only the
    last dotted segment is kept (``osv.osv`` → ``osv``).
    """
    names: set[str] = set()
    for raw in bases_src.split(','):
        token = raw.strip()
        if not token or '=' in token:
            continue
        names.add(token.rsplit('.', 1)[-1])
    return names


def _resolve_era1_framework_bases(
    base_names: set[str],
    class_bases: dict[str, set[str]],
    framework_bases: set[str],
    seen: set[str] | None = None,
) -> set[str]:
    """Walk same-file local base classes (by name) until framework bases are reached.

    Text-regex mirror of ``parser_python._resolve_local_model_base`` (era1 has no
    AST node map). Returns the subset of ``framework_bases`` (``MODEL_BASE_CLASSES``)
    reachable from ``base_names`` directly or via same-file local base classes
    (e.g. ``CashBoxIn(CashBox)`` where ``CashBox(TransientModel)``). Empty set when
    none is reachable. Cycle-safe via ``seen``.
    """
    if seen is None:
        seen = set()
    direct = base_names & framework_bases
    if direct:
        return direct
    for base in base_names:
        if base in seen:
            continue
        seen.add(base)
        local = class_bases.get(base)
        if local is None:
            continue
        result = _resolve_era1_framework_bases(local, class_bases, framework_bases, seen)
        if result:
            return result
    return set()


def _parse_era1_text(source: str, module_info: ModuleInfo) -> list[ModelInfo]:
    """Best-effort regex extract for modules that fail ast.parse (v8/v9, or any
    version whose source hits a SyntaxError and falls back here — see #285).

    Splits source by top-level `class X(...):` headers, then for each class block
    pulls out _name / _inherit / fields-from-_columns, method names + decorators,
    and the is_transient / is_abstract model-type flags (resolved from the class's
    framework base, transitively through same-file local bases — mirrors era2).
    """
    # Lazy import (see module-top NOTE): breaks the parser_python <-> era1 cycle
    # so this module stays cold-importable.
    from .parser_python import (
        FIELD_TYPES,
        FIELD_TYPES_LEGACY,
        MODEL_BASE_CLASSES,
        RELATIONAL_FIELD_TYPES,
        _classify_method_convention,
    )

    classes = list(_RE_CLASS_HEAD.finditer(source))
    if not classes:
        return []

    # name → direct base short-names, for is_transient/is_abstract resolution
    # (mirrors era2 _parse_class + _resolve_local_model_base). #285 follow-up.
    class_bases: dict[str, set[str]] = {
        head.group(1): _era1_base_short_names(head.group(2)) for head in classes
    }

    models: list[ModelInfo] = []
    for idx, head in enumerate(classes):
        body_start = head.end()
        body_end = classes[idx + 1].start() if idx + 1 < len(classes) else len(source)
        body = source[body_start:body_end]

        # is_transient/is_abstract from the class's (possibly local) framework base
        # — mirrors era2 so a TransientModel/AbstractModel recovered via the
        # fallback still gets the right model-type flag in Neo4j (#285 follow-up).
        resolved_bases = _resolve_era1_framework_bases(
            class_bases.get(head.group(1), set()), class_bases, MODEL_BASE_CLASSES,
        )
        is_abstract = 'AbstractModel' in resolved_bases
        is_transient = 'TransientModel' in resolved_bases

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

        # Fields from _columns dict — use balanced-paren entry extractor to
        # capture multi-line entries (fields.function / fields.related chains)
        # and record source_definition for richer pgvector embeddings.
        cols_block = _extract_columns_block(body)
        fields_list: list[FieldInfo] = []
        seen_field_names: set[str] = set()  # dedup guard (idempotent per class)
        if cols_block:
            for fm in _RE_ERA1_ENTRY_START.finditer(cols_block):
                field_name = fm.group(1)
                ttype = fm.group(2).lower()
                if ttype not in FIELD_TYPES_LEGACY:
                    continue
                if field_name in seen_field_names:
                    continue
                seen_field_names.add(field_name)
                src_def = _extract_era1_field_entry(cols_block, fm.start())
                # M10.5 P1 — comodel extraction (best-effort, relational only)
                era1_comodel: str | None = None
                if ttype in RELATIONAL_FIELD_TYPES:
                    cm = _RE_ERA1_COMODEL.search(src_def or "")
                    era1_comodel = cm.group(1) if cm else None
                fields_list.append(FieldInfo(
                    name=field_name, ttype=ttype,
                    related=None, compute=None,
                    stored=True, required=False,
                    source_definition=src_def or None,
                    comodel_name=era1_comodel,
                ))

        # Fields from _columns.update({...}) calls — may appear with or without
        # a prior `_columns = {...}` assignment (WI-4).
        for upd_match in _RE_COLUMNS_UPDATE.finditer(body):
            # upd_match.end() points to char right after the opening '{'
            upd_block = _extract_balanced_braces(body, upd_match.end())
            for fm in _RE_ERA1_ENTRY_START.finditer(upd_block):
                field_name = fm.group(1)
                ttype = fm.group(2).lower()
                if ttype not in FIELD_TYPES_LEGACY:
                    continue
                if field_name in seen_field_names:
                    continue
                seen_field_names.add(field_name)
                src_def = _extract_era1_field_entry(upd_block, fm.start())
                # M10.5 P1 — comodel extraction (best-effort, relational only)
                upd_comodel: str | None = None
                if ttype in RELATIONAL_FIELD_TYPES:
                    cm = _RE_ERA1_COMODEL.search(src_def or "")
                    upd_comodel = cm.group(1) if cm else None
                fields_list.append(FieldInfo(
                    name=field_name, ttype=ttype,
                    related=None, compute=None,
                    stored=True, required=False,
                    source_definition=src_def or None,
                    comodel_name=upd_comodel,
                ))

        # Detect `_columns = X._columns.copy()` pattern (WI-5).
        # Parent fields already represented via INHERITS; copying via copy() is
        # Python-level convenience. Do NOT extract fields — they're duplicates.
        for copy_match in _RE_COLUMNS_COPY.finditer(body):
            # copy_match.group(1) would be the parent class name (e.g. 'ParentCls')
            # We detect and skip — no field extraction from this line.
            pass

        # V9-G2 safety-net: extract new-API class-attr fields when AST failed.
        # v9 files may mix `_columns = {...}` (era1) WITH `amount = fields.Float(...)`
        # (era2 new-API) in the same class. The text-regex for _columns misses the
        # latter entirely. This additional scan catches them as a best-effort fallback.
        # Only fields in FIELD_TYPES (capitalized) are accepted — era1 dict entries
        # use lowercase types and are already handled above via FIELD_TYPES_LEGACY.
        for nm in _RE_ERA1_NEWAPI_FIELD.finditer(body):
            field_name = nm.group(1)
            field_type_raw = nm.group(2)  # e.g. 'Float', 'Many2one'
            # Skip private/dunder and known non-field class assignments
            if field_name.startswith('_'):
                continue
            if field_type_raw not in FIELD_TYPES:
                continue
            if field_name in seen_field_names:
                continue
            seen_field_names.add(field_name)
            ttype = field_type_raw.lower()
            fields_list.append(FieldInfo(
                name=field_name, ttype=ttype,
                related=None, compute=None,
                stored=True, required=False,
                source_definition=None,
                comodel_name=None,
            ))

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
            is_abstract=is_abstract,
            is_transient=is_transient,
            inherit=inherit,
            inherits={},
            fields=fields_list,
            methods=methods_list,
            had_explicit_name=had_explicit_name,
        ))
    return models
