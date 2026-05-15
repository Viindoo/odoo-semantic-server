# src/web_ui/routes/operations.py
"""Operations routes — long-running indexer commands (M8 W1 — pure JSON API)."""
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.indexer.version_presets import PRESETS

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/operations")

_VERSION_RE = re.compile(r"^\d{1,2}\.\d+$")


@router.get("/presets")
async def list_presets(request: Request):
    """Return available version presets."""
    return JSONResponse({"presets": PRESETS})


class IndexCoreBody(BaseModel):
    source: str
    version: str
    static_data_dir: str = ""


@router.post("/index-core")
async def post_index_core(body: IndexCoreBody, request: Request):
    """Validate inputs, spawn index-core subprocess, return job info."""
    # --- Validation ---
    error: str | None = None

    if not _VERSION_RE.match(body.version.strip()):
        error = f"Invalid version '{body.version}'. Expected format: 17.0 (up to 2-digit major)"
    elif not Path(body.source).is_dir():
        error = f"Source path does not exist or is not a directory: {body.source}"
    elif body.static_data_dir.strip() and not Path(body.static_data_dir.strip()).is_dir():
        error = (
            f"Static data dir does not exist or is not a directory: {body.static_data_dir}"
        )

    if error:
        return JSONResponse({"error": error}, status_code=400)

    # --- Spawn subprocess ---
    job_id: int | None = None
    try:
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        argv = ["index-core", "--source", body.source.strip(), "--version", body.version.strip()]
        if body.static_data_dir.strip():
            argv += ["--static-data-dir", body.static_data_dir.strip()]
        job_label = f"core:{body.version.strip()}"
        job_id = spawn_indexer_subcommand(argv, job_label=job_label)
    except Exception as exc:
        _logger.warning("index-core spawn failed: %s", exc)

    return JSONResponse({
        "ok": True,
        "job_id": job_id,
        "version": body.version.strip(),
        "message": (
            f"Indexing core specs for {body.version.strip()} (job {job_id})"
            if job_id is not None
            else f"Indexing core specs for {body.version.strip()} (job tracking unavailable)"
        ),
    })


class SeedPatternsBody(BaseModel):
    version: str = ""
    no_embed: str = ""
    force: str = ""
    patterns_file: str = ""


@router.post("/seed-patterns")
async def post_seed_patterns(body: SeedPatternsBody, request: Request):
    """Validate inputs, spawn seed-patterns subprocess, return job info."""
    # --- Validation ---
    error: str | None = None

    version_stripped = body.version.strip()
    patterns_file_stripped = body.patterns_file.strip()

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
        return JSONResponse({"error": error}, status_code=400)

    # --- Spawn subprocess ---
    job_id: int | None = None
    try:
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        argv = ["seed-patterns"]
        if version_stripped:
            argv += ["--version", version_stripped]
        if body.no_embed:
            argv.append("--no-embed")
        if body.force:
            argv.append("--force")
        if patterns_file_stripped:
            argv += ["--patterns-file", patterns_file_stripped]

        job_label = f"patterns:{version_stripped}" if version_stripped else "patterns"
        job_id = spawn_indexer_subcommand(argv, job_label=job_label)
    except Exception as exc:
        _logger.warning("seed-patterns spawn failed: %s", exc)

    label = f"patterns:{version_stripped}" if version_stripped else "patterns"
    return JSONResponse({
        "ok": True,
        "job_id": job_id,
        "message": (
            f"Seeding pattern catalogue ({label}) (job {job_id})"
            if job_id is not None
            else "Seeding pattern catalogue (job tracking unavailable)"
        ),
    })


class ApplyPresetBody(BaseModel):
    name: str
    repo_base_dir: str = ""
    repo_map_urls: list[str] = []
    repo_map_paths: list[str] = []
    dry_run: str = ""


@router.post("/apply-preset")
async def post_apply_preset(body: ApplyPresetBody, request: Request):
    """Validate inputs, run apply-preset synchronously, return result."""
    # --- Validation ---
    error: str | None = None

    _urls_raw = body.repo_map_urls or []
    _paths_raw = body.repo_map_paths or []

    if body.name not in PRESETS:
        available = ", ".join(sorted(PRESETS.keys()))
        error = f"Unknown preset {body.name!r}. Available: {available}"
    elif body.repo_base_dir.strip() and not Path(body.repo_base_dir.strip()).is_dir():
        error = (
            f"repo_base_dir does not exist or is not a directory: {body.repo_base_dir.strip()}"
        )
    elif len(_urls_raw) != len(_paths_raw):
        error = (
            "repo_map_urls and repo_map_paths must have the same"
            " number of entries"
        )

    if error:
        return JSONResponse({"error": error}, status_code=400)

    # --- Build argv ---
    argv = ["-m", "src.manager", "apply-preset", body.name]
    if body.repo_base_dir.strip():
        argv += ["--repo-base-dir", body.repo_base_dir.strip()]

    repo_map_pairs = [
        (u.strip(), p.strip())
        for u, p in zip(_urls_raw, _paths_raw)
        if u.strip() and p.strip()
    ]
    for url, path in repo_map_pairs:
        argv += ["--repo-map", f"{url}={path}"]

    if body.dry_run:
        argv.append("--dry-run")

    # --- Run synchronously (apply-preset is fast: ~seconds) ---
    # Timeout raised to 120s (was 60s) — a large profile with many repos needs
    # more time to register all repos in PostgreSQL on a loaded server.
    # Override via APPLY_PRESET_TIMEOUT env var if needed.
    _apply_preset_timeout = int(os.getenv("APPLY_PRESET_TIMEOUT", "120"))
    try:
        result = subprocess.run(
            [sys.executable, *argv],
            capture_output=True,
            text=True,
            timeout=_apply_preset_timeout,
        )
    except subprocess.TimeoutExpired:
        return JSONResponse(
            {"error": f"apply-preset timed out after {_apply_preset_timeout} seconds"},
            status_code=500,
        )

    if result.returncode != 0:
        stderr_text = result.stderr.strip() or result.stdout.strip() or "Unknown error"
        return JSONResponse(
            {"error": f"apply-preset failed (exit {result.returncode}): {stderr_text}"},
            status_code=400,
        )

    if body.dry_run:
        return JSONResponse({
            "ok": True,
            "dry_run": True,
            "preview": result.stdout,
            "preset": body.name,
            "repo_base_dir": body.repo_base_dir,
            "repo_map_pairs": repo_map_pairs,
        })

    return JSONResponse({"ok": True, "message": f"Preset '{body.name}' applied successfully"})
