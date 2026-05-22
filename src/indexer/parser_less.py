# SPDX-License-Identifier: AGPL-3.0-or-later
"""LESS parser for Odoo codebases (RP WI-3).

Indexes `.less` stylesheet files used by Odoo v8–v11 (v8=156, v9=236, v11=312
files) that the current indexer silently drops (it handles only .css and .scss).
Produces :Stylesheet nodes with language='less', :IMPORTS edges, and pgvector
chunks — enabling MCP tools `resolve_stylesheet` / `find_style_override` for
legacy Odoo versions.

LESS syntax differences vs SCSS handled here:
  - Variables: @var: value;   (not $var:)
  - Import: @import "file";   or @import (reference) "file";  — LESS may omit
    extension or use .less; no underscore-partial convention.
  - Mixin definitions: .mixin-name() { } or #ns > .mixin() { }
  - Mixin calls: .mixin-name(); or .mixin-name(@arg);
  - Same nested rules + & selector as SCSS.

Always uses regex-based extraction (same rationale as parser_scss.py — tree-sitter-css
does not recognise LESS-specific syntax).
"""
import logging
import re
from pathlib import Path

from .models import ModuleInfo, SCSSChunk, StylesheetInfo

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WINDOW = 2048
_OVERLAP = 256
_MAX_LESS_BYTES = 200_000

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
    """Split *content* into sliding-window SCSSChunk entries."""
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


def _resolve_less_import(import_path: str, source_file: str) -> str | None:
    """Resolve a LESS @import path to an absolute file path.

    LESS import resolution rules (differ from SCSS):
    1. Try direct path relative to source file's directory.
    2. Try with .less suffix if no extension.
    3. No underscore-partial convention (unlike SCSS).

    Returns the resolved absolute path as string, or None if not found.
    """
    source_dir = Path(source_file).parent
    candidates = []

    p = Path(import_path)
    if not p.suffix:
        base_with_ext = p.with_suffix(".less")
    else:
        base_with_ext = p

    # Direct path
    candidates.append(source_dir / base_with_ext)
    # Also try the bare path in case extension was provided
    candidates.append(source_dir / p)

    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# LESS variables: @varname: value;  (but NOT @media, @import, @mixin, @keyframes etc.)
_RE_LESS_VAR = re.compile(
    r'^\s*@(?!import|media|charset|keyframes|font-face|mixin|include|extend|use|forward'
    r'|page|viewport)'
    r'[\w-]+\s*:',
    re.MULTILINE,
)

# Mixin definitions: .name() { ... } or #ns > .sub() { ... }
# We match lines that start with .[name]( or #[name] > .[name]( at start of block
_RE_MIXIN_DEF = re.compile(
    r'^(\s*[.#][\w-]+(?:\s*>\s*[.#][\w-]+)*\s*\([^)]*\))\s*\{',
    re.MULTILINE,
)

# Mixin calls: .name(); or .name(@arg);  — inside rule blocks
_RE_MIXIN_CALL = re.compile(
    r'^\s*[.#][\w-]+(?:\s*>\s*[.#][\w-]+)*\s*\([^)]*\)\s*;',
    re.MULTILINE,
)

# @import with optional LESS import options: @import (reference) "path";
_RE_IMPORT = re.compile(
    r'''@import\s+(?:\([^)]*\)\s*)?["']([^"']+)["']''',
    re.IGNORECASE,
)

# @media blocks
_RE_MEDIA = re.compile(r'(@media\s[^{]+)\{', re.IGNORECASE)

# Non-mixin rule selectors: lines ending with { that don't start with @ or whitespace-only
_RE_SELECTOR = re.compile(r'^([^@\s{}\n][^{}\n]*)\s*\{', re.MULTILINE)

# Guard: detect a mixin-def selector shape — [.#]ident(...) with the paren
# immediately following the leading ident (not a pseudo-class like :not(.x)).
# Matches: ".o-flex-center()" or "#ns > .sub(@a, @b)" — any selector whose
# FIRST token (after optional whitespace) is [.#]word immediately followed by (.
# Does NOT match "a:hover", "div:nth-child(2n)", ".btn:not(.x)" because those
# all start with an element/class token that does NOT have '(' right after the ident.
_RE_MIXIN_DEF_SHAPE = re.compile(r'^[.#][\w-]+\s*\(')


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


# ---------------------------------------------------------------------------
# Regex-based LESS parser
# ---------------------------------------------------------------------------

