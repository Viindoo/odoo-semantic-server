"""libcst-based parser for Odoo Python source: models, fields, methods."""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import blake2b
from typing import NamedTuple

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedModel:
    name: str | None
    inherit: tuple[str, ...]
    inherits: Mapping[str, str]
    table: str | None
    rec_name: str | None
    order: str | None
    abstract: bool
    transient: bool
    register_false: bool
    start_line: int
    end_line: int
    content_hash: str
    file_path: str
    class_name: str
    indexer_notes: Mapping[str, bool]


@dataclass(frozen=True)
class ParsedField:
    model_class_name: str
    field_name: str
    field_type: str
    compute: str | None
    inverse: str | None
    search: str | None
    store: bool | None
    required: bool | None
    readonly: bool | None
    related: str | None
    default_source: str | None
    comodel_name: str | None
    depends: tuple[str, ...]
    start_line: int
    end_line: int
    content_hash: str
    indexer_notes: Mapping[str, bool]


@dataclass(frozen=True)
class ParsedMethod:
    model_class_name: str
    method_name: str
    signature: str
    decorators: tuple[str, ...]
    calls_super: bool
    async_def: bool
    start_line: int
    end_line: int
    content_hash: str


class FileParseResult(NamedTuple):
    models: list[ParsedModel]
    fields: list[ParsedField]
    methods: list[ParsedMethod]
    notes: dict[str, object]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ODOO_BASE_CLASSES = {"Model", "TransientModel", "AbstractModel"}

# Track whether a class derives from models.X; also plain Model if imported directly.
_MODEL_QUALIFIERS = {f"models.{b}" for b in _ODOO_BASE_CLASSES} | _ODOO_BASE_CLASSES


def _content_hash(source: str) -> str:
    """blake2b-16 hex of normalized source (strip trailing ws, remove BOM)."""
    normalized = "\n".join(line.rstrip() for line in source.lstrip("﻿").splitlines())
    return blake2b(normalized.encode(), digest_size=16).hexdigest()


def _extract_string_literal(node: cst.BaseExpression) -> str | None:
    """Return the Python string value of a SimpleString/ConcatenatedString, or None."""
    if isinstance(node, cst.SimpleString):
        try:
            return str(eval(node.value))  # noqa: S307 — evaluating a string literal
        except Exception:
            return None
    if isinstance(node, cst.FormattedString):
        return None
    if isinstance(node, cst.ConcatenatedString):
        left = _extract_string_literal(node.left)
        right = _extract_string_literal(node.right)
        if left is not None and right is not None:
            return left + right
        return None
    return None



def _extract_string_list(node: cst.BaseExpression) -> list[str] | None:
    """Parse `_inherit = 'x'` or `_inherit = ['x', 'y']`; return None if non-literal."""
    if isinstance(node, (cst.SimpleString, cst.ConcatenatedString, cst.FormattedString)):
        s = _extract_string_literal(node)
        return [s] if s is not None else None
    if isinstance(node, cst.List):
        result: list[str] = []
        for el in node.elements:
            if isinstance(el, cst.Element):
                s = _extract_string_literal(el.value)
                if s is None:
                    return None
                result.append(s)
        return result
    return None


def _extract_dict_str_str(node: cst.BaseExpression) -> dict[str, str] | None:
    """Parse `_inherits = {'model': 'field'}` dict literal."""
    if not isinstance(node, cst.Dict):
        return None
    result: dict[str, str] = {}
    for el in node.elements:
        if not isinstance(el, cst.DictElement):
            return None
        k = _extract_string_literal(el.key)
        v = _extract_string_literal(el.value)
        if k is None or v is None:
            return None
        result[k] = v
    return result


def _extract_bool(node: cst.BaseExpression) -> bool | None:
    if isinstance(node, cst.Name):
        if node.value == "True":
            return True
        if node.value == "False":
            return False
    return None


def _qualified_base(base: cst.Arg | cst.BaseExpression) -> str | None:
    """Return 'models.Model' / 'Model' style string from a class base."""
    expr: cst.BaseExpression = base.value if isinstance(base, cst.Arg) else base
    if isinstance(expr, cst.Attribute):
        if isinstance(expr.value, cst.Name):
            return f"{expr.value.value}.{expr.attr.value}"
    if isinstance(expr, cst.Name):
        return expr.value
    return None


