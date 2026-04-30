"""Handler for the `resolve_view` MCP tool.

Matches ``docs/specs/resolve_view.md`` §3 envelope:

    {
      "result": {
        "xmlid": "...", "model": "...", "view_type": "...",
        "chain": [{"xmlid", "module", "priority", "mode"}, ...],
        "patch_log": [PatchLogEntry, ...],        # iff include_patch_log
        "final_xml": "<form>...</form>",          # iff include_final_xml
      },
      "indexed_at_sha": "<sha>",
      "warnings": [...]
    }

Lookup strategy:
    1. Find primary row by `xmlid` across tenant + public (UNION ALL).
    2. For each schema that carries a primary row, walk children via
       recursive CTE on `inherit_id` (FK is per-schema — cross-schema
       inheritance is not supported, see data-model/views.md invariants).
    3. Interleave tenant + public chains and sort by
       ``(priority ASC, load_order ASC, xmlid ASC)`` — mirrors the
       multi-tenancy overlay (tenant rows win on tie).
    4. Fetch `view_patches` per extension row, ordered by `ordinal`.
    5. Run :func:`osm.indexer.view_resolver.resolve_chain` over the sorted
       extension list against the primary `arch_xml`.

Tenant-private primaries (no public row with the same xmlid) resolve purely
from the tenant schema. Public-only primaries resolve across public and any
tenant-origin extensions that chose to inherit a public xmlid.
"""

from __future__ import annotations

from typing import Any

from psycopg import sql
from pydantic import BaseModel, Field

from osm.indexer.view_resolver import (
    PatchLogEntry,
    PatchRow,
    ViewRow,
    resolve_chain,
)
from osm.server.db import effective_indexed_at_sha
from osm.server.errors import InvalidInputError, NotFoundError, StaleIndexError
from osm.server.tenancy import TenantContext


class ResolveViewInput(BaseModel):
    xmlid: str = Field(..., min_length=1)
    include_final_xml: bool = True
    include_patch_log: bool = True


# ---------------------------------------------------------------------------
# Internal row types
# ---------------------------------------------------------------------------


