"""FastMCP application exposing `resolve_model`, `resolve_field`, `resolve_method`.

Run over stdio:

    python -m osm.server.app

Run over streamable-http:

    python -m osm.server.app --http --port 8000

Environment:
    DATABASE_URL   Postgres connection (required at serve time)
    OSM_TENANT     Tenant schema (default: public)

Handlers are pure functions in `osm.server.handlers.*` — the MCP wrapper here
only owns transport, tenant context, and DB connection lifetime.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from osm.server.errors import HandlerError
from osm.server.handlers.resolve_field import resolve_field as _resolve_field
from osm.server.handlers.resolve_method import resolve_method as _resolve_method
from osm.server.handlers.resolve_model import resolve_model as _resolve_model
from osm.server.tenancy import TenantContext, context_from_env

_logger = logging.getLogger(__name__)


@dataclass
class AppState:
    database_url: str
    tenant_ctx: TenantContext


@asynccontextmanager
async def _lifespan(app: FastMCP[AppState]) -> AsyncIterator[AppState]:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set; the MCP server requires Postgres")
    tenant_ctx = context_from_env()
    _logger.info(
        "osm-mcp starting tenant=%s schemas=%s", tenant_ctx.tenant, tenant_ctx.schemas
    )
    yield AppState(database_url=db_url, tenant_ctx=tenant_ctx)


def build_app() -> FastMCP[AppState]:
    app: FastMCP[AppState] = FastMCP(
        name="osm-mcp",
        instructions=(
            "Resolve Odoo model / field / method chains against the osm index. "
            "Every response includes indexed_at_sha; a 409-style error surfaces "
            "when cross-schema rows disagree on the effective sha."
        ),
        lifespan=_lifespan,
    )

    @app.tool(description="Resolve a model name to its inheritance chain, "
              "delegated models, and defining module.")
    def resolve_model(
        ctx: Context[Any, AppState, Any],
        model_name: str,
        include_field_summary: bool = True,
        include_method_summary: bool = False,
    ) -> dict[str, Any]:
        state = ctx.request_context.lifespan_context
        with _open_cursor(state) as cur:
            try:
                return _resolve_model(
                    cur,
                    state.tenant_ctx,
                    model_name,
                    include_field_summary=include_field_summary,
                    include_method_summary=include_method_summary,
                )
            except HandlerError as exc:
                return _err_envelope(exc)

    @app.tool(description="Resolve a field on a model to its full override "
              "chain and effective definition.")
    def resolve_field(
        ctx: Context[Any, AppState, Any],
        model_name: str,
        field_name: str,
        include_source_snippets: bool = False,
    ) -> dict[str, Any]:
        state = ctx.request_context.lifespan_context
        with _open_cursor(state) as cur:
            try:
                return _resolve_field(
                    cur,
                    state.tenant_ctx,
                    model_name,
                    field_name,
                    include_source_snippets=include_source_snippets,
                )
            except HandlerError as exc:
                return _err_envelope(exc)

    @app.tool(description="Resolve a method override chain on a model, "
              "including super() usage per step.")
    def resolve_method(
        ctx: Context[Any, AppState, Any],
        model_name: str,
        method_name: str,
        include_source_snippets: bool = True,
    ) -> dict[str, Any]:
        state = ctx.request_context.lifespan_context
        with _open_cursor(state) as cur:
            try:
                return _resolve_method(
                    cur,
                    state.tenant_ctx,
                    model_name,
                    method_name,
                    include_source_snippets=include_source_snippets,
                )
            except HandlerError as exc:
                return _err_envelope(exc)

    return app


class _Cursor:
    def __init__(self, state: AppState) -> None:
        import psycopg
        self._conn = psycopg.connect(state.database_url)
        self._cur = self._conn.cursor()

    def __enter__(self) -> Any:
        return self._cur

    def __exit__(self, *_exc: object) -> None:
        try:
            self._cur.close()
        finally:
            self._conn.close()


def _open_cursor(state: AppState) -> _Cursor:
    return _Cursor(state)


def _err_envelope(exc: HandlerError) -> dict[str, Any]:
    """Package a handler error as the standard envelope so MCP clients see a
    structured failure instead of a raw exception."""
    return {
        "result": None,
        "indexed_at_sha": None,
        "warnings": [str(exc)],
        "error": {
            "status_code": exc.status_code,
            "message": str(exc),
            "type": type(exc).__name__,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over streamable-http instead of stdio.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="HOST",
        help=(
            "Extra Host header value permitted by DNS-rebinding protection. "
            "Repeatable. Port wildcards are added automatically (HOST:*). "
            "Also accepts OSM_ALLOWED_HOSTS env as comma-separated list."
        ),
    )
    args = parser.parse_args(argv)

    app = build_app()
    if args.http:
        app.settings.host = args.host
        app.settings.port = args.port

        extras = list(args.allowed_host)
        env_extras = os.environ.get("OSM_ALLOWED_HOSTS", "").strip()
        if env_extras:
            extras += [h.strip() for h in env_extras.split(",") if h.strip()]

        if args.host not in ("127.0.0.1", "localhost", "::1") or extras:
            from mcp.server.transport_security import TransportSecuritySettings

            bind_hosts = [args.host] if args.host not in ("0.0.0.0", "::") else []
            host_list = ["127.0.0.1", "localhost", "[::1]", *bind_hosts, *extras]
            seen: set[str] = set()
            dedup = [h for h in host_list if not (h in seen or seen.add(h))]
            app.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[f"{h}:*" for h in dedup],
                allowed_origins=[f"http://{h}:*" for h in dedup]
                + [f"https://{h}:*" for h in dedup],
            )

        app.run(transport="streamable-http")
    else:
        app.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
