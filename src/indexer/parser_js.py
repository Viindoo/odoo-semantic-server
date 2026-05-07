"""Era-aware JavaScript parser for Odoo codebases.

Era 1: Widget.extend({...})  — Odoo 8–12, no module system
Era 2: odoo.define(...)       — Odoo 12–15
Era 3: @odoo-module / OWL    — Odoo 16+

Each era produces JSChunk objects. Large files are sliding-windowed into
~2048-char chunks with 256-char overlap when no named entity is found.
"""
from __future__ import annotations

from pathlib import Path

import tree_sitter_javascript as _tsjs
from tree_sitter import Language, Node, Parser

from .models import JSChunk, ModuleInfo

_LANG = Language(_tsjs.language())
_PARSER = Parser(_LANG)

_WINDOW = 2048
_OVERLAP = 256


def _detect_era(source: str) -> str:
    if "@odoo-module" in source:
        return "era3"
    if "import {" in source or "import{" in source:
        return "era3"
    if "odoo.define(" in source:
        return "era2"
    return "era1"


def _sliding_chunks(
    content: str,
    module: str,
    version: str,
    file_path: str,
    era: str,
    entity_name: str,
) -> list[JSChunk]:
    """Split a large content string into overlapping window chunks."""
    chunks = []
    start = 0
    idx = 0
    while start < len(content):
        end = min(start + _WINDOW, len(content))
        chunks.append(JSChunk(
            module=module,
            odoo_version=version,
            file_path=file_path,
            era=era,
            entity_name=entity_name,
            chunk_idx=idx,
            content=content[start:end],
        ))
        if end == len(content):
            break
        start = end - _OVERLAP
        idx += 1
    return chunks


def _extract_string_from_node(node: Node, source: bytes) -> str | None:
    """Extract string value from a string literal node."""
    if node.type in ("string", "template_string"):
        text = source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")
        return text.strip("\"'`")
    return None


def _find_children_by_type(node: Node, type_: str) -> list[Node]:
    return [c for c in node.children if c.type == type_]


def _find_first_child_by_type(node: Node, type_: str) -> Node | None:
    for c in node.children:
        if c.type == type_:
            return c
    return None


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


# --- Era 1: Widget.extend ---

def _parse_era1(source: bytes, module: str, version: str, file_path: str) -> list[JSChunk]:
    tree = _PARSER.parse(source)
    chunks: list[JSChunk] = []
    src_str = source.decode("utf-8", errors="ignore")

    for node in _walk(tree.root_node):
        # Pattern: identifier.extend({ ... })  or  SomeName = identifier.extend({ ... })
        if node.type != "call_expression":
            continue
        func = _find_first_child_by_type(node, "member_expression")
        if not func:
            continue
        prop = _find_first_child_by_type(func, "property_identifier")
        if not prop or source[prop.start_byte:prop.end_byte] != b"extend":
            continue

        # Try to find the assigned variable name
        entity_name = "widget"
        parent = node.parent
        if parent and parent.type == "assignment_expression":
            left = _find_first_child_by_type(parent, "member_expression") or \
                   _find_first_child_by_type(parent, "identifier")
            if left:
                name_bytes = source[left.start_byte:left.end_byte]
                entity_name = name_bytes.decode("utf-8", errors="ignore").split(".")[-1]
        elif parent and parent.type == "variable_declarator":
            id_node = _find_first_child_by_type(parent, "identifier")
            if id_node:
                entity_name = source[
                    id_node.start_byte:id_node.end_byte
                ].decode("utf-8", errors="ignore")

        content = src_str[node.start_byte:node.end_byte]
        if len(content) <= _WINDOW:
            chunks.append(JSChunk(
                module=module, odoo_version=version, file_path=file_path,
                era="era1", entity_name=entity_name, chunk_idx=0, content=content,
            ))
        else:
            chunks.extend(_sliding_chunks(content, module, version, file_path, "era1", entity_name))

    if not chunks:
        chunks.extend(_sliding_chunks(src_str, module, version, file_path, "era1",
                                       Path(file_path).stem))
    return chunks


# --- Era 2: odoo.define ---

