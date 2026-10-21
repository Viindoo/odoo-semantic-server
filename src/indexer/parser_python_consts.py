# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_python_consts.py
"""Static resolution of simple string constants in model meta attributes.

Odoo addons sometimes declare a model identity through a constant instead of a
literal::

    INTROSPECT_SERVICE = 'viin.ai.ui.introspect'      # models/ui_introspect.py

    class UiIntrospect(models.AbstractModel):
        _name = INTROSPECT_SERVICE

    from .ui_introspect import INTROSPECT_SERVICE      # models/ui_suggest.py

or reuse an attribute bound earlier in the same class body
(``_name = 'ir.module.module'`` then ``_inherit = _name``, Odoo ``website``).
The era2 AST parser used to accept only literals there, so such a class lost its
``_name`` (the model and all its methods vanished from the parse) or its
``_inherit`` (no INHERITS edge, wrong ``is_definition``).

This module evaluates, WITHOUT executing anything:

* a string literal, or a tuple / list whose elements are string literals or
  resolvable names;
* a ``Name`` bound in the class body before the statement that reads it (class
  scope first, as Python resolves it);
* a ``Name`` bound at module level of the same file BEFORE the class statement
  (top-level classes only - a class nested in a function may see a function
  local instead);
* a ``Name`` imported from a file of the same addon: ``from .x import NAME``,
  ``from ..x import NAME``, ``from . import NAME`` (the package
  ``__init__.py``), ``from odoo.addons.<this_addon>.x import NAME``, with
  ``as`` aliases, followed through re-exports.

Anything else - a call, a concatenation, an attribute (``const.NAME``), a name
rebound inside ``if`` / ``try`` / a loop, a star import, a file outside the
addon directory, an import chain longer than :data:`_MAX_IMPORT_HOPS` or a cycle
- is unresolvable and the caller keeps its literal-only behaviour.

The text-regex fallback (``parser_python_era1``, files Python 3 cannot parse,
mostly v8-v10 Python-2 source) does not use this: it has no AST, and the
surveyed v8-v9 checkouts declare every ``_name`` / ``_inherit`` as a literal.
"""
from __future__ import annotations

import ast
import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .parser_util import parse_external_source

_logger = logging.getLogger(__name__)

# A resolved constant: one string or a tuple of strings.
ConstValue = str | tuple[str, ...]
# lookup(name) -> value, or None when the name is not statically resolvable.
Lookup = Callable[[str], "ConstValue | None"]

# Longest re-export chain followed (``from .a import X`` where a.py has
# ``from .b import X`` ...). Real addons use one hop; the bound only stops a
# pathological chain.
_MAX_IMPORT_HOPS = 8

# Top-level absolute import prefixes that name an addon (``odoo.addons.<name>``,
# v8-v9 ``openerp.addons.<name>``).
_ADDON_IMPORT_PREFIXES = ("odoo.addons.", "openerp.addons.")


@dataclass(frozen=True)
class _Value:
    """A binding to an evaluated constant (``value`` None: not a constant)."""
    value: ConstValue | None


@dataclass(frozen=True)
class _Import:
    """A binding to a name of another file of the addon."""
    candidates: tuple[Path, ...]   # files that may define it, in Python's order
    source_name: str


_Binding = _Value | _Import


def evaluate(node: ast.expr | None, lookup: Lookup | None) -> ConstValue | None:
    """The constant *node* denotes, or None when it is not statically known.

    A tuple / list evaluates only when EVERY element does (a partial sequence
    would silently drop a parent); the caller decides what to do with None.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return lookup(node.id) if lookup is not None else None
    if isinstance(node, (ast.Tuple, ast.List)):
        items: list[str] = []
        for elt in node.elts:
            # A nested sequence or a `*BASE` (Starred) element is not a valid
            # model name: the whole sequence is unresolvable.
            value = evaluate(elt, lookup)
            if not isinstance(value, str):
                return None
            items.append(value)
        return tuple(items)
    return None


def bound_names(node: ast.AST) -> Iterator[str]:
    """Names a statement binds, without entering nested function / class scopes."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        yield node.name
        return
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            if alias.name != "*":
                yield alias.asname or alias.name.split(".")[0]
        return
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        yield node.id
        return
    for child in ast.iter_child_nodes(node):
        yield from bound_names(child)


