# SPDX-License-Identifier: AGPL-3.0-or-later
"""Era-aware JavaScript parser for Odoo codebases.

Era 1: Widget.extend({...})  — Odoo 8–12, no module system
Era 2: odoo.define(...)       — Odoo 12–15
Era 3: @odoo-module / OWL    — Odoo 16+

Each era produces JSChunk objects. Large files are sliding-windowed into
~2048-char chunks with 256-char overlap when no named entity is found.
"""
from __future__ import annotations

import threading
from pathlib import Path

import tree_sitter_javascript as _tsjs
from tree_sitter import Language, Node, Parser

from .models import JSChunk, JSGraphResult, JSPatchInfo, ModuleInfo, OWLCompInfo

# tree-sitter Parser objects are NOT thread-safe — concurrent parse() calls on
# the same instance can corrupt internal state.  ADR-0006 enables cross-profile
# parallel indexing (--profile-workers N), so the indexer can call parse_module*
# from N threads simultaneously.  Hold one Parser per OS thread via
# `threading.local`, mirroring the embedder.py pattern (ADR-0010 §D1).
#
# `_LANG` is the immutable Language object (safe to share); only the mutable
# Parser is thread-local.

_LANG = Language(_tsjs.language())
_LOCAL = threading.local()


def _get_parser() -> Parser:
    """Return a thread-local tree-sitter JS Parser."""
    parser = getattr(_LOCAL, "parser", None)
    if parser is None:
        parser = Parser(_LANG)
        _LOCAL.parser = parser
    return parser

_WINDOW = 2048
_OVERLAP = 256

_SKIP_DIRS = frozenset({"lib", "tests"})  # Odoo convention: third-party libs + test dirs
_MAX_JS_BYTES = 200_000  # 200 KB — minified third-party files are usually > 100 KB


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
    tree = _get_parser().parse(source)
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
    tree = _get_parser().parse(source)
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
    tree = _get_parser().parse(source)
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
        if any(part in _SKIP_DIRS for part in js_file.relative_to(static_src).parts):
            continue
        if js_file.stat().st_size > _MAX_JS_BYTES:
            continue
        chunks.extend(parse_file(str(js_file), module_info))
    return chunks


# --- Graph extraction (M4: produces JSPatchInfo + OWLCompInfo) ---

def _extract_era1_patches(
    tree, source: bytes, module_info: ModuleInfo, filepath: str, result: JSGraphResult
) -> None:
    """era1: var Foo = SomeWidget.extend({}) → JSPatchInfo(era='extend')."""
    for node in _walk(tree.root_node):
        if node.type != "call_expression":
            continue
        func = _find_first_child_by_type(node, "member_expression")
        if not func:
            continue
        prop = _find_first_child_by_type(func, "property_identifier")
        if not prop or source[prop.start_byte:prop.end_byte] != b"extend":
            continue
        # target = the object being extended (left side of the dot)
        obj = _find_first_child_by_type(func, "identifier")
        if not obj:
            continue
        target = source[obj.start_byte:obj.end_byte].decode("utf-8", errors="ignore")

        # patch_name = variable the result is assigned to
        patch_name = Path(filepath).stem
        parent = node.parent
        if parent and parent.type == "variable_declarator":
            id_node = _find_first_child_by_type(parent, "identifier")
            if id_node:
                patch_name = source[id_node.start_byte:id_node.end_byte].decode(
                    "utf-8", errors="ignore"
                )
        elif parent and parent.type == "assignment_expression":
            left = _find_first_child_by_type(parent, "identifier")
            if left:
                patch_name = source[left.start_byte:left.end_byte].decode(
                    "utf-8", errors="ignore"
                ).split(".")[-1]

        result.patches.append(JSPatchInfo(
            target=target,
            patch_name=patch_name,
            module=module_info.name,
            odoo_version=module_info.odoo_version,
            era="extend",
            file_path=filepath,
        ))


