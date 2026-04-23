"""Handler for the `resolve_model` MCP tool.

Matches `docs/specs/resolve_model.md` §3 output schema at the minimum-viable
level agreed with the golden fixture (`tests/fixtures/golden/resolve_model.json`):

    {
      "model_name": "...",
      "abstract": bool,
      "transient": bool,
      "inherits": {parent: fk_field, ...},
      "chain": [{"module": "...", "file": "..."}, ...],
      "warnings": [...]
    }

`fields_contributed` / `methods_contributed` summaries from spec §3 are
deferred — callers can fetch them through `resolve_field` / `resolve_method`
when needed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from osm.server.db import effective_indexed_at_sha, union_all
from osm.server.errors import InvalidInputError, NotFoundError, StaleIndexError
from osm.server.tenancy import TenantContext


class ResolveModelInput(BaseModel):
    model_name: str = Field(..., min_length=1)
    include_field_summary: bool = True
    include_method_summary: bool = False


def resolve_model(
    cur: Any,
    ctx: TenantContext,
    model_name: str,
    *,
    include_field_summary: bool = True,
    include_method_summary: bool = False,
) -> dict[str, Any]:
    if not model_name:
        raise InvalidInputError("model_name must be non-empty")

    sql = union_all(
        """
        SELECT m.name, mod.name AS module_name, m.file_path,
               m.is_primary_declaration, m.abstract, m.transient,
               m.delegates_to, m.indexer_notes, m.indexed_at_sha,
               mod.load_order
          FROM {schema}.models m
          JOIN {schema}.modules mod ON mod.id = m.module_id
         WHERE m.name = %s
        """,
        ctx,
    ) + "\nORDER BY osm_u.load_order ASC, osm_u.module_name ASC"

    # INVARIANT: params length must equal (placeholders per SELECT block) *
    # len(ctx.schemas). The inner SELECT has 1 `%s` (model_name); adding
    # another `%s` without bumping the multiplier here will silently bind
    # shifted values. Same pattern in resolve_field.py and resolve_method.py.
    params = tuple([model_name] * len(ctx.schemas))
    cur.execute(sql, params)
    rows = list(cur.fetchall())

    if not rows:
        raise NotFoundError(f"model {model_name!r} not in index")

    chain: list[dict[str, Any]] = []
    abstract = False
    transient = False
    inherits: dict[str, str] = {}
    warnings: list[str] = []
    shas: list[str] = []

    for (
        _name, module_name, file_path, is_primary, is_abstract, is_transient,
        delegates_to, indexer_notes, indexed_at_sha, _load_order,
    ) in rows:
        entry: dict[str, Any] = {"module": module_name, "file": file_path}
        if is_primary:
            entry["kind"] = "primary"
            abstract = bool(is_abstract)
            transient = bool(is_transient)
            inherits = dict(delegates_to or {})
        chain.append(entry)
        shas.append(indexed_at_sha)
        if indexer_notes and indexer_notes.get("dynamic_inherit"):
            warnings.append(
                f"{module_name}: dynamic_inherit on {model_name}; chain may be incomplete"
            )
        if indexer_notes and indexer_notes.get("conditional_import"):
            warnings.append(
                f"{module_name}: conditional_import on {model_name}; "
                "chain valid only when the optional dep is installed"
            )
        if indexer_notes and indexer_notes.get("register_false_chain"):
            warnings.append(
                f"{module_name}: _register=False in ancestry on {model_name}; "
                "registration cannot be confirmed statically"
            )

    sha = effective_indexed_at_sha(shas)
    if sha is None:
        raise StaleIndexError("stale_cross_schema_ref on models rows")

    # include_* flags are accepted for forward compatibility; field/method
    # summaries live in dedicated tools for P1 so we do not re-derive them
    # here.
    _ = include_field_summary, include_method_summary

    return {
        "result": {
            "model_name": model_name,
            "chain": chain,
            "inherits": inherits,
            "abstract": abstract,
            "transient": transient,
            "warnings": warnings,
        },
        "indexed_at_sha": sha,
        "warnings": warnings,
    }
