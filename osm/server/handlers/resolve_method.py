"""Handler for the `resolve_method` MCP tool.

Matches the golden shape in `tests/fixtures/golden/resolve_method.json`:

    {
      "model_name": "...",
      "method_name": "...",
      "chain": [{"module", "file", "signature", "decorators", "calls_super",
                 "is_override": bool}, ...],
      "chain_is_broken": bool,
      "warnings": [...]
    }

Chain order is load-order (earliest first) per `docs/specs/resolve_method.md` §5b
Step 4. Note: the spec stores the chain linearly and derives C3 MRO at query
time; the WP-6 write-back collapses within-module duplicates to a single DB
row so what we return here is already the cross-module sequence.

`chain_is_broken` is true when any non-root override has calls_super=False.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from osm.server.db import effective_indexed_at_sha, union_all
from osm.server.errors import InvalidInputError, NotFoundError, StaleIndexError
from osm.server.tenancy import TenantContext


class ResolveMethodInput(BaseModel):
    model_name: str = Field(..., min_length=1)
    method_name: str = Field(..., min_length=1)
    include_source_snippets: bool = True


def resolve_method(
    cur: Any,
    ctx: TenantContext,
    model_name: str,
    method_name: str,
    *,
    include_source_snippets: bool = True,
) -> dict[str, Any]:
    if not model_name:
        raise InvalidInputError("model_name must be non-empty")
    if not method_name:
        raise InvalidInputError("method_name must be non-empty")
    _ = include_source_snippets  # snippet attach deferred to P2 for cleaner handler

    sql = union_all(
        """
        SELECT me.id, mod.name AS module_name, me.file_path, me.signature,
               me.decorators, me.calls_super, me.start_line, me.end_line,
               me.indexed_at_sha, mod.load_order, m.indexer_notes
          FROM {schema}.methods me
          JOIN {schema}.models m ON m.id = me.model_id
          JOIN {schema}.modules mod ON mod.id = m.module_id
         WHERE m.name = %s AND me.method_name = %s
        """,
        ctx,
    ) + "\nORDER BY osm_u.load_order ASC, osm_u.module_name ASC, osm_u.id ASC"

    # INVARIANT: 2 placeholders per SELECT block (model_name, method_name);
    # must match len(ctx.schemas) multiplication below. Adding a 3rd `%s`
    # requires updating this list too.
    params = tuple([model_name, method_name] * len(ctx.schemas))
    cur.execute(sql, params)
    rows = list(cur.fetchall())

    if not rows:
        raise NotFoundError(
            f"method {method_name!r} on model {model_name!r} not in index"
        )

    chain: list[dict[str, Any]] = []
    shas: list[str] = []
    warnings: list[str] = []
    chain_is_broken = False

    for idx, (
        _mid, module_name, file_path, signature, decorators, calls_super,
        _start, _end, indexed_at_sha, _load_order, indexer_notes,
    ) in enumerate(rows):
        entry = {
            "module": module_name,
            "file": file_path,
            "signature": signature,
            "decorators": list(decorators or []),
            "calls_super": bool(calls_super),
            "is_override": idx > 0,
        }
        chain.append(entry)
        shas.append(indexed_at_sha)
        if idx > 0 and not calls_super:
            chain_is_broken = True
            warnings.append(
                f"chain_is_broken: {module_name} does not call super()"
            )
        if indexer_notes and indexer_notes.get("conditional_import"):
            warnings.append(
                f"{module_name}: conditional_import on {model_name}.{method_name}"
            )

    sha = effective_indexed_at_sha(shas)
    if sha is None:
        raise StaleIndexError("stale_cross_schema_ref on methods rows")

    return {
        "result": {
            "model_name": model_name,
            "method_name": method_name,
            "chain": chain,
            "chain_is_broken": chain_is_broken,
            "warnings": warnings,
        },
        "indexed_at_sha": sha,
        "warnings": warnings,
    }