class _ChainRow:
    """One row in the resolved chain.

    Not a dataclass — we pack DB query output into this for in-memory sorting
    without committing to a schema that future refactors must maintain.
    """

    __slots__ = (
        "schema",
        "id",
        "xmlid",
        "module",
        "model",
        "view_type",
        "mode",
        "priority",
        "load_order",
        "inherit_id",
        "arch_xml",
        "indexed_at_sha",
    )

    def __init__(
        self,
        *,
        schema: str,
        id: int,
        xmlid: str,
        module: str,
        model: str,
        view_type: str,
        mode: str,
        priority: int,
        load_order: int | None,
        inherit_id: int | None,
        arch_xml: bytes,
        indexed_at_sha: str,
    ) -> None:
        self.schema = schema
        self.id = id
        self.xmlid = xmlid
        self.module = module
        self.model = model
        self.view_type = view_type
        self.mode = mode
        self.priority = priority
        self.load_order = load_order
        self.inherit_id = inherit_id
        self.arch_xml = arch_xml
        self.indexed_at_sha = indexed_at_sha


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _fetch_chain_for_schema(
    cur: Any, schema: str, xmlid: str
) -> list[_ChainRow]:
    """Return every view row connected to the primary identified by ``xmlid``
    in ``schema`` — the primary itself + all recursive descendants via
    ``inherit_id``.

    Returns ``[]`` when no primary matches (caller decides 404 vs tenant-only).
    Cross-schema inheritance is intentionally not traversed here: the
    ``inherit_id`` FK is schema-local (see ``architecture/graph-store.md`` —
    no cross-schema hard FKs), so each schema's chain is self-contained.
    """
    # Depth counter caps recursion at 50 levels — protects against a
    # cyclic ``inherit_id`` (poisoned/buggy index) that would otherwise spin
    # the recursive CTE up to Postgres' max_recursive_iterations and DoS
    # every resolve_view call for that xmlid.
    # Schema is already ``validate_tenant``-checked upstream; composing via
    # ``sql.Identifier`` is structural defence-in-depth against future callers
    # that forget the validation.
    query = sql.SQL(
        """
    WITH RECURSIVE chain AS (
        SELECT v.id, v.xmlid, v.module_id, v.model, v.view_type, v.mode,
               v.priority, v.inherit_id, v.arch_xml, v.indexed_at_sha,
               0 AS depth
          FROM {schema}.views v
         WHERE v.xmlid = %s AND v.mode = 'primary'
        UNION ALL
        SELECT c.id, c.xmlid, c.module_id, c.model, c.view_type, c.mode,
               c.priority, c.inherit_id, c.arch_xml, c.indexed_at_sha,
               p.depth + 1
          FROM {schema}.views c
          JOIN chain p ON c.inherit_id = p.id
         WHERE p.depth < 50
    )
    SELECT chain.id, chain.xmlid, mod.name AS module_name, chain.model,
           chain.view_type, chain.mode, chain.priority, mod.load_order,
           chain.inherit_id, chain.arch_xml, chain.indexed_at_sha
      FROM chain
      JOIN {schema}.modules mod ON mod.id = chain.module_id
    """
    ).format(schema=sql.Identifier(schema))
    cur.execute(query, (xmlid,))
    rows: list[_ChainRow] = []
    for (
        vid, row_xmlid, module_name, model, view_type, mode, priority,
        load_order, inherit_id, arch_xml, indexed_at_sha,
    ) in cur.fetchall():
        rows.append(
            _ChainRow(
                schema=schema,
                id=int(vid),
                xmlid=row_xmlid,
                module=module_name,
                model=model,
                view_type=view_type,
                mode=mode,
                priority=int(priority),
                load_order=(int(load_order) if load_order is not None else None),
                inherit_id=(int(inherit_id) if inherit_id is not None else None),
                arch_xml=bytes(arch_xml) if arch_xml is not None else b"",
                indexed_at_sha=indexed_at_sha,
            )
        )
    return rows