def _extract_era2_patches(
    tree, source: bytes, module_info: ModuleInfo, filepath: str, result: JSGraphResult
) -> None:
    """era2: Foo.include({}) → JSPatchInfo(era='include').
    odoo.define() without .include → no patch produced.
    """
    for node in _walk(tree.root_node):
        if node.type != "call_expression":
            continue
        func = _find_first_child_by_type(node, "member_expression")
        if not func:
            continue
        prop = _find_first_child_by_type(func, "property_identifier")
        if not prop or source[prop.start_byte:prop.end_byte] != b"include":
            continue
        # target = the object calling .include
        obj = _find_first_child_by_type(func, "identifier")
        if not obj:
            continue
        target = source[obj.start_byte:obj.end_byte].decode("utf-8", errors="ignore")
        result.patches.append(JSPatchInfo(
            target=target,
            patch_name=Path(filepath).stem,
            module=module_info.name,
            odoo_version=module_info.odoo_version,
            era="include",
            file_path=filepath,
        ))


def _extract_era3_patches(
    tree, source: bytes, module_info: ModuleInfo, filepath: str, result: JSGraphResult
) -> None:
    """era3: patch(MyComp.prototype, "name", {}) → JSPatchInfo(era='patch')."""
    for node in _walk(tree.root_node):
        if node.type != "call_expression":
            continue
        func_node = _find_first_child_by_type(node, "identifier")
        if not func_node or source[func_node.start_byte:func_node.end_byte] != b"patch":
            continue

        args = _find_first_child_by_type(node, "arguments")
        if not args:
            continue

        # Collect non-punctuation argument nodes
        arg_nodes = [c for c in args.children if c.type not in (",", "(", ")")]
        if not arg_nodes:
            continue

        # First arg: MyComp.prototype or MyComp — extract the base identifier
        first_arg = arg_nodes[0]
        if first_arg.type == "member_expression":
            # MyComp.prototype → take the object identifier
            obj = _find_first_child_by_type(first_arg, "identifier")
            target = (
                source[obj.start_byte:obj.end_byte].decode("utf-8", errors="ignore")
                if obj else Path(filepath).stem
            )
        elif first_arg.type == "identifier":
            target = source[first_arg.start_byte:first_arg.end_byte].decode(
                "utf-8", errors="ignore"
            )
        else:
            target = Path(filepath).stem

        # Optional second arg: string literal is the patch name
        patch_name = Path(filepath).stem
        if len(arg_nodes) >= 2 and arg_nodes[1].type in ("string", "template_string"):
            raw = source[arg_nodes[1].start_byte:arg_nodes[1].end_byte]
            patch_name = raw.decode("utf-8", errors="ignore").strip("\"'`")

        result.patches.append(JSPatchInfo(
            target=target,
            patch_name=patch_name,
            module=module_info.name,
            odoo_version=module_info.odoo_version,
            era="patch",
            file_path=filepath,
        ))


_ORM_READ_METHODS = frozenset({"read", "readGroup", "searchRead", "search_read"})


def _detect_bound_model_from_class_body(body: Node, source: bytes) -> str | None:
    """Heuristic: scan class body methods for ORM calls with a string literal model name.

    Detects two patterns:
    1. this.orm.read/readGroup/searchRead("<model>", ...) — first positional arg is string
    2. kwargs with resModel: "<model>" or model: "<model>" anywhere in class body

    Returns the first static string found, or None if only dynamic expressions detected.
    """
    for node in _walk(body):
        # Pattern 1: this.orm.read("sale.order", ...) or this.orm.readGroup(...)
        if node.type == "call_expression":
            func = _find_first_child_by_type(node, "member_expression")
            if func:
                # Check for `orm.read`, `orm.readGroup`, etc.
                prop = _find_first_child_by_type(func, "property_identifier")
                if prop:
                    method_name = source[prop.start_byte:prop.end_byte].decode(
                        "utf-8", errors="ignore"
                    )
                    if method_name in _ORM_READ_METHODS:
                        args = _find_first_child_by_type(node, "arguments")
                        if args:
                            # First non-punctuation argument
                            arg_nodes = [
                                c for c in args.children
                                if c.type not in (",", "(", ")")
                            ]
                            if arg_nodes:
                                first_arg = arg_nodes[0]
                                val = _extract_string_from_node(first_arg, source)
                                if val:
                                    return val

        # Pattern 2: resModel: "sale.order" or model: "sale.order" in object/pair
        if node.type == "pair":
            key_node = node.children[0] if node.children else None
            if key_node and key_node.type in ("string", "property_identifier"):
                key = source[key_node.start_byte:key_node.end_byte].decode(
                    "utf-8", errors="ignore"
                ).strip("\"'`")
                if key in ("resModel", "model"):
                    # Value node — skip punctuation
                    val_nodes = [
                        c for c in node.children
                        if c.type not in (":", ",")
                    ][1:]  # skip key itself
                    for val_node in val_nodes:
                        val = _extract_string_from_node(val_node, source)
                        if val:
                            return val

    return None