def _is_odoo_model_base(base: cst.Arg) -> tuple[bool, bool, bool]:
    """Return (is_model, abstract, transient)."""
    q = _qualified_base(base)
    if q in ("models.Model", "Model"):
        return True, False, False
    if q in ("models.AbstractModel", "AbstractModel"):
        return True, True, False
    if q in ("models.TransientModel", "TransientModel"):
        return True, False, True
    return False, False, False



# ---------------------------------------------------------------------------
# Visitor — one pass collects models + fields + methods
# ---------------------------------------------------------------------------


@dataclass
class _ClassContext:
    class_name: str
    is_model: bool
    abstract: bool
    transient: bool
    depth: int
    start_line: int
    end_line: int
    # model attrs
    model_name: str | None = None
    inherit: list[str] = field(default_factory=list)
    inherits_map: dict[str, str] = field(default_factory=dict)
    table: str | None = None
    rec_name: str | None = None
    order: str | None = None
    register_false: bool = False
    dynamic_inherit: bool = False
    # pending @api.depends for next field assignment
    pending_depends: tuple[str, ...] = ()


class _OdooVisitor(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, source_code: str, file_path: str) -> None:
        self._source = source_code
        self._file_path = file_path
        self._module: cst.Module | None = None

        self._class_stack: list[_ClassContext] = []
        self._models: list[ParsedModel] = []
        self._fields: list[ParsedField] = []
        self._methods: list[ParsedMethod] = []

        # pending @api.depends collected just before a field SimpleStatementLine
        self._pending_depends: tuple[str, ...] = ()

    # -----------------------------------------------------------------------
    # Module
    # -----------------------------------------------------------------------

    def visit_Module(self, node: cst.Module) -> None:
        self._module = node

    # -----------------------------------------------------------------------
    # Classes
    # -----------------------------------------------------------------------

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        pos = self.get_metadata(PositionProvider, node)
        start_line = pos.start.line
        end_line = pos.end.line

        is_model = False
        abstract = False
        transient = False

        for base in node.bases:
            m, a, t = _is_odoo_model_base(base)
            if m:
                is_model = True
                abstract = a
                transient = t
                break

        # Only top-level classes are independent models; nested classes are tracked
        # at their own depth but NOT emitted as independent model rows.
        ctx = _ClassContext(
            class_name=node.name.value,
            is_model=is_model,
            abstract=abstract,
            transient=transient,
            depth=len(self._class_stack),
            start_line=start_line,
            end_line=end_line,
        )
        self._class_stack.append(ctx)
        return True  # visit children

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        ctx = self._class_stack.pop()

        # Only emit top-level model classes (depth == 0 when we entered).
        if ctx.is_model and ctx.depth == 0:
            pos = self.get_metadata(PositionProvider, node)
            source_snippet = self._source_for_lines(pos.start.line, pos.end.line)
            notes: dict[str, bool] = {}
            if ctx.dynamic_inherit:
                notes["dynamic_inherit"] = True
            if ctx.register_false:
                notes["register_false_chain"] = True

            self._models.append(
                ParsedModel(
                    name=ctx.model_name,
                    inherit=tuple(ctx.inherit),
                    inherits=dict(ctx.inherits_map),
                    table=ctx.table,
                    rec_name=ctx.rec_name,
                    order=ctx.order,
                    abstract=ctx.abstract,
                    transient=ctx.transient,
                    register_false=ctx.register_false,
                    start_line=ctx.start_line,
                    end_line=ctx.end_line,
                    content_hash=_content_hash(source_snippet),
                    file_path=self._file_path,
                    class_name=ctx.class_name,
                    indexer_notes=notes,
                )
            )

    # -----------------------------------------------------------------------
    # Class body — simple assignments (_name, _inherit, etc.)
    # -----------------------------------------------------------------------

    def visit_SimpleStatementLine(self, node: cst.SimpleStatementLine) -> bool:
        if not self._class_stack:
            return True

        ctx = self._current_model_ctx()
        if ctx is None:
            return True

        # Check if line has a decorator-like @api.depends that precedes it.
        # (Decorators on fields are handled via the preceding FunctionDef visitor.)
        # Here we look for the depends accumulated before this statement.
        depends_for_field = self._pending_depends
        self._pending_depends = ()

        for stmt in node.body:
            if not isinstance(stmt, cst.Assign):
                continue
            for target_node in stmt.targets:
                if not isinstance(target_node.target, cst.Name):
                    continue
                name = target_node.target.value
                val = stmt.value

                if ctx is not None:
                    if name == "_name":
                        s = _extract_string_literal(val)
                        if s:
                            ctx.model_name = s
                    elif name == "_inherit":
                        lst = _extract_string_list(val)
                        if lst is not None:
                            ctx.inherit = lst
                        else:
                            ctx.dynamic_inherit = True
                    elif name == "_inherits":
                        d = _extract_dict_str_str(val)
                        if d is not None:
                            ctx.inherits_map = d
                    elif name == "_table":
                        s = _extract_string_literal(val)
                        if s:
                            ctx.table = s
                    elif name == "_rec_name":
                        s = _extract_string_literal(val)
                        if s:
                            ctx.rec_name = s
                    elif name == "_order":
                        s = _extract_string_literal(val)
                        if s:
                            ctx.order = s
                    elif name == "_register":
                        b = _extract_bool(val)
                        if b is False:
                            ctx.register_false = True

            # Check for field assignment: `<attr> = fields.<Type>(...)`.
            if len(node.body) == 1 and isinstance(stmt, cst.Assign):
                self._try_extract_field(stmt, node, depends_for_field, ctx)

        return True

    def _try_extract_field(
        self,
        stmt: cst.Assign,
        line_node: cst.SimpleStatementLine,
        depends: tuple[str, ...],
        ctx: _ClassContext,
    ) -> None:
        if len(stmt.targets) != 1:
            return
        target = stmt.targets[0].target
        if not isinstance(target, cst.Name):
            return
        field_name = target.value
        if field_name.startswith("_"):
            return

        call = stmt.value
        if not isinstance(call, cst.Call):
            return
        func = call.func
        if isinstance(func, cst.Attribute) and isinstance(func.value, cst.Name):
            if func.value.value != "fields":
                return
            field_type = func.attr.value
        else:
            return

        # Extract keyword arguments
        compute: str | None = None
        inverse: str | None = None
        search_method: str | None = None
        store: bool | None = None
        required: bool | None = None
        readonly: bool | None = None
        related: str | None = None
        default_source: str | None = None
        comodel_name: str | None = None

        # First positional arg may be comodel_name for relational fields
        pos_args = [a for a in call.args if a.keyword is None]
        if pos_args:
            s = _extract_string_literal(pos_args[0].value)
            if s:
                comodel_name = s

        for arg in call.args:
            if arg.keyword is None:
                continue
            kw = arg.keyword.value
            if kw == "compute":
                compute = _extract_string_literal(arg.value)
            elif kw == "inverse":
                inverse = _extract_string_literal(arg.value)
            elif kw == "search":
                search_method = _extract_string_literal(arg.value)
            elif kw == "store":
                store = _extract_bool(arg.value)
            elif kw == "required":
                required = _extract_bool(arg.value)
            elif kw == "readonly":
                readonly = _extract_bool(arg.value)
            elif kw == "related":
                related = _extract_string_literal(arg.value)
            elif kw == "default":
                assert self._module is not None
                try:
                    default_source = self._module.code_for_node(arg.value)
                except Exception:
                    default_source = None
            elif kw == "comodel_name":
                s = _extract_string_literal(arg.value)
                if s:
                    comodel_name = s

        pos = self.get_metadata(PositionProvider, line_node)
        source_snippet = self._source_for_lines(pos.start.line, pos.end.line)

        self._fields.append(
            ParsedField(
                model_class_name=ctx.class_name,
                field_name=field_name,
                field_type=field_type,
                compute=compute,
                inverse=inverse,
                search=search_method,
                store=store,
                required=required,
                readonly=readonly,
                related=related,
                default_source=default_source,
                comodel_name=comodel_name,
                depends=depends,
                start_line=pos.start.line,
                end_line=pos.end.line,
                content_hash=_content_hash(source_snippet),
                indexer_notes={},
            )
        )

    # -----------------------------------------------------------------------
    # Methods
    # -----------------------------------------------------------------------

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if not self._class_stack:
            return False

        ctx = self._current_model_ctx()
        if ctx is None:
            return False

        # Only capture direct children of the innermost model class (depth == 0 from model top)
        if not self._is_direct_model_child():
            return False

        pos = self.get_metadata(PositionProvider, node)

        # Collect decorators as source text
        assert self._module is not None
        dec_texts: list[str] = []
        for dec in node.decorators:
            try:
                dec_src = self._module.code_for_node(dec)
            except Exception:
                dec_src = ""
            dec_texts.append(dec_src.strip())

        # Build signature = "(params)" source text
        try:
            sig = f"({self._module.code_for_node(node.params)})"
        except Exception:
            sig = "(self)"

        # Detect super() call in body
        calls_super = _body_calls_super(node.body, node.name.value)

        source_snippet = self._source_for_lines(pos.start.line, pos.end.line)

        self._methods.append(
            ParsedMethod(
                model_class_name=ctx.class_name,
                method_name=node.name.value,
                signature=sig,
                decorators=tuple(dec_texts),
                calls_super=calls_super,
                async_def=isinstance(node, cst.FunctionDef) and node.asynchronous is not None,
                start_line=pos.start.line,
                end_line=pos.end.line,
                content_hash=_content_hash(source_snippet),
            )
        )
        return False  # don't visit children (nested functions not independent)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _current_model_ctx(self) -> _ClassContext | None:
        """Return innermost class context that IS an Odoo model (top-level only)."""
        if not self._class_stack:
            return None
        # The outermost class with is_model=True at depth=0
        for ctx in self._class_stack:
            if ctx.is_model and ctx.depth == 0:
                return ctx
        return None

    def _is_direct_model_child(self) -> bool:
        """True if we are directly inside a top-level model class (stack depth == 1)."""
        if len(self._class_stack) != 1:
            return False
        return self._class_stack[0].is_model and self._class_stack[0].depth == 0

    def _source_for_lines(self, start: int, end: int) -> str:
        lines = self._source.splitlines()
        return "\n".join(lines[start - 1 : end])