class _FileScope:
    """Module-level bindings of one file, in statement order."""

    def __init__(self, tree: ast.Module, path: Path | None, resolver: ConstantResolver | None):
        self._path = path
        self._resolver = resolver
        self._bindings: dict[str, list[tuple[int, _Binding]]] = {}
        for stmt in tree.body:
            self._record(stmt)

    def _bind(self, name: str, line: int, binding: _Binding) -> None:
        self._bindings.setdefault(name, []).append((line, binding))

    def _record(self, stmt: ast.stmt) -> None:
        line = stmt.lineno
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            simple = [t.id for t in targets if isinstance(t, ast.Name)]
            if len(simple) == len(targets) and stmt.value is not None:
                # Evaluated against the bindings recorded so far (statement order).
                value = evaluate(stmt.value, self._lookup_before(line))
                for name in simple:
                    self._bind(name, line, _Value(value))
                return
        elif isinstance(stmt, ast.ImportFrom) and self._path is not None:
            candidates = self._import_candidates(stmt)
            if candidates is not None:
                for alias in stmt.names:
                    if alias.name == "*":
                        continue
                    self._bind(
                        alias.asname or alias.name, line, _Import(candidates, alias.name),
                    )
                return
        # Any other binding (def, class, import of a package, a name bound in an
        # if / try / for / with, tuple unpacking ...) hides earlier constants.
        for name in bound_names(stmt):
            self._bind(name, line, _Value(None))

    def _import_candidates(self, stmt: ast.ImportFrom) -> tuple[Path, ...] | None:
        """Files of the addon ``from <stmt.module> import ...`` may read, or None."""
        root = self._resolver.root if self._resolver is not None else None
        if root is None or self._path is None:
            return None
        if stmt.level:
            base = self._path.parent
            for _ in range(stmt.level - 1):
                base = base.parent
            parts = stmt.module.split(".") if stmt.module else []
        else:
            module = stmt.module or ""
            prefix = next(
                (p for p in _ADDON_IMPORT_PREFIXES
                 if module == p + root.name or module.startswith(p + root.name + ".")),
                None,
            )
            if prefix is None:
                return None
            base = root
            parts = module[len(prefix) + len(root.name):].lstrip(".").split(".")
            parts = [p for p in parts if p]
        target = base.joinpath(*parts) if parts else base
        candidates = (target / "__init__.py",) if not parts else (
            target.with_suffix(".py"), target / "__init__.py",
        )
        try:
            inside = [c for c in candidates if c.resolve().is_relative_to(root)]
        except (OSError, RuntimeError):
            return None
        return tuple(inside) or None

    def _lookup_before(self, line: float) -> Lookup:
        return lambda name: self.lookup(name, before_line=line)

    def lookup(
        self, name: str, *, before_line: float = math.inf,
        _seen: frozenset[tuple[str, str]] = frozenset(), _hops: int = 0,
    ) -> ConstValue | None:
        """Value of module-level *name* as bound before *before_line*, or None."""
        bound = [b for line, b in self._bindings.get(name, ()) if line < before_line]
        if not bound:
            return None
        binding = bound[-1]
        if isinstance(binding, _Value):
            return binding.value
        if self._resolver is None or _hops >= _MAX_IMPORT_HOPS:
            return None
        return self._resolver.lookup_in(
            binding.candidates, binding.source_name, _seen=_seen, _hops=_hops + 1,
        )


class ConstantResolver:
    """Per-addon cache of file scopes; resolves names across the addon's files.

    *module_root* is the addon directory: an import is followed only to a file
    inside it. One instance serves every file of one ``parse_module`` call.
    """

    def __init__(self, module_root: str | Path | None):
        try:
            self.root = Path(module_root).resolve() if module_root else None
        except (OSError, RuntimeError):
            self.root = None
        self._scopes: dict[Path, _FileScope | None] = {}

    def scope_for(self, tree: ast.Module, path: str | Path | None) -> _FileScope:
        """The module-level scope of an already parsed file."""
        resolved = self._resolve(path)
        if resolved is not None:
            self._scopes[resolved] = None   # an import cycle back here stops
        scope = _FileScope(tree, resolved, self)
        if resolved is not None:
            self._scopes[resolved] = scope
        return scope

    @staticmethod
    def _resolve(path: str | Path | None) -> Path | None:
        if path is None:
            return None
        try:
            return Path(path).resolve()
        except (OSError, RuntimeError):
            return None

    def _scope_of_file(self, path: Path) -> _FileScope | None:
        key = self._resolve(path)
        if key is None:
            return None
        if key not in self._scopes:
            self._scopes[key] = None   # in progress / unreadable: never re-entered
            try:
                source = key.read_text(encoding="utf-8", errors="ignore")
                tree = parse_external_source(source, filename=str(key))
            except (OSError, SyntaxError, ValueError):
                return None
            self._scopes[key] = _FileScope(tree, key, self)
        return self._scopes[key]

    def lookup_in(
        self, candidates: tuple[Path, ...], name: str, *,
        _seen: frozenset[tuple[str, str]] = frozenset(), _hops: int = 0,
    ) -> ConstValue | None:
        """Value of *name* in the first existing candidate file, or None."""
        for path in candidates:
            if not path.is_file():
                continue
            key = (str(path), name)
            if key in _seen:
                return None   # import cycle
            scope = self._scope_of_file(path)
            if scope is None:
                return None
            return scope.lookup(name, _seen=_seen | {key}, _hops=_hops)
        return None


def class_lookup(
    class_bindings: dict[str, ConstValue | None],
    module_scope: _FileScope | None,
    class_line: int,
) -> Lookup:
    """Lookup for a class body: names bound earlier in the class, then the module.

    *class_bindings* is filled by the caller while it walks the class body, so a
    name resolves to its binding as of the statement being evaluated.
    *module_scope* is None for a class that is not at module level.
    """
    def lookup(name: str) -> ConstValue | None:
        if name in class_bindings:
            return class_bindings[name]
        if module_scope is None:
            return None
        return module_scope.lookup(name, before_line=class_line)

    return lookup


def log_unresolved(attr: str, node: ast.expr, class_name: str, file_path: str | None) -> None:
    """DEBUG trace for a meta attribute whose value could not be evaluated."""
    _logger.debug(
        "parser_python: %s of class %s in %s is not a statically resolvable "
        "constant (%s) - treated as absent",
        attr, class_name, file_path or "<source>", ast.dump(node)[:120],
    )