def _fetch_patches_for_schema(
    cur: Any, schema: str, view_ids: list[int]
) -> dict[int, list[PatchRow]]:
    """Return ``{view_id: [PatchRow, ...]}`` ordered by ``ordinal`` for every
    extension row in ``view_ids`` within ``schema``. Empty input → empty dict.
    """
    if not view_ids:
        return {}
    query = sql.SQL(
        "SELECT view_id, ordinal, expr, position, content "
        "FROM {schema}.view_patches "
        "WHERE view_id = ANY(%s) ORDER BY view_id, ordinal"
    ).format(schema=sql.Identifier(schema))
    cur.execute(query, (view_ids,))
    out: dict[int, list[PatchRow]] = {}
    for view_id, ordinal, expr, position, content in cur.fetchall():
        out.setdefault(int(view_id), []).append(
            PatchRow(
                ordinal=int(ordinal),
                expr=expr,
                position=position,
                content=content,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_view(
    cur: Any,
    ctx: TenantContext,
    xmlid: str,
    *,
    include_final_xml: bool = True,
    include_patch_log: bool = True,
) -> dict[str, Any]:
    if not xmlid:
        raise InvalidInputError("xmlid must be non-empty")

    # Fetch chains per schema and collect patches.
    per_schema_rows: dict[str, list[_ChainRow]] = {}
    for schema in ctx.schemas:
        per_schema_rows[schema] = _fetch_chain_for_schema(cur, schema, xmlid)

    all_rows: list[_ChainRow] = []
    for rows in per_schema_rows.values():
        all_rows.extend(rows)

    if not all_rows:
        raise NotFoundError(f"view {xmlid!r} not in index")

    # Pick the primary row — tenant takes precedence over public when the same
    # xmlid exists in both (multi-tenant overlay).
    primary: _ChainRow | None = None
    for schema in reversed(ctx.schemas):  # tenant last in schemas → check last first
        for row in per_schema_rows.get(schema, []):
            if row.mode == "primary" and row.xmlid == xmlid:
                primary = row
                break
        if primary is not None:
            break

    if primary is None:
        # We found rows but no primary matches the xmlid — the xmlid resolved
        # only to extension rows. NotFound is the correct response: the caller
        # asked to resolve a *primary* and there is none.
        raise NotFoundError(f"view {xmlid!r} has no primary row in index")

    # Gather all extension rows (from every schema) plus the primary's siblings
    # that live in the same schemas. Deduplicate by ``(schema, id)`` — the
    # recursive CTE starts from every primary match per schema, which in the
    # tenant+public case pulls each schema's chain independently.
    extension_rows: list[_ChainRow] = []
    for row in all_rows:
        if row.mode == "extension":
            extension_rows.append(row)

    # Sort extensions per spec §4: (priority ASC, load_order ASC, xmlid ASC).
    # Unknown load_order pushed to the end.
    def _sort_key(row: _ChainRow) -> tuple[int, int, str]:
        lo = row.load_order if row.load_order is not None else 10**9
        return (row.priority, lo, row.xmlid)

    extension_rows.sort(key=_sort_key)

    # Fetch patches for the extension rows, grouped by schema.
    patches_by_schema_and_id: dict[tuple[str, int], list[PatchRow]] = {}
    by_schema: dict[str, list[int]] = {}
    for row in extension_rows:
        by_schema.setdefault(row.schema, []).append(row.id)
    for schema, ids in by_schema.items():
        for view_id, plist in _fetch_patches_for_schema(cur, schema, ids).items():
            patches_by_schema_and_id[(schema, view_id)] = plist

    # Build resolver input.
    resolver_input: list[tuple[ViewRow, list[PatchRow]]] = []
    for row in extension_rows:
        patches = patches_by_schema_and_id.get((row.schema, row.id), [])
        resolver_input.append((ViewRow(xmlid=row.xmlid), patches))

    # Staleness check — every row's indexed_at_sha must agree.
    shas = [primary.indexed_at_sha] + [r.indexed_at_sha for r in extension_rows]
    sha = effective_indexed_at_sha(shas)
    if sha is None:
        # Generic message — do not leak internal topology (cross-schema ref,
        # table names). Operators trace via handler logs + indexed_at_sha diff.
        raise StaleIndexError("index out of date; re-run indexer")

    resolved = resolve_chain(primary.arch_xml, resolver_input)

    # Build chain metadata in final (sorted) order: primary first, extensions
    # in resolver-application order. ``mode`` is redundant with position but
    # the spec envelope §3 lists it explicitly.
    chain_meta: list[dict[str, Any]] = [
        {
            "xmlid": primary.xmlid,
            "module": primary.module,
            "priority": primary.priority,
            "mode": "primary",
        }
    ]
    for row in extension_rows:
        chain_meta.append(
            {
                "xmlid": row.xmlid,
                "module": row.module,
                "priority": row.priority,
                "mode": "extension",
            }
        )

    warnings: list[str] = list(resolved.warnings)

    result: dict[str, Any] = {
        "xmlid": primary.xmlid,
        "model": primary.model,
        "view_type": primary.view_type,
        "chain": chain_meta,
    }
    if include_patch_log:
        result["patch_log"] = [_patch_log_as_dict(e) for e in resolved.patch_log]
    if include_final_xml:
        result["final_xml"] = resolved.final_xml.decode("utf-8")

    return {
        "result": result,
        "indexed_at_sha": sha,
        "warnings": warnings,
    }


def _patch_log_as_dict(entry: PatchLogEntry) -> dict[str, Any]:
    out: dict[str, Any] = {
        "from_xmlid": entry.from_xmlid,
        "ordinal": entry.ordinal,
        "expr": entry.expr,
        "position": entry.position,
        "applied": entry.applied,
    }
    if entry.reason is not None:
        out["reason"] = entry.reason
    return out


__all__ = ["ResolveViewInput", "resolve_view"]