# ---------------------------------------------------------------------------
# super() call detection
# ---------------------------------------------------------------------------


class _SuperCallVisitor(cst.CSTVisitor):
    def __init__(self, method_name: str) -> None:
        self._method_name = method_name
        self.found = False

    def visit_Call(self, node: cst.Call) -> None:
        func = node.func
        # Matches both super().<method>(…) and super(ClassName, self).<method>(…)
        if (
            isinstance(func, cst.Attribute)
            and isinstance(func.value, cst.Call)
            and isinstance(func.value.func, cst.Name)
            and func.value.func.value == "super"
            and func.attr.value == self._method_name
        ):
            self.found = True


def _body_calls_super(body: cst.BaseSuite, method_name: str) -> bool:
    visitor = _SuperCallVisitor(method_name)
    body.visit(visitor)
    return visitor.found


# ---------------------------------------------------------------------------
# Post-process: link @api.depends to fields via compute= method name
# ---------------------------------------------------------------------------


def _link_depends(
    models: list[ParsedModel],
    fields: list[ParsedField],
    methods: list[ParsedMethod],
) -> list[ParsedField]:
    """For each field with compute=X, find the method X and copy its @api.depends args."""
    method_depends: dict[tuple[str, str], tuple[str, ...]] = {}
    for m in methods:
        for dec in m.decorators:
            if "@api.depends(" in dec:
                # parse the depends args from decorator text
                try:
                    dec_node = cst.parse_expression(dec.lstrip("@"))
                    if isinstance(dec_node, cst.Call):
                        deps: list[str] = []
                        for a in dec_node.args:
                            s = _extract_string_literal(a.value)
                            if s:
                                deps.append(s)
                        if deps:
                            method_depends[(m.model_class_name, m.method_name)] = tuple(deps)
                except cst.ParserSyntaxError:
                    _logger.warning("failed to parse decorator for depends: %s", dec)
                    continue

    updated: list[ParsedField] = []
    for f in fields:
        if f.compute and not f.depends:
            linked_deps: tuple[str, ...] | None = method_depends.get(
                (f.model_class_name, f.compute)
            )
            if linked_deps:
                updated.append(
                    ParsedField(
                        model_class_name=f.model_class_name,
                        field_name=f.field_name,
                        field_type=f.field_type,
                        compute=f.compute,
                        inverse=f.inverse,
                        search=f.search,
                        store=f.store,
                        required=f.required,
                        readonly=f.readonly,
                        related=f.related,
                        default_source=f.default_source,
                        comodel_name=f.comodel_name,
                        depends=linked_deps,
                        start_line=f.start_line,
                        end_line=f.end_line,
                        content_hash=f.content_hash,
                        indexer_notes=f.indexer_notes,
                    )
                )
                continue
        updated.append(f)
    return updated


