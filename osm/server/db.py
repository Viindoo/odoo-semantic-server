"""Lightweight DB helpers shared by all handlers.

Raw parameterised SQL against psycopg3. Schemas are inlined via format-string
interpolation (tenant names are validated in `tenancy.py` so this is
injection-safe) because identifiers cannot be passed as bound parameters.
"""

from __future__ import annotations

from typing import Any

from osm.server.tenancy import TenantContext


def union_all(
    select_body: str,
    ctx: TenantContext,
) -> str:
    """Render ``SELECT ... FROM {schema}.<tables> ...`` as a UNION ALL across
    every schema in ctx.schemas, wrapped in a subquery so the caller can
    ORDER BY output-column names regardless of the source-table aliases.

    ``select_body`` must contain one ``{schema}`` placeholder per schema-
    qualified table reference. Placeholder resolution happens per-schema.
    """
    parts = [select_body.format(schema=schema) for schema in ctx.schemas]
    if len(parts) == 1:
        return f"SELECT * FROM (\n{parts[0]}\n) AS osm_u"
    joined = "\nUNION ALL\n".join(parts)
    return f"SELECT * FROM (\n{joined}\n) AS osm_u"


def effective_indexed_at_sha(shas: list[str]) -> str | None:
    """Collapse a list of per-row shas into a single envelope sha.

    Returns the common sha when every row agrees, else ``None``. A ``None``
    result is the handler's cue to emit a 409 via StaleIndexError at the
    caller's discretion.
    """
    unique = {s for s in shas if s}
    if len(unique) == 1:
        return next(iter(unique))
    return None


def fetch_all(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    cur.execute(sql, params)
    return list(cur.fetchall())
