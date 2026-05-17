"""SCSS parser for Odoo codebases (WI-A1, ADR-0025).

Extends parser_css with SCSS-specific constructs:
  - $variable declarations (grouped into variable blocks)
  - @mixin definitions
  - @include directives
  - @extend directives
  - Nested rules (flattened to selector chunks)
  - @import with SCSS module resolution to absolute path

Uses tree-sitter-css (which handles SCSS dialect) when available. Falls back
to a regex-based parser otherwise (see ADR-0025 §D2 trade-off note).

Note: tree-sitter-css parses SCSS via the same grammar (CSS superset mode).
The SCSS-specific nodes differ slightly: `mixin_statement`, `include_statement`,
`extend_statement`, `variable_declaration`. The tree-sitter parser used here is
the same _TS_PARSER from parser_css, since tree-sitter-css handles both dialects.
"""
import logging
import re
from pathlib import Path

from .models import ModuleInfo, SCSSChunk, StylesheetInfo
from .parser_css import _TS_PARSER

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WINDOW = 2048
_OVERLAP = 256
_MAX_SCSS_BYTES = 200_000

_SKIP_DIRS = frozenset({"lib", "tests"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit(
    content: str,
    module: str,
    version: str,
    file_path: str,
    chunk_kind: str,
    entity_name: str,
) -> list[SCSSChunk]:
    if len(content) <= _WINDOW:
        return [SCSSChunk(
            module=module,
            odoo_version=version,
            file_path=file_path,
            chunk_kind=chunk_kind,
            entity_name=entity_name,
            chunk_idx=0,
            content=content,
        )]
    chunks: list[SCSSChunk] = []
    start = 0
    idx = 0
    while start < len(content):
        end = min(start + _WINDOW, len(content))
        chunks.append(SCSSChunk(
            module=module,
            odoo_version=version,
            file_path=file_path,
            chunk_kind=chunk_kind,
            entity_name=entity_name,
            chunk_idx=idx,
            content=content[start:end],
        ))
        if end == len(content):
            break
        start = end - _OVERLAP
        idx += 1
    return chunks


def _resolve_scss_import(import_path: str, source_file: str) -> str | None:
    """Resolve a SCSS @import path to an absolute file path.

    SCSS import resolution rules:
    1. Try direct path relative to source file's directory.
    2. Try with _ prefix (SCSS partial convention: @import 'foo' → _foo.scss).
    3. Try with .scss suffix.
    Returns the resolved absolute path as string, or None if not found.
    """
    source_dir = Path(source_file).parent
    candidates = []

    p = Path(import_path)
    # Add .scss suffix if missing
    if not p.suffix:
        base_with_ext = p.with_suffix(".scss")
    else:
        base_with_ext = p

    # Direct path
    candidates.append(source_dir / base_with_ext)
    # Partial prefix (_foo.scss)
    partial_name = "_" + base_with_ext.name
    candidates.append(source_dir / base_with_ext.parent / partial_name)

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


# ---------------------------------------------------------------------------
# tree-sitter-based SCSS parser (preferred)
# ---------------------------------------------------------------------------

def _walk_ts(node):
    yield node
    for child in node.children:
        yield from _walk_ts(child)


def _ts_node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _parse_scss_treesitter(
    source: bytes, module: str, version: str, file_path: str
) -> tuple[list[SCSSChunk], StylesheetInfo]:
    """Parse SCSS with tree-sitter. Returns (chunks, info)."""
    tree = _TS_PARSER.parse(source)
    stem = Path(file_path).stem

    chunks: list[SCSSChunk] = []
    selector_count = 0
    variable_count = 0
    import_count = 0
    mixin_count = 0
    imports: list[str] = []

    var_block: list[str] = []

    def _flush_var_block():
        nonlocal var_block
        if not var_block:
            return
        content = "\n".join(var_block)
        entity = f"{stem}:variables"
        chunks.extend(_emit(content, module, version, file_path, "variable", entity))
        var_block.clear()

    for node in _walk_ts(tree.root_node):
        node_type = node.type

        # SCSS $variable declaration: `$foo: value;`
        if node_type == "declaration":
            raw = _ts_node_text(node, source)
            if raw.lstrip().startswith("$"):
                variable_count += 1
                var_block.append(raw)
                continue

        # @import "path" or @use "path" (SCSS module system)
        elif node_type in ("import_statement", "use_statement", "forward_statement"):
            import_count += 1
            _flush_var_block()
            raw = _ts_node_text(node, source)
            # Extract import path
            for child in node.children:
                if child.type in ("string_value", "call_expression"):
                    val = _ts_node_text(child, source).strip("\"'")
                    resolved = _resolve_scss_import(val, file_path)
                    imports.append(resolved or val)
                    break
                elif child.type == "string":
                    val = _ts_node_text(child, source).strip("\"'")
                    resolved = _resolve_scss_import(val, file_path)
                    imports.append(resolved or val)
                    break
            chunks.extend(_emit(raw, module, version, file_path, "import", stem))

        # @mixin definition
        elif node_type == "mixin_statement":
            mixin_count += 1
            _flush_var_block()
            raw = _ts_node_text(node, source)
            # Extract mixin name
            mixin_name = stem
            for child in node.children:
                if child.type in ("name", "identifier", "plain_value"):
                    mixin_name = _ts_node_text(child, source).strip()
                    break
            chunks.extend(_emit(raw, module, version, file_path, "mixin", mixin_name))

        # @include directive
        elif node_type == "include_statement":
            _flush_var_block()
            raw = _ts_node_text(node, source)
            chunks.extend(_emit(raw, module, version, file_path, "include", stem))

        # @extend directive
        elif node_type == "extend_statement":
            _flush_var_block()
            raw = _ts_node_text(node, source)
            chunks.extend(_emit(raw, module, version, file_path, "extend", stem))

        # @media block
        elif node_type == "media_statement":
            _flush_var_block()
            raw = _ts_node_text(node, source)
            condition = stem
            for child in node.children:
                if child.type == "media_query_list":
                    condition = _ts_node_text(child, source).strip()
                    break
            entity = f"@media {condition}"
            chunks.extend(_emit(raw, module, version, file_path, "media", entity))

        # Rule set (selector + block)
        elif node_type == "rule_set":
            selector_count += 1
            _flush_var_block()
            raw = _ts_node_text(node, source)
            selector_text = ""
            for child in node.children:
                if child.type == "selectors":
                    selector_text = _ts_node_text(child, source).strip()
                    break
            chunks.extend(_emit(raw, module, version, file_path, "selector", selector_text or stem))

    _flush_var_block()

    if not chunks:
        src_str = source.decode("utf-8", errors="ignore")
        chunks.extend(_emit(src_str, module, version, file_path, "raw", stem))

    info = StylesheetInfo(
        file_path=file_path,
        module=module,
        odoo_version=version,
        language="scss",
        selector_count=selector_count,
        variable_count=variable_count,
        import_count=import_count,
        mixin_count=mixin_count,
        imports=imports,
    )
    return chunks, info


# ---------------------------------------------------------------------------
# Regex-based SCSS fallback parser
# ---------------------------------------------------------------------------

_RE_SCSS_VAR = re.compile(r'^\s*\$[\w-]+\s*:', re.MULTILINE)
_RE_MIXIN_DEF = re.compile(r'@mixin\s+([\w-]+)\s*(?:\([^)]*\))?\s*\{', re.MULTILINE)
_RE_INCLUDE = re.compile(r'@include\s+[\w-]+(?:\([^)]*\))?\s*;', re.MULTILINE)
_RE_EXTEND = re.compile(r'@extend\s+[^;]+;', re.MULTILINE)
_RE_IMPORT = re.compile(r'''@(?:import|use|forward)\s+["']([^"']+)["']''', re.IGNORECASE)
_RE_MEDIA = re.compile(r'(@media\s[^{]+)\{', re.IGNORECASE)
_RE_SELECTOR = re.compile(r'^([^@$\s{}\n][^{}\n]*)\s*\{', re.MULTILINE)


def _extract_block(text: str, start: int) -> tuple[str, int]:
    """Extract balanced {...} starting at index `start` (must be '{')."""
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return text[start:], len(text)


def _parse_scss_regex(
    src: str, module: str, version: str, file_path: str
) -> tuple[list[SCSSChunk], StylesheetInfo]:
    """Regex-based SCSS parser. Returns (chunks, info)."""
    stem = Path(file_path).stem
    chunks: list[SCSSChunk] = []

    selector_count = 0
    variable_count = 0
    import_count = 0
    mixin_count = 0
    imports: list[str] = []

    var_block: list[str] = []

    def _flush_var_block():
        nonlocal var_block
        if not var_block:
            return
        content = "\n".join(var_block)
        entity = f"{stem}:variables"
        chunks.extend(_emit(content, module, version, file_path, "variable", entity))
        var_block.clear()

    # Process line by line for $variable blocks
    lines = src.splitlines(keepends=True)
    for line in lines:
        if _RE_SCSS_VAR.match(line):
            variable_count += 1
            var_block.append(line.rstrip())
        else:
            if var_block and line.strip():
                _flush_var_block()

    _flush_var_block()

    # @import/@use/@forward directives
    for m in _RE_IMPORT.finditer(src):
        import_count += 1
        resolved = _resolve_scss_import(m.group(1), file_path)
        imports.append(resolved or m.group(1))
        line_start = src.rfind("\n", 0, m.start()) + 1
        line_end = src.find("\n", m.end())
        raw = src[line_start:(line_end if line_end != -1 else len(src))].strip()
        chunks.extend(_emit(raw, module, version, file_path, "import", stem))

    # @mixin definitions
    for m in _RE_MIXIN_DEF.finditer(src):
        mixin_name = m.group(1)
        mixin_count += 1
        block_start = m.end() - 1
        block_text, _ = _extract_block(src, block_start)
        raw = src[m.start():m.start() + len(m.group(0)) - 1] + block_text
        chunks.extend(_emit(raw, module, version, file_path, "mixin", mixin_name))

    # @include directives
    for m in _RE_INCLUDE.finditer(src):
        raw = m.group(0)
        chunks.extend(_emit(raw, module, version, file_path, "include", stem))

    # @extend directives
    for m in _RE_EXTEND.finditer(src):
        raw = m.group(0)
        chunks.extend(_emit(raw, module, version, file_path, "extend", stem))

    # @media blocks
    for m in _RE_MEDIA.finditer(src):
        header = m.group(1).strip()
        block_text, _ = _extract_block(src, m.end() - 1)
        raw = header + " " + block_text
        entity = header
        chunks.extend(_emit(raw, module, version, file_path, "media", entity))

    # Rule sets (non-mixin selectors)
    for m in _RE_SELECTOR.finditer(src):
        selector_count += 1
        header = m.group(1).strip()
        block_text, _ = _extract_block(src, m.end() - 1)
        raw = header + " " + block_text
        entity = header[:80]
        chunks.extend(_emit(raw, module, version, file_path, "selector", entity))

    if not chunks:
        chunks.extend(_emit(src, module, version, file_path, "raw", stem))

    info = StylesheetInfo(
        file_path=file_path,
        module=module,
        odoo_version=version,
        language="scss",
        selector_count=selector_count,
        variable_count=variable_count,
        import_count=import_count,
        mixin_count=mixin_count,
        imports=imports,
    )
    return chunks, info


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_file(
    filepath: str, module_info: ModuleInfo
) -> tuple[list[SCSSChunk], StylesheetInfo]:
    """Parse a single SCSS file.

    Returns (chunks, StylesheetInfo). Uses tree-sitter when available,
    falls back to regex parser otherwise (ADR-0025 §D2).
    """
    try:
        raw = Path(filepath).read_bytes()
    except OSError:
        return [], StylesheetInfo(
            file_path=filepath, module=module_info.name,
            odoo_version=module_info.odoo_version, language="scss",
        )

    src_str = raw.decode("utf-8", errors="ignore")

    if _TS_PARSER is not None:
        try:
            return _parse_scss_treesitter(raw, module_info.name, module_info.odoo_version, filepath)
        except Exception as exc:
            _logger.warning(
                "tree-sitter SCSS parse failed for %s: %s — using regex fallback",
                filepath, exc,
            )

    return _parse_scss_regex(src_str, module_info.name, module_info.odoo_version, filepath)


def parse_module(
    module_info: ModuleInfo,
) -> tuple[list[SCSSChunk], list[StylesheetInfo]]:
    """Parse all SCSS files in a module's static/ directory.

    Returns (all_chunks, all_stylesheet_infos).
    SCSS files typically live in static/src/scss/ or static/scss/.
    """
    module_path = Path(module_info.path)
    static = module_path / "static"
    if not static.exists():
        return [], []

    all_chunks: list[SCSSChunk] = []
    all_infos: list[StylesheetInfo] = []

    for scss_file in sorted(static.rglob("*.scss")):
        rel = scss_file.relative_to(static)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if scss_file.stat().st_size > _MAX_SCSS_BYTES:
            _logger.debug("Skipping large SCSS file: %s", scss_file)
            continue

        chunks, info = parse_file(str(scss_file), module_info)
        all_chunks.extend(chunks)
        all_infos.append(info)

    return all_chunks, all_infos