def _parse_less_regex(
    src: str, module: str, version: str, file_path: str
) -> tuple[list[SCSSChunk], StylesheetInfo]:
    """Regex-based LESS parser. Returns (chunks, info)."""
    stem = Path(file_path).stem
    chunks: list[SCSSChunk] = []

    selector_count = 0
    variable_count = 0
    import_count = 0
    mixin_count = 0
    imports: list[str] = []

    var_block: list[str] = []

    def _flush_var_block() -> None:
        nonlocal var_block
        if not var_block:
            return
        content = "\n".join(var_block)
        entity = f"{stem}:variables"
        chunks.extend(_emit(content, module, version, file_path, "variable", entity))
        var_block.clear()

    # Process line by line for @variable blocks
    lines = src.splitlines(keepends=True)
    for line in lines:
        if _RE_LESS_VAR.match(line):
            variable_count += 1
            var_block.append(line.rstrip())
        else:
            if var_block and line.strip():
                _flush_var_block()

    _flush_var_block()

    # @import directives
    for m in _RE_IMPORT.finditer(src):
        import_count += 1
        resolved = _resolve_less_import(m.group(1), file_path)
        imports.append(resolved or m.group(1))
        line_start = src.rfind("\n", 0, m.start()) + 1
        line_end = src.find("\n", m.end())
        raw = src[line_start:(line_end if line_end != -1 else len(src))].strip()
        chunks.extend(_emit(raw, module, version, file_path, "import", stem))

    # Mixin definitions: .mixin() { }
    for m in _RE_MIXIN_DEF.finditer(src):
        mixin_name = m.group(1).strip()
        mixin_count += 1
        block_start = m.end() - 1
        block_text, _ = _extract_block(src, block_start)
        raw = mixin_name + " " + block_text
        chunks.extend(_emit(raw, module, version, file_path, "mixin", mixin_name[:80]))

    # Mixin calls: .mixin(); — produce "include" chunks (mirrors SCSS @include)
    for m in _RE_MIXIN_CALL.finditer(src):
        raw = m.group(0)
        chunks.extend(_emit(raw, module, version, file_path, "include", stem))

    # @media blocks
    for m in _RE_MEDIA.finditer(src):
        header = m.group(1).strip()
        block_text, _ = _extract_block(src, m.end() - 1)
        raw = header + " " + block_text
        entity = header
        chunks.extend(_emit(raw, module, version, file_path, "media", entity))

    # Rule sets (non-mixin selectors)
    for m in _RE_SELECTOR.finditer(src):
        header = m.group(1).strip()
        # Skip mixin definitions — they are already handled by _RE_MIXIN_DEF above.
        # A mixin def starts with [.#]ident( immediately (e.g. ".o-flex-center()").
        # Real pseudo-class selectors like "a:hover" or ".btn:not(.x)" do NOT start
        # their first token with a bare '(' right after the ident, so they pass through.
        if _RE_MIXIN_DEF_SHAPE.match(header):
            continue
        selector_count += 1
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
        language="less",
        selector_count=selector_count,
        variable_count=variable_count,
        import_count=import_count,
        mixin_count=mixin_count,
        imports=imports,
    )
    return chunks, info


# ---------------------------------------------------------------------------
# Public API — mirrors parser_scss.py interface
# ---------------------------------------------------------------------------

def parse_file(
    filepath: str, module_info: ModuleInfo
) -> tuple[list[SCSSChunk], StylesheetInfo]:
    """Parse a single LESS file.

    Returns (chunks, StylesheetInfo) with language='less'.
    Uses regex-based parsing — tree-sitter-css does not handle LESS syntax.
    """
    try:
        raw = Path(filepath).read_bytes()
    except OSError:
        return [], StylesheetInfo(
            file_path=filepath, module=module_info.name,
            odoo_version=module_info.odoo_version, language="less",
        )

    src_str = raw.decode("utf-8", errors="ignore")
    return _parse_less_regex(src_str, module_info.name, module_info.odoo_version, filepath)


def parse_module(
    module_info: ModuleInfo,
) -> tuple[list[SCSSChunk], list[StylesheetInfo]]:
    """Parse all LESS files in a module's static/ directory.

    Returns (all_chunks, all_stylesheet_infos) with language='less'.
    LESS files typically live in static/src/less/ or static/less/ in v8-v11 modules.
    """
    module_path = Path(module_info.path)
    static = module_path / "static"
    if not static.exists():
        return [], []

    all_chunks: list[SCSSChunk] = []
    all_infos: list[StylesheetInfo] = []

    for less_file in sorted(static.rglob("*.less")):
        rel = less_file.relative_to(static)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if less_file.stat().st_size > _MAX_LESS_BYTES:
            _logger.debug("Skipping large LESS file: %s", less_file)
            continue

        chunks, info = parse_file(str(less_file), module_info)
        all_chunks.extend(chunks)
        all_infos.append(info)

    return all_chunks, all_infos
