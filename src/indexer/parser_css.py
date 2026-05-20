# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSS parser for Odoo codebases (WI-A1, ADR-0025).

Extracts semantic chunks from .css files:
  - CSS custom property blocks (--* declarations grouped by proximity)
  - Selector groups (rule-sets: selector { ... })
  - @media query blocks
  - @import directives

Uses tree-sitter-css when available (preferred). Falls back to a lightweight
regex-based parser when the tree-sitter-css package is not installed.

Trade-off documented in ADR-0025 §D2:
  tree-sitter-css: accurate parse tree, handles nested media queries, ignores
  string content of comments. Requires ~1 MB native extension.
  Regex fallback: handles 95%+ of real Odoo CSS (flat rules, no complex nesting).
  Both produce identical CSSChunk objects; callers cannot distinguish them.
"""
import logging
import re
import threading
from pathlib import Path

from .models import CSSChunk, ModuleInfo, StylesheetInfo

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sliding-window constants (same as parser_js.py convention)
# ---------------------------------------------------------------------------

_WINDOW = 2048
_OVERLAP = 256
_MAX_CSS_BYTES = 200_000   # 200 KB — skip minified/generated stylesheets

# ---------------------------------------------------------------------------
# Skip directories (same as parser_js.py convention)
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({"lib", "tests"})


# ---------------------------------------------------------------------------
# tree-sitter backend (optional, thread-local)
# ---------------------------------------------------------------------------
#
# tree-sitter Parser objects are NOT thread-safe — concurrent parse() calls on
# the same instance can corrupt internal state.  ADR-0006 enables cross-profile
# parallel indexing (--profile-workers N), so the indexer can call parse_file()
# from N threads simultaneously.  Mirror the embedder.py thread-safety pattern
# (ADR-0010 §D1) by holding one Parser per OS thread via `threading.local`.
#
# `_TS_LANGUAGE` is the immutable Language object (safe to share); only the
# mutable Parser is thread-local.

def _load_ts_language():
    """Return the tree-sitter CSS Language, or None when package not installed."""
    try:
        import tree_sitter_css as _tscss
        from tree_sitter import Language
        return Language(_tscss.language())
    except ImportError:
        return None


_TS_LANGUAGE = _load_ts_language()
_TS_AVAILABLE = _TS_LANGUAGE is not None
_TS_LOCAL = threading.local()


def _get_ts_parser():
    """Return a thread-local tree-sitter Parser, or None when unavailable.

    Each calling thread gets its own Parser instance lazily — instantiation is
    cheap (~microseconds) and parser state never crosses threads.
    """
    if not _TS_AVAILABLE:
        return None
    parser = getattr(_TS_LOCAL, "parser", None)
    if parser is None:
        from tree_sitter import Parser
        parser = Parser(_TS_LANGUAGE)
        _TS_LOCAL.parser = parser
    return parser


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _sliding_chunks(
    content: str,
    module: str,
    version: str,
    file_path: str,
    chunk_kind: str,
    entity_name: str,
) -> list[CSSChunk]:
    """Split large content into overlapping CSSChunk windows."""
    chunks: list[CSSChunk] = []
    start = 0
    idx = 0
    while start < len(content):
        end = min(start + _WINDOW, len(content))
        chunks.append(CSSChunk(
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


def _emit(
    content: str,
    module: str,
    version: str,
    file_path: str,
    chunk_kind: str,
    entity_name: str,
) -> list[CSSChunk]:
    """Emit one-or-more CSSChunks for a single semantic unit."""
    if len(content) <= _WINDOW:
        return [CSSChunk(
            module=module,
            odoo_version=version,
            file_path=file_path,
            chunk_kind=chunk_kind,
            entity_name=entity_name,
            chunk_idx=0,
            content=content,
        )]
    return _sliding_chunks(content, module, version, file_path, chunk_kind, entity_name)


# ---------------------------------------------------------------------------
# tree-sitter-based parser (preferred)
# ---------------------------------------------------------------------------

def _walk_ts(node):
    yield node
    for child in node.children:
        yield from _walk_ts(child)


def _ts_node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _parse_css_treesitter(
    source: bytes, module: str, version: str, file_path: str
) -> tuple[list[CSSChunk], StylesheetInfo]:
    """Parse CSS with tree-sitter. Returns (chunks, info)."""
    tree = _get_ts_parser().parse(source)
    src_str = source.decode("utf-8", errors="ignore")
    stem = Path(file_path).stem

    chunks: list[CSSChunk] = []
    selector_count = 0
    variable_count = 0
    import_count = 0
    imports: list[str] = []

    # Group consecutive custom-property declarations into variable blocks
    var_block: list[str] = []
    var_block_start = 0

    def _flush_var_block():
        nonlocal var_block, var_block_start
        if not var_block:
            return
        content = "\n".join(var_block)
        entity = f"{stem}:variables"
        chunks.extend(_emit(content, module, version, file_path, "variable", entity))
        var_block = []

    for node in _walk_ts(tree.root_node):
        node_type = node.type

        # @import "path"
        if node_type == "import_statement":
            import_count += 1
            _flush_var_block()
            raw = _ts_node_text(node, source)
            # Extract path string
            for child in node.children:
                if child.type in ("string_value", "call_expression"):
                    val = _ts_node_text(child, source).strip("\"'")
                    imports.append(val)
                    break
            chunks.extend(_emit(raw, module, version, file_path, "import", stem))

        # @media block
        elif node_type == "media_statement":
            _flush_var_block()
            raw = _ts_node_text(node, source)
            # Extract the media condition as entity name
            condition = stem
            for child in node.children:
                if child.type == "media_query_list":
                    condition = _ts_node_text(child, source).strip()
                    break
            entity = f"@media {condition}"
            chunks.extend(_emit(raw, module, version, file_path, "media", entity))

        # Rule set: selector { declarations }
        elif node_type == "rule_set":
            selector_count += 1
            _flush_var_block()
            # Check if block contains only CSS custom properties
            block_node = None
            selector_text = ""
            for child in node.children:
                if child.type == "selectors":
                    selector_text = _ts_node_text(child, source).strip()
                elif child.type == "block":
                    block_node = child

            if block_node:
                # Count custom property declarations in this block
                decls = [c for c in _walk_ts(block_node) if c.type == "declaration"]
                var_decls = [
                    d for d in decls
                    if _ts_node_text(d, source).lstrip().startswith("--")
                ]
                variable_count += len(var_decls)
                if var_decls and len(var_decls) == len(decls):
                    # Entire block is variable declarations — add to var block
                    raw_block = _ts_node_text(node, source)
                    var_block.append(raw_block)
                    continue

            raw = _ts_node_text(node, source)
            chunks.extend(_emit(raw, module, version, file_path, "selector", selector_text or stem))

    _flush_var_block()

    if not chunks:
        chunks.extend(_emit(src_str, module, version, file_path, "raw", stem))

    info = StylesheetInfo(
        file_path=file_path,
        module=module,
        odoo_version=version,
        language="css",
        selector_count=selector_count,
        variable_count=variable_count,
        import_count=import_count,
        imports=imports,
    )
    return chunks, info


# ---------------------------------------------------------------------------
# Regex-based fallback parser
# ---------------------------------------------------------------------------

# Match @import "path" or @import url("path")
_RE_IMPORT = re.compile(
    r'''@import\s+(?:url\s*\(\s*)?["']([^"']+)["']''',
    re.IGNORECASE,
)

# Match @media condition { ... } — non-greedy block extraction
_RE_MEDIA = re.compile(
    r'(@media\s[^{]+)\{',
    re.IGNORECASE,
)

# Match simple selectors (not @-rules)
_RE_SELECTOR = re.compile(
    r'^([^@{}\n][^{}\n]*)\s*\{',
    re.MULTILINE,
)

# Match CSS custom properties
_RE_VAR_DECL = re.compile(r'--[\w-]+\s*:', re.MULTILINE)


def _extract_block(text: str, start: int) -> tuple[str, int]:
    """Extract the balanced {...} block starting at index `start`.

    Returns (block_content_with_braces, end_index_exclusive).
    `start` must point to the opening '{'.
    """
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return text[start:], len(text)


def _parse_css_regex(
    src: str, module: str, version: str, file_path: str
) -> tuple[list[CSSChunk], StylesheetInfo]:
    """Regex-based CSS parser fallback. Returns (chunks, info)."""
    stem = Path(file_path).stem
    chunks: list[CSSChunk] = []
    selector_count = 0
    variable_count = 0
    import_count = 0
    imports: list[str] = []

    # --- @import directives ---
    for m in _RE_IMPORT.finditer(src):
        import_count += 1
        imports.append(m.group(1))
        line_start = src.rfind("\n", 0, m.start()) + 1
        line_end = src.find("\n", m.end())
        if line_end == -1:
            line_end = len(src)
        raw = src[line_start:line_end].strip()
        chunks.extend(_emit(raw, module, version, file_path, "import", stem))

    # --- Walk blocks ---
    # Build a list of top-level at-rule and rule positions.
    # Simplified: scan line by line for block openers.
    var_block: list[str] = []

    def _flush_var_block():
        nonlocal var_block
        if not var_block:
            return
        content = "\n".join(var_block)
        entity = f"{stem}:variables"
        chunks.extend(_emit(content, module, version, file_path, "variable", entity))
        var_block.clear()

    # Find all top-level rule starts
    for m in re.finditer(r'(@media\b[^{]*|@[a-z-]+[^{]*|[^@{}\n][^{}]*)\{', src):
        header = m.group(1).strip()
        block_start = m.end() - 1  # position of '{'

        block_text, end_pos = _extract_block(src, block_start)

        if header.startswith("@import"):
            # Already handled above
            continue
        elif header.lower().startswith("@media"):
            _flush_var_block()
            raw = header + " " + block_text
            entity = header.strip()
            chunks.extend(_emit(raw, module, version, file_path, "media", entity))
        elif header.startswith("@"):
            # Other @-rules: @keyframes, @charset, etc. — emit as raw
            _flush_var_block()
            raw = header + " " + block_text
            chunks.extend(_emit(raw, module, version, file_path, "raw", stem))
        else:
            # Regular rule-set
            selector_count += 1
            raw = header + " " + block_text
            var_lines = _RE_VAR_DECL.findall(raw)
            variable_count += len(var_lines)
            if var_lines and len(var_lines) == raw.count(":"):
                # Entire block is variable declarations
                var_block.append(raw)
            else:
                _flush_var_block()
                entity = header.strip().replace("\n", " ")[:80]
                chunks.extend(_emit(raw, module, version, file_path, "selector", entity))

    _flush_var_block()

    if not chunks:
        chunks.extend(_emit(src, module, version, file_path, "raw", stem))

    info = StylesheetInfo(
        file_path=file_path,
        module=module,
        odoo_version=version,
        language="css",
        selector_count=selector_count,
        variable_count=variable_count,
        import_count=import_count,
        imports=imports,
    )
    return chunks, info


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_file(
    filepath: str, module_info: ModuleInfo
) -> tuple[list[CSSChunk], StylesheetInfo]:
    """Parse a single CSS file.

    Returns (chunks, StylesheetInfo). Uses tree-sitter when available,
    falls back to regex parser otherwise (see ADR-0025 §D2).
    """
    try:
        raw = Path(filepath).read_bytes()
    except OSError:
        return [], StylesheetInfo(
            file_path=filepath, module=module_info.name,
            odoo_version=module_info.odoo_version, language="css",
        )

    src_str = raw.decode("utf-8", errors="ignore")

    if _TS_AVAILABLE:
        try:
            return _parse_css_treesitter(raw, module_info.name, module_info.odoo_version, filepath)
        except Exception as exc:
            _logger.warning(
                "tree-sitter CSS parse failed for %s: %s — using regex fallback",
                filepath, exc,
            )

    return _parse_css_regex(src_str, module_info.name, module_info.odoo_version, filepath)


def parse_module(
    module_info: ModuleInfo,
) -> tuple[list[CSSChunk], list[StylesheetInfo]]:
    """Parse all CSS files in a module's static/ directory.

    Returns (all_chunks, all_stylesheet_infos).
    Odoo CSS lives in: static/src/, static/lib/ (skipped), static/scss/ (separate).
    """
    module_path = Path(module_info.path)
    static = module_path / "static"
    if not static.exists():
        return [], []

    all_chunks: list[CSSChunk] = []
    all_infos: list[StylesheetInfo] = []

    for css_file in sorted(static.rglob("*.css")):
        # Skip skip-dirs and minified files
        rel = css_file.relative_to(static)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if css_file.stat().st_size > _MAX_CSS_BYTES:
            _logger.debug("Skipping large CSS file: %s", css_file)
            continue

        chunks, info = parse_file(str(css_file), module_info)
        all_chunks.extend(chunks)
        all_infos.append(info)

    return all_chunks, all_infos
