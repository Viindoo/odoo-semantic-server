"""Override-chain computation for fields (load-order stack) and methods (linear chain + C3 MRO).

Field stack: last-loaded definition wins; chain is ordered earliest-first so
override_of points at the prior row and the chain tail is authoritative.
Method chain: same linear sort; MRO is derived at query time via
compute_method_mro().
_inherits synthesis: delegated parent fields appear as synthesized links on the
child model; child-local definitions suppress synthesis (Risk R1).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from osm.indexer.load_order import LoadOrderRecord
from osm.indexer.python_parser import FileParseResult, ParsedField, ParsedMethod, ParsedModel

if TYPE_CHECKING:
    from collections.abc import Iterable

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldOverrideLink:
    field_row_id: int | None
    model_name: str
    field_name: str
    module_name: str
    load_order: int
    override_of: FieldOverrideLink | None
    source_row: ParsedField | None
    synthesized: bool
    synthesized_via: str | None


@dataclass(frozen=True)
class MethodOverrideLink:
    method_row_id: int | None
    model_name: str
    method_name: str
    module_name: str
    load_order: int
    override_of: MethodOverrideLink | None
    source_row: ParsedMethod | None


@dataclass(frozen=True)
class ResolverResult:
    field_chains: list[FieldOverrideLink]
    method_chains: list[MethodOverrideLink]
    synthesized_fields: list[FieldOverrideLink]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _model_names_for(model: ParsedModel) -> list[str]:
    """Return all model names a ParsedModel contributes to.

    - Declared model (_name set): contributes only to _name.
    - Pure extension (no _name, single _inherit): contributes to _inherit[0].
    - Multi-inherit (no _name, >1 _inherit): contributes to EACH inherited model;
      this matches how Odoo's field _base_fields stack works when a class
      extends multiple parents without declaring a new _name.
    """
    if model.name:
        return [model.name]
    if model.inherit:
        return list(model.inherit)
    return []


def _file_to_module(
    file_path: str,
    load_order_map: dict[str, LoadOrderRecord],
) -> str | None:
    """Extract module name by finding a known module name segment in the path."""
    normalized = file_path.replace("\\", "/")
    parts = normalized.split("/")
    for part in parts:
        if part in load_order_map:
            return part
    return None


def _build_file_alpha_index(parsed_files: Iterable[FileParseResult]) -> dict[str, int]:
    """Build a global alphabetical index over all file paths seen.

    Used as the second sort key (file_order_in_module) to produce a
    deterministic per-module file order matching the alphabetical import order
    Odoo follows within models/__init__.py.
    """
    paths: set[str] = set()
    for fr in parsed_files:
        for m in fr.models:
            paths.add(m.file_path)
    return {fp: idx for idx, fp in enumerate(sorted(paths))}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_field_override_chains(
    parsed_files: list[FileParseResult],
    load_orders: list[LoadOrderRecord],
) -> list[FieldOverrideLink]:
    """Compute field override chains for all models across all parsed files.

    Sort key per group: (load_order ASC, file_path_alpha ASC, start_line ASC).
    Within each (model_name, field_name) group, link each row's override_of to
    the previous, so the chain walks from root to latest override.
    Dynamic-inherit models produce no chain (spec §5c / Risk R8).
    """
    load_order_map = {r.name: r for r in load_orders}
    file_alpha = _build_file_alpha_index(parsed_files)

    RawEntry = tuple[tuple[int, int, int], str, ParsedField, ParsedModel, str]
    by_model_field: dict[tuple[str, str], list[RawEntry]] = defaultdict(list)

    for fr in parsed_files:
        for model in fr.models:
            if model.indexer_notes.get("dynamic_inherit"):
                continue
            module_name = _file_to_module(model.file_path, load_order_map)
            if module_name is None or module_name not in load_order_map:
                _logger.warning("cannot determine module for %s; skipping fields", model.file_path)
                continue
            lo = load_order_map[module_name].load_order
            fa = file_alpha.get(model.file_path, 0)
            for field in fr.fields:
                if field.model_class_name != model.class_name:
                    continue
                sort_key = (lo, fa, field.start_line)
                for mn in _model_names_for(model):
                    by_model_field[(mn, field.field_name)].append(
                        (sort_key, mn, field, model, module_name)
                    )

    result: list[FieldOverrideLink] = []
    for (mn, fn), entries in by_model_field.items():
        entries.sort(key=lambda e: e[0])
        prev: FieldOverrideLink | None = None
        for sort_key, _mn, field, model, module_name in entries:
            notes = {**dict(model.indexer_notes), **dict(field.indexer_notes)}
            if notes.get("conditional_import"):
                _logger.warning(
                    "field '%s' on '%s' in %s: conditional import",
                    fn,
                    mn,
                    model.file_path,
                )
            link = FieldOverrideLink(
                field_row_id=None,
                model_name=mn,
                field_name=fn,
                module_name=module_name,
                load_order=sort_key[0],
                override_of=prev,
                source_row=field,
                synthesized=False,
                synthesized_via=None,
            )
            result.append(link)
            prev = link

    return result


def compute_method_override_chains(
    parsed_files: list[FileParseResult],
    load_orders: list[LoadOrderRecord],
) -> list[MethodOverrideLink]:
    """Compute linear method override chains for all models.

    The stored chain is linear (load-order sorted); C3 MRO is derived at query
    time via compute_method_mro(). Same sort key as field chains.
    """
    load_order_map = {r.name: r for r in load_orders}
    file_alpha = _build_file_alpha_index(parsed_files)

    RawEntry = tuple[tuple[int, int, int], str, ParsedMethod, ParsedModel, str]
    by_model_method: dict[tuple[str, str], list[RawEntry]] = defaultdict(list)

    for fr in parsed_files:
        for model in fr.models:
            if model.indexer_notes.get("dynamic_inherit"):
                continue
            module_name = _file_to_module(model.file_path, load_order_map)
            if module_name is None or module_name not in load_order_map:
                _logger.warning(
                    "cannot determine module for %s; skipping methods", model.file_path
                )
                continue
            lo = load_order_map[module_name].load_order
            fa = file_alpha.get(model.file_path, 0)
            for method in fr.methods:
                if method.model_class_name != model.class_name:
                    continue
                sort_key = (lo, fa, method.start_line)
                for mn in _model_names_for(model):
                    by_model_method[(mn, method.method_name)].append(
                        (sort_key, mn, method, model, module_name)
                    )

    result: list[MethodOverrideLink] = []
    for (mn, mname), entries in by_model_method.items():
        entries.sort(key=lambda e: e[0])
        prev: MethodOverrideLink | None = None
        for sort_key, _mn, method, _model, module_name in entries:
            link = MethodOverrideLink(
                method_row_id=None,
                model_name=mn,
                method_name=mname,
                module_name=module_name,
                load_order=sort_key[0],
                override_of=prev,
                source_row=method,
            )
            result.append(link)
            prev = link

    return result


def synthesize_inherits_fields(
    parsed_files: list[FileParseResult],
    load_orders: list[LoadOrderRecord],
) -> list[FieldOverrideLink]:
    """Generate synthesized FieldOverrideLinks for _inherits delegation.

    For each ParsedModel with _inherits, walk each parent model's locally-
    declared fields. For each parent field NOT defined locally on the child,
    emit a synthesized link. Child-local definition suppresses synthesis
    (child-local wins — Risk R1 critical invariant per odoo/models.py:3374).

    synthesized_via is the FK field name (e.g. 'product_tmpl_id').
    The related path is f'{fk_name}.{parent_field_name}' — computable by
    callers from synthesized_via + field_name.
    """
    load_order_map = {r.name: r for r in load_orders}

    parent_declared_fields: dict[str, set[str]] = defaultdict(set)
    child_local_fields: dict[str, set[str]] = defaultdict(set)
    child_inherits_map: dict[str, dict[str, str]] = {}
    child_module: dict[str, str] = {}

    for fr in parsed_files:
        for model in fr.models:
            if not model.name:
                continue
            module_name = _file_to_module(model.file_path, load_order_map)
            if module_name and module_name in load_order_map:
                child_module.setdefault(model.name, module_name)

            if model.inherits:
                child_inherits_map[model.name] = dict(model.inherits)

            for field in fr.fields:
                if field.model_class_name != model.class_name:
                    continue
                parent_declared_fields[model.name].add(field.field_name)
                child_local_fields[model.name].add(field.field_name)

    result: list[FieldOverrideLink] = []
    for child_name, inherits_spec in child_inherits_map.items():
        module_name = child_module.get(child_name)
        if not module_name:
            _logger.warning(
                "cannot find module for child model %r; skipping synthesis", child_name
            )
            continue
        lo = load_order_map[module_name].load_order
        local_fields = child_local_fields.get(child_name, set())

        for parent_name, fk_field_name in inherits_spec.items():
            for pf_name in parent_declared_fields.get(parent_name, set()):
                if pf_name in local_fields:
                    continue
                if pf_name == fk_field_name:
                    continue
                link = FieldOverrideLink(
                    field_row_id=None,
                    model_name=child_name,
                    field_name=pf_name,
                    module_name=module_name,
                    load_order=lo,
                    override_of=None,
                    source_row=None,
                    synthesized=True,
                    synthesized_via=fk_field_name,
                )
                result.append(link)

    return result


def compute_method_mro(
    model_name: str,
    method_name: str,
    linear_chain: list[MethodOverrideLink],
    inheritance_graph: dict[str, list[str]],
) -> list[MethodOverrideLink]:
    """Derive MRO-ordered method dispatch list from the linear chain.

    inheritance_graph maps model_name -> ordered list of _inherit parents.
    C3 linearization determines ancestor order; the method from each ancestor
    is included if it appears in linear_chain.

    Falls back to reversed linear chain (most-recent-first) if C3 fails.
    The returned list is ordered for dispatch: index 0 is called first.
    """
    relevant = [
        lnk
        for lnk in linear_chain
        if lnk.model_name == model_name and lnk.method_name == method_name
    ]
    if not relevant:
        return []

    try:
        mro_models = _c3_linearize(model_name, inheritance_graph)
    except ValueError as exc:
        _logger.warning(
            "C3 linearization failed for model %r: %s; falling back to linear chain",
            model_name,
            exc,
        )
        return list(reversed(relevant))

    seen_modules: set[str] = set()
    ordered: list[MethodOverrideLink] = []
    for ancestor_model in mro_models:
        for lnk in relevant:
            if lnk.model_name == ancestor_model and lnk.module_name not in seen_modules:
                seen_modules.add(lnk.module_name)
                ordered.append(lnk)

    for lnk in reversed(relevant):
        if lnk.module_name not in seen_modules:
            seen_modules.add(lnk.module_name)
            ordered.append(lnk)

    return ordered


def _c3_linearize(model_name: str, graph: dict[str, list[str]]) -> list[str]:
    """C3 linearization of model_name in the given inheritance graph.

    graph maps each model to its direct parents in declaration order.
    Raises ValueError on inconsistent (cyclic or ambiguous) hierarchy.
    """

    def linearize(name: str, visiting: frozenset[str]) -> list[str]:
        if name in visiting:
            raise ValueError(f"cycle involving {name!r}")
        parents = graph.get(name, [])
        if not parents:
            return [name]
        new_visiting = visiting | {name}
        parent_mros = [linearize(p, new_visiting) for p in parents]
        return [name] + _c3_merge(parent_mros + [list(parents)])

    return linearize(model_name, frozenset())


def _c3_merge(sequences: list[list[str]]) -> list[str]:
    """C3 merge step: select the next element with no tail appearances."""
    result: list[str] = []
    seqs = [list(s) for s in sequences if s]
    while True:
        seqs = [s for s in seqs if s]
        if not seqs:
            return result
        for seq in seqs:
            candidate = seq[0]
            if not any(candidate in s[1:] for s in seqs):
                result.append(candidate)
                for s in seqs:
                    if s and s[0] == candidate:
                        s.pop(0)
                break
        else:
            raise ValueError("inconsistent hierarchy; C3 linearization failed")


def compute_resolver_result(
    parsed_files: list[FileParseResult],
    load_orders: list[LoadOrderRecord],
) -> ResolverResult:
    """Top-level entry: compute all chains and return a ResolverResult.

    Collects warnings from dynamic_inherit and conditional_import flags.
    """
    warnings: list[str] = []
    for fr in parsed_files:
        for model in fr.models:
            if model.indexer_notes.get("dynamic_inherit"):
                warnings.append(
                    f"dynamic _inherit in {model.file_path} "
                    f"class {model.class_name!r}; chain omitted"
                )
            if model.indexer_notes.get("conditional_import"):
                warnings.append(
                    f"conditional import in {model.file_path} "
                    f"class {model.class_name!r}; chain may be incomplete"
                )

    return ResolverResult(
        field_chains=compute_field_override_chains(parsed_files, load_orders),
        method_chains=compute_method_override_chains(parsed_files, load_orders),
        synthesized_fields=synthesize_inherits_fields(parsed_files, load_orders),
        warnings=warnings,
    )