def _extract_era3_components(
    tree, source: bytes, module_info: ModuleInfo, filepath: str, result: JSGraphResult
) -> None:
    """era3: class Foo extends Bar { static template = "x.y" } → OWLCompInfo.

    bound_model is detected heuristically from ORM call patterns in the class body:
    - this.orm.read/readGroup/searchRead("<model>", ...) → bound_model = "<model>"
    - kwargs { resModel: "<model>" } or { model: "<model>" } → bound_model = "<model>"
    Dynamic expressions (this.props.model, variables) are not resolved → bound_model = None.
    Full static analysis via F4 USES_FIELD edge is deferred to M5.
    """
    major_version = int(module_info.odoo_version.split(".")[0])
    if major_version < 14:
        return  # OWL framework only exists in v14+

    for node in _walk(tree.root_node):
        if node.type not in ("class_declaration", "class"):
            continue
        name_node = _find_first_child_by_type(node, "identifier")
        if not name_node:
            continue
        class_name = source[name_node.start_byte:name_node.end_byte].decode(
            "utf-8", errors="ignore"
        )

        # extends clause
        extends_name: str | None = None
        heritage = _find_first_child_by_type(node, "class_heritage")
        if heritage:
            # class_heritage: "extends Foo" — find the identifier
            ext_id = _find_first_child_by_type(heritage, "identifier")
            if ext_id:
                extends_name = source[ext_id.start_byte:ext_id.end_byte].decode(
                    "utf-8", errors="ignore"
                )

        # static template = "..." inside class body
        template_val: str | None = None
        body = _find_first_child_by_type(node, "class_body")
        if body:
            for child in body.children:
                if child.type != "field_definition":
                    continue
                # Check it's named "template"
                field_name_node = _find_first_child_by_type(child, "property_identifier")
                if not field_name_node:
                    continue
                if source[field_name_node.start_byte:field_name_node.end_byte] != b"template":
                    continue
                # Get the value — look for string node
                for val_node in child.children:
                    if val_node.type in ("string", "template_string"):
                        raw = source[val_node.start_byte:val_node.end_byte]
                        template_val = raw.decode("utf-8", errors="ignore").strip("\"'`")
                        break

        # bound_model: heuristic from ORM calls and resModel/model kwargs in class body
        bound_model: str | None = None
        if body:
            bound_model = _detect_bound_model_from_class_body(body, source)

        result.components.append(OWLCompInfo(
            name=class_name,
            module=module_info.name,
            odoo_version=module_info.odoo_version,
            template=template_val,
            extends=extends_name,
            bound_model=bound_model,
            file_path=filepath,
        ))


def _extract_graph_from_file(
    filepath: str, module_info: ModuleInfo, result: JSGraphResult
) -> None:
    try:
        source = Path(filepath).read_bytes()
    except OSError:
        return
    src_str = source.decode("utf-8", errors="ignore")
    era = _detect_era(src_str)
    tree = _get_parser().parse(source)

    if era == "era1":
        _extract_era1_patches(tree, source, module_info, filepath, result)
    elif era == "era2":
        _extract_era2_patches(tree, source, module_info, filepath, result)
    else:  # era3
        _extract_era3_patches(tree, source, module_info, filepath, result)
        _extract_era3_components(tree, source, module_info, filepath, result)


def parse_module_graph(module_info: ModuleInfo) -> JSGraphResult:
    """Extract JS graph entities (patches + OWL components) from a module's static/src/."""
    result = JSGraphResult(module=module_info)
    module_path = Path(module_info.path)
    static_src = module_path / "static" / "src"
    if not static_src.exists():
        return result

    for js_file in sorted(static_src.rglob("*.js")):
        if any(part in _SKIP_DIRS for part in js_file.relative_to(static_src).parts):
            continue
        if js_file.stat().st_size > _MAX_JS_BYTES:
            continue
        _extract_graph_from_file(str(js_file), module_info, result)

    return result
