"""Handler for the `resolve_field` MCP tool.

Matches the golden shape in `tests/fixtures/golden/resolve_field.json`:

    {
      "model_name": "...",
      "field_name": "...",
      "chain": [{"module", "file", "field_type", "compute"|"store"|"readonly",
                 "is_override": bool, "kind"?: "primary"}, ...],
      "effective": { non-null attrs merged last-wins },
      "warnings": [...]
    }

Chain ordering follows the _base_fields stack rule from `specs/resolve_field.md`
§5b: earliest module first, last-loaded overrides. The handler walks the
override_of link persisted in the fields table (written by the WP-6 driver).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from osm.server.db import effective_indexed_at_sha, union_all
from osm.server.errors import InvalidInputError, NotFoundError, StaleIndexError
from osm.server.tenancy import TenantContext


class ResolveFieldInput(BaseModel):
    model_name: str = Field(..., min_length=1)
    field_name: str = Field(..., min_length=1)
    include_source_snippets: bool = False


def resolve_field(
    cur: Any,
    ctx: TenantContext,
    model_name: str,
    field_name: str,
    *,
    include_source_snippets: bool = False,
) -> dict[str, Any]:
    if not model_name:
        raise InvalidInputError("model_name must be non-empty")
    if not field_name:
        raise InvalidInputError("field_name must be non-empty")
    _ = include_source_snippets  # snippet attach is P2+; accepted for forward compat

    sql = union_all(
        """
        SELECT f.id, mod.name AS module_name, f.file_path, f.field_type,
               f.compute, f.inverse, f.search, f.store, f.required, f.readonly,
               f.related_path, f.depends, f.related_model, f.default,
               f.indexed_at_sha, mod.load_order, m.is_primary_declaration,
               m.indexer_notes
          FROM {schema}.fields f
          JOIN {schema}.models m ON m.id = f.model_id
          JOIN {schema}.modules mod ON mod.id = m.module_id
         WHERE m.name = %s AND f.field_name = %s
        """,
        ctx,
    ) + "\nORDER BY osm_u.load_order ASC, osm_u.module_name ASC, osm_u.id ASC"

    # INVARIANT: 2 placeholders per SELECT block (model_name, field_name);
    # must match len(ctx.schemas) multiplication below. Adding a 3rd `%s`
    # requires updating this list too.
    params = tuple([model_name, field_name] * len(ctx.schemas))
    cur.execute(sql, params)
    rows = list(cur.fetchall())

    if not rows:
        raise NotFoundError(
            f"field {field_name!r} on model {model_name!r} not in index"
        )

    chain: list[dict[str, Any]] = []
    warnings: list[str] = []
    shas: list[str] = []
    effective: dict[str, Any] = {}

    for idx, (
        _fid, module_name, file_path, field_type, compute, inverse, search_m,
        store, required, readonly, related_path, depends, related_model,
        default, indexed_at_sha, _load_order, is_primary, indexer_notes,
    ) in enumerate(rows):
        entry: dict[str, Any] = {
            "module": module_name,
            "file": file_path,
            "field_type": field_type,
            "compute": compute,
            "is_override": idx > 0,
        }
        if is_primary:
            entry["kind"] = "primary"
        # Populate the column the golden file expects without stuffing every
        # possible attribute: golden entries include `store` or `readonly`
        # when non-null, so mirror that.
        if store is not None:
            entry["store"] = store
        if readonly is not None:
            entry["readonly"] = readonly
        chain.append(entry)
        shas.append(indexed_at_sha)

        # Last-wins effective merge over non-null values.
        if field_type is not None:
            effective["field_type"] = field_type
        if compute is not None:
            effective["compute"] = compute
        elif idx == 0:
            effective["compute"] = None  # root with no compute -> explicit null
        if inverse is not None:
            effective["inverse"] = inverse
        if search_m is not None:
            effective["search"] = search_m
        if store is not None:
            effective["store"] = store
        if required is not None:
            effective["required"] = required
        if readonly is not None:
            effective["readonly"] = readonly
        if related_path is not None:
            effective["related"] = related_path
        if related_model is not None:
            effective["related_model"] = related_model
        if default is not None:
            effective["default"] = default
        if depends:
            # Union depends across chain (last override still contributes).
            merged = list(effective.get("depends", []))
            for d in depends:
                if d not in merged:
                    merged.append(d)
            effective["depends"] = merged

        if indexer_notes and indexer_notes.get("conditional_import"):
            warnings.append(
                f"{module_name}: conditional_import on {model_name}.{field_name}; "
                "chain valid only when the optional dep is installed"
            )
        if indexer_notes and indexer_notes.get("register_false_chain"):
            warnings.append(
                f"{module_name}: _register=False in ancestry on {model_name}.{field_name}"
            )

    sha = effective_indexed_at_sha(shas)
    if sha is None:
        raise StaleIndexError("stale_cross_schema_ref on fields rows")

    return {
        "result": {
            "model_name": model_name,
            "field_name": field_name,
            "chain": chain,
            "effective": effective,
            "warnings": warnings,
        },
        "indexed_at_sha": sha,
        "warnings": warnings,
    }