# ---------------------------------------------------------------------------
# Conditional import scanner
# ---------------------------------------------------------------------------


def _is_import_error_guard(node: cst.Try) -> bool:
    """Return True if any handler of a try block catches ImportError."""
    return any(
        isinstance(h.type, cst.Name) and h.type.value == "ImportError"
        or (
            isinstance(h.type, cst.Attribute)
            and isinstance(h.type.attr, cst.Name)
            and h.type.attr.value == "ImportError"
        )
        for h in node.handlers
        if h.type is not None
    )


class _ConditionalImportVisitor(cst.CSTVisitor):
    """Walks a models/__init__.py and collects submodule names imported
    inside try/except ImportError blocks."""

    def __init__(self) -> None:
        self._in_try_except: int = 0
        self.conditional_submodules: set[str] = set()

    def visit_Try(self, node: cst.Try) -> bool:
        if _is_import_error_guard(node):
            self._in_try_except += 1
        return True

    def leave_Try(self, node: cst.Try) -> None:
        if _is_import_error_guard(node) and self._in_try_except > 0:
            self._in_try_except -= 1

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if self._in_try_except <= 0:
            return
        if isinstance(node.names, cst.ImportStar):
            return
        for alias in node.names:
            if isinstance(alias, cst.ImportAlias):
                if isinstance(alias.name, cst.Name):
                    self.conditional_submodules.add(alias.name.value)
                elif isinstance(alias.name, cst.Attribute):
                    # from . import foo.bar — last attr
                    self.conditional_submodules.add(alias.name.attr.value)


