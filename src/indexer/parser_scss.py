# SPDX-License-Identifier: AGPL-3.0-or-later
"""SCSS parser for Odoo codebases (WI-A1, ADR-0025).

Extracts SCSS-specific constructs:
  - $variable declarations (grouped into variable blocks)
  - @mixin definitions
  - @include directives
  - @extend directives
  - Nested rules (flattened to selector chunks)
  - @import / @use / @forward with SCSS module resolution to absolute path

**Always uses regex-based extraction** (not tree-sitter).

Rationale (corrects an earlier assumption documented in this docstring):
  The `tree-sitter-css` grammar parses *standard CSS only*.  It does not
  recognise SCSS-specific syntax — `@mixin`, `@include`, `@extend`, and
  bare `$variable` declarations are all silently absorbed into generic
  `rule_set` / `error` nodes.  Probing for `mixin_statement` /
  `include_statement` / `extend_statement` node types returns nothing,
  so a tree-sitter-backed SCSS parser yields `mixin_count = 0`,
  `variable_count = 0`, etc. — exactly the bug surfaced by the
  PR #120 CI run.

  The regex-based parser handles every SCSS construct Odoo themes use in
  practice (Odoo's `web/static/src/scss/` and theme modules), and runs
  in microseconds per file.  See ADR-0025 §D2 for the trade-off rationale.
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
# Regex-based SCSS parser (always-on — see module docstring)
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

    Returns (chunks, StylesheetInfo).  Always uses the regex-based parser:
    `tree-sitter-css` cannot recognise SCSS-specific syntax (@mixin, @include,
    @extend, $vars), so a tree-sitter path here would silently undercount
    every SCSS file.  See module docstring + ADR-0025 §D2.
    """
    try:
        raw = Path(filepath).read_bytes()
    except OSError:
        return [], StylesheetInfo(
            file_path=filepath, module=module_info.name,
            odoo_version=module_info.odoo_version, language="scss",
        )

    src_str = raw.decode("utf-8", errors="ignore")
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
