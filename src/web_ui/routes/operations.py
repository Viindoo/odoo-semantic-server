# src/web_ui/routes/operations.py
"""Operations page — long-running indexer commands with background job tracking."""
import logging
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

_logger = logging.getLogger(__name__)
router = APIRouter()

_VERSION_RE = re.compile(r"^\d{1,2}\.\d+$")


def _get_conn():
    import psycopg2

    from src import config

    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        return None
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception:
        return None


@router.get("/operations", response_class=HTMLResponse)
async def operations_page(request: Request):
    """Render operations shell page."""
    templates = request.app.state.templates
    flash = request.query_params.get("flash")
    return templates.TemplateResponse(
        request,
        "operations.html",
        {"flash": flash},
    )


@router.post("/operations/index-core", response_class=HTMLResponse)
async def post_index_core(
    request: Request,
    source: Annotated[str, Form()],
    version: Annotated[str, Form()],
    static_data_dir: Annotated[str, Form()] = "",
):
    """Validate inputs, spawn index-core subprocess, redirect with flash."""
    templates = request.app.state.templates

    # --- Validation ---
    error: str | None = None

    if not _VERSION_RE.match(version.strip()):
        error = f"Invalid version '{version}'. Expected format: 17.0 (up to 2-digit major)"
    elif not Path(source).is_dir():
        error = f"Source path does not exist or is not a directory: {source}"
    elif static_data_dir.strip() and not Path(static_data_dir.strip()).is_dir():
        error = (
            f"Static data dir does not exist or is not a directory: {static_data_dir}"
        )

    if error:
        return templates.TemplateResponse(
            request,
            "operations.html",
            {"flash": None, "index_core_error": error},
            status_code=400,
        )

    # --- Spawn subprocess ---
    conn = _get_conn()
    job_id: int | None = None
    if conn:
        try:
            from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

            argv = ["index-core", "--source", source.strip(), "--version", version.strip()]
            if static_data_dir.strip():
                argv += ["--static-data-dir", static_data_dir.strip()]
            job_label = f"core:{version.strip()}"
            job_id = spawn_indexer_subcommand(conn, argv, job_label=job_label)
        except Exception as exc:
            _logger.warning("index-core spawn failed: %s", exc)
        finally:
            conn.close()

    if job_id is not None:
        flash = quote_plus(
            f"Indexing core specs for {version.strip()} (job {job_id})"
        )
    else:
        flash = quote_plus(
            f"Indexing core specs for {version.strip()} (job tracking unavailable)"
        )

    return RedirectResponse(f"/operations?flash={flash}", status_code=303)


@router.post("/operations/seed-patterns", response_class=HTMLResponse)
async def post_seed_patterns(
    request: Request,
    version: Annotated[str, Form()] = "",
    no_embed: Annotated[str, Form()] = "",
    force: Annotated[str, Form()] = "",
    patterns_file: Annotated[str, Form()] = "",
):
    """Validate inputs, spawn seed-patterns subprocess, redirect with flash."""
    templates = request.app.state.templates

    # --- Validation ---
    error: str | None = None

    version_stripped = version.strip()
    patterns_file_stripped = patterns_file.strip()

    if version_stripped and not _VERSION_RE.match(version_stripped):
        error = (
            f"Invalid version '{version_stripped}'. "
            "Expected format: 17.0 (up to 2-digit major)"
        )
    elif patterns_file_stripped and not Path(patterns_file_stripped).is_file():
        error = (
            f"Patterns file does not exist or is not a file: {patterns_file_stripped}"
        )

    if error:
        return templates.TemplateResponse(
            request,
            "operations.html",
            {"flash": None, "seed_patterns_error": error},
            status_code=400,
        )

    # --- Spawn subprocess ---
    conn = _get_conn()
    job_id: int | None = None
    if conn:
        try:
            from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

            argv = ["seed-patterns"]
            if version_stripped:
                argv += ["--version", version_stripped]
            if no_embed:
                argv.append("--no-embed")
            if force:
                argv.append("--force")
            if patterns_file_stripped:
                argv += ["--patterns-file", patterns_file_stripped]

            job_label = f"patterns:{version_stripped}" if version_stripped else "patterns"
            job_id = spawn_indexer_subcommand(conn, argv, job_label=job_label)
        except Exception as exc:
            _logger.warning("seed-patterns spawn failed: %s", exc)
        finally:
            conn.close()

    if job_id is not None:
        label = f"patterns:{version_stripped}" if version_stripped else "patterns"
        flash = quote_plus(f"Seeding pattern catalogue ({label}) (job {job_id})")
    else:
        flash = quote_plus("Seeding pattern catalogue (job tracking unavailable)")

    return RedirectResponse(f"/operations?flash={flash}", status_code=303)