def _parse_era2(source: bytes, module: str, version: str, file_path: str) -> list[JSChunk]:
    tree = _PARSER.parse(source)
    chunks: list[JSChunk] = []
    src_str = source.decode("utf-8", errors="ignore")

    for node in _walk(tree.root_node):
        if node.type != "call_expression":
            continue
        func = _find_first_child_by_type(node, "member_expression")
        if not func:
            continue
        obj = _find_first_child_by_type(func, "identifier")
        prop = _find_first_child_by_type(func, "property_identifier")
        if not obj or not prop:
            continue
        obj_name = source[obj.start_byte:obj.end_byte]
        prop_name = source[prop.start_byte:prop.end_byte]
        if obj_name != b"odoo" or prop_name != b"define":
            continue

        args = _find_first_child_by_type(node, "arguments")
        entity_name = Path(file_path).stem
        if args:
            string_nodes = [c for c in args.children if c.type in ("string", "template_string")]
            if string_nodes:
                raw = source[string_nodes[0].start_byte:string_nodes[0].end_byte]
                entity_name = raw.decode("utf-8", errors="ignore").strip("\"'`").split(".")[-1]

        content = src_str[node.start_byte:node.end_byte]
        if len(content) <= _WINDOW:
            chunks.append(JSChunk(
                module=module, odoo_version=version, file_path=file_path,
                era="era2", entity_name=entity_name, chunk_idx=0, content=content,
            ))
        else:
            chunks.extend(_sliding_chunks(content, module, version, file_path, "era2", entity_name))

    if not chunks:
        chunks.extend(_sliding_chunks(src_str, module, version, file_path, "era2",
                                       Path(file_path).stem))
    return chunks


# --- Era 3: @odoo-module / OWL ---

def _parse_era3(source: bytes, module: str, version: str, file_path: str) -> list[JSChunk]:
    tree = _PARSER.parse(source)
    chunks: list[JSChunk] = []
    src_str = source.decode("utf-8", errors="ignore")

    for node in _walk(tree.root_node):
        # Class declarations (possibly exported)
        if node.type == "class_declaration":
            name_node = _find_first_child_by_type(node, "identifier")
            entity_name = (
                source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore")
                if name_node else Path(file_path).stem
            )
            content = src_str[node.start_byte:node.end_byte]
            if len(content) <= _WINDOW:
                chunks.append(JSChunk(
                    module=module, odoo_version=version, file_path=file_path,
                    era="era3", entity_name=entity_name, chunk_idx=0, content=content,
                ))
            else:
                chunks.extend(
                    _sliding_chunks(content, module, version, file_path, "era3", entity_name)
                )

        # patch() calls: patch("MyComponent", { ... })
        elif node.type == "call_expression":
            func = _find_first_child_by_type(node, "identifier")
            if func and source[func.start_byte:func.end_byte] == b"patch":
                args = _find_first_child_by_type(node, "arguments")
                entity_name = Path(file_path).stem
                if args:
                    string_nodes = [c for c in args.children
                                    if c.type in ("string", "template_string")]
                    if string_nodes:
                        raw = source[string_nodes[0].start_byte:string_nodes[0].end_byte]
                        entity_name = raw.decode("utf-8", errors="ignore").strip("\"'`")
                content = src_str[node.start_byte:node.end_byte]
                if len(content) <= _WINDOW:
                    chunks.append(JSChunk(
                        module=module, odoo_version=version, file_path=file_path,
                        era="era3", entity_name=entity_name, chunk_idx=0, content=content,
                    ))
                else:
                    chunks.extend(
                        _sliding_chunks(content, module, version, file_path, "era3", entity_name)
                    )

    if not chunks:
        chunks.extend(_sliding_chunks(src_str, module, version, file_path, "era3",
                                       Path(file_path).stem))
    return chunks


# --- Public API ---

def parse_file(filepath: str, module_info: ModuleInfo) -> list[JSChunk]:
    """Parse a JS file, return JSChunks grouped by era and entity."""
    try:
        source = Path(filepath).read_bytes()
    except OSError:
        return []

    src_str = source.decode("utf-8", errors="ignore")
    era = _detect_era(src_str)

    m = module_info.name
    v = module_info.odoo_version
    fp = filepath

    if era == "era1":
        return _parse_era1(source, m, v, fp)
    if era == "era2":
        return _parse_era2(source, m, v, fp)
    return _parse_era3(source, m, v, fp)


def parse_module(module_info: ModuleInfo) -> list[JSChunk]:
    """Parse all JS files in a module's static/src/ directory."""
    module_path = Path(module_info.path)
    static_src = module_path / "static" / "src"
    if not static_src.exists():
        return []

    chunks: list[JSChunk] = []
    for js_file in sorted(static_src.rglob("*.js")):
        chunks.extend(parse_file(str(js_file), module_info))
    return chunks