def scan_models_package(init_path: pathlib.Path) -> set[str]:
    """Return submodule names that are conditionally imported (inside try/except ImportError)."""
    try:
        source = init_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _logger.warning("scan_models_package: cannot read %s: %s", init_path, exc)
        return set()
    try:
        tree = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        _logger.warning("scan_models_package: parse error in %s: %s", init_path, exc)
        return set()
    visitor = _ConditionalImportVisitor()
    tree.visit(visitor)
    return visitor.conditional_submodules


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_file(
    path: pathlib.Path,
    conditional_submodules: set[str] | None = None,
) -> FileParseResult:
    """Parse a single Python file and return (models, fields, methods, notes).

    conditional_submodules: set of submodule names whose classes should be
    flagged with conditional_import=True (from scan_models_package).
    """
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        _logger.warning("parse_file: encoding error reading %s: %s", path, exc)
        return FileParseResult([], [], [], {"error": str(exc)})

    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        _logger.warning("parse_file: syntax error in %s: %s", path, exc)
        return FileParseResult([], [], [], {"error": str(exc)})

    wrapper = MetadataWrapper(module)
    visitor = _OdooVisitor(source, str(path))
    wrapper.visit(visitor)

    models_list = visitor._models
    fields_list = visitor._fields
    methods_list = visitor._methods

    # Apply conditional_import flag if this file's stem is in the conditional set
    if conditional_submodules:
        stem = path.stem
        if stem in conditional_submodules:
            models_list = [
                ParsedModel(
                    name=m.name,
                    inherit=m.inherit,
                    inherits=m.inherits,
                    table=m.table,
                    rec_name=m.rec_name,
                    order=m.order,
                    abstract=m.abstract,
                    transient=m.transient,
                    register_false=m.register_false,
                    start_line=m.start_line,
                    end_line=m.end_line,
                    content_hash=m.content_hash,
                    file_path=m.file_path,
                    class_name=m.class_name,
                    indexer_notes={**dict(m.indexer_notes), "conditional_import": True},
                )
                for m in models_list
            ]

    # Link @api.depends to fields
    fields_list = _link_depends(models_list, fields_list, methods_list)

    notes: dict[str, object] = {}
    return FileParseResult(models_list, fields_list, methods_list, notes)
