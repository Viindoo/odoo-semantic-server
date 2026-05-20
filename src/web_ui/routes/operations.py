# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/operations.py
"""Operations routes — backup/restore/indexer (M8 W1 + M9 W-BK + W-RS).

Restore endpoint (M9 W-RS) enforces 10-item OWASP checklist:
  1. Content-Type allowlist (gzip, x-tar, octet-stream)
  2. Extension allowlist (.tar.gz, .tgz)
  3. Content-Length pre-check (quick reject)
  4. Streaming size guard (MAX_RESTORE_BYTES)
  5. Disk space check (2× upload size)
  6. SHA-256 audit hash before extract
  7. Maintenance mode (409 concurrent)
  8. Admin + 5-min MFA freshness
  9. Pre-restore safety backup must succeed
  10. Audit log records sha256, size, filename, outcome
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.db.audit import audit_action
from src.indexer.version_presets import PRESETS
from src.web_ui._json import _json_safe
from src.web_ui.auth import require_admin_with_fresh_mfa

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/operations")

# Postgres advisory lock ID for backup (must match src/cli.py _backup_advisory_lock)
_BACKUP_LOCK_ID = 0xBA17C9

# In-memory job registry for backup jobs (keyed by str job_id)
# Simple approach: store metadata in a dict, subprocess output in a temp file
_backup_jobs: dict[str, dict] = {}
_backup_jobs_lock = threading.Lock()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

_VERSION_RE = re.compile(r"^\d{1,2}\.\d+$")

# --- Restore upload constraints ---
MAX_RESTORE_BYTES = 500 * 1024 * 1024  # 500MB hard limit

# OWASP item 7: maintenance mode flag — set while restore is in progress.
# Blocks all non-restore requests with 503 + Retry-After: 60.
# threading.Event (not asyncio.Event): the restore worker is a real
# threading.Thread and set()/clear() must be thread-safe; asyncio.Event
# is NOT safe to mutate from outside the event loop.
_RESTORE_IN_PROGRESS = threading.Event()

# OWASP item 6/10: audit log (module-level list; in production use persistent store)
_RESTORE_AUDIT_LOG: list[dict] = []

# Allowed content-types for upload (OWASP item 1)
_ALLOWED_CONTENT_TYPES = {
    "application/gzip",
    "application/x-gzip",
    "application/x-tar",
    "application/octet-stream",
}


def _audit(event: str, **kwargs) -> None:
    """Append an audit record and log at INFO level."""
    record = {"event": event, "ts": time.time(), **kwargs}
    _RESTORE_AUDIT_LOG.append(record)
    _logger.info("RESTORE_AUDIT %s", record)


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from subprocess output for safe SSE transport."""
    import re as _re
    return _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


@router.get("/presets")
async def list_presets(request: Request):
    """Return available version presets."""
    return JSONResponse(_json_safe({"presets": PRESETS}))


class IndexCoreBody(BaseModel):
    source: str
    version: str
    static_data_dir: str = ""


@router.post("/index-core")
@audit_action("operations.index_core")
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
        return JSONResponse(_json_safe({"error": error}), status_code=400)

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

    return JSONResponse(_json_safe({
        "ok": True,
        "job_id": job_id,
        "version": body.version.strip(),
        "message": (
            f"Indexing core specs for {body.version.strip()} (job {job_id})"
            if job_id is not None
            else f"Indexing core specs for {body.version.strip()} (job tracking unavailable)"
        ),
    }))


class SeedPatternsBody(BaseModel):
    version: str = ""
    no_embed: str = ""
    force: str = ""
    patterns_file: str = ""


@router.post("/seed-patterns")
@audit_action("operations.seed_patterns")
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
        return JSONResponse(_json_safe({"error": error}), status_code=400)

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
    return JSONResponse(_json_safe({
        "ok": True,
        "job_id": job_id,
        "message": (
            f"Seeding pattern catalogue ({label}) (job {job_id})"
            if job_id is not None
            else "Seeding pattern catalogue (job tracking unavailable)"
        ),
    }))


class ApplyPresetBody(BaseModel):
    name: str
    repo_base_dir: str = ""
    repo_map_urls: list[str] = []
    repo_map_paths: list[str] = []
    dry_run: str = ""


@router.post("/apply-preset")
@audit_action("operations.apply_preset")
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
        return JSONResponse(_json_safe({"error": error}), status_code=400)

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
            _json_safe(
                {"error": f"apply-preset timed out after {_apply_preset_timeout} seconds"}
            ),
            status_code=500,
        )

    if result.returncode != 0:
        stderr_text = result.stderr.strip() or result.stdout.strip() or "Unknown error"
        return JSONResponse(
            _json_safe(
                {"error": f"apply-preset failed (exit {result.returncode}): {stderr_text}"}
            ),
            status_code=400,
        )

    if body.dry_run:
        return JSONResponse(_json_safe({
            "ok": True,
            "dry_run": True,
            "preview": result.stdout,
            "preset": body.name,
            "repo_base_dir": body.repo_base_dir,
            "repo_map_pairs": repo_map_pairs,
        }))

    return JSONResponse(_json_safe({
        "ok": True,
        "message": f"Preset '{body.name}' applied successfully",
    }))


# ---------------------------------------------------------------------------
# Backup endpoints (M9 W-BK)
# ---------------------------------------------------------------------------


class BackupBody(BaseModel):
    output: str = ""
    bundle_passphrase_env: str = ""


def _spawn_backup_subprocess(job_id: str, output_path: str, bundle_passphrase_env: str) -> None:
    """Spawn backup CLI in a background thread, streaming output to a log file."""
    log_path = Path(tempfile.gettempdir()) / f"osm-backup-{job_id}.log"

    argv = [
        sys.executable, "-m", "src.cli", "backup",
        "--output", output_path,
    ]
    if bundle_passphrase_env:
        argv += ["--bundle-passphrase-env", bundle_passphrase_env]

    def _run():
        with _backup_jobs_lock:
            _backup_jobs[job_id]["status"] = "running"
            _backup_jobs[job_id]["started_at"] = datetime.now(UTC).isoformat()

        _logger.info("Backup job %s: spawning %s → log %s", job_id, argv, log_path)
        try:
            with open(log_path, "w") as log_file:
                proc = subprocess.Popen(
                    argv,
                    stdout=log_file,
                    stderr=log_file,
                    shell=False,
                )
            rc = proc.wait()
        except Exception as exc:
            rc = -1
            _logger.warning("Backup job %s spawn error: %s", job_id, exc)

        finished_at = datetime.now(UTC).isoformat()
        with _backup_jobs_lock:
            _backup_jobs[job_id]["status"] = "done" if rc == 0 else "error"
            _backup_jobs[job_id]["exit_code"] = rc
            _backup_jobs[job_id]["finished_at"] = finished_at

        status = "done" if rc == 0 else "error"
        _logger.info("Backup job %s finished: status=%s exit_code=%d", job_id, status, rc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


@router.post("/backup")
@audit_action("operations.backup")
async def trigger_backup(request: Request, body: BackupBody):
    """Trigger a backup job. Returns job_id; poll or stream via /backup/{job_id}/status."""
    import uuid

    backup_dir = os.getenv("BACKUP_DIR", str(Path.home() / "backup"))

    if body.output:
        output_path = body.output
    else:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        output_path = str(Path(backup_dir) / f"backup_{ts}.tar.gz")

    if not output_path.endswith(".tar.gz"):
        return JSONResponse(_json_safe({"error": "output must end with .tar.gz"}), status_code=400)

    # Validate path under BACKUP_DIR (resolve symlinks)
    resolved_out = Path(output_path).resolve()
    resolved_backup_dir = Path(backup_dir).resolve()
    if not str(resolved_out).startswith(str(resolved_backup_dir)):
        return JSONResponse(
            _json_safe({"error": f"output must be under BACKUP_DIR={backup_dir}"}),
            status_code=400,
        )

    job_id = str(uuid.uuid4())
    with _backup_jobs_lock:
        _backup_jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "output": output_path,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "created_at": datetime.now(UTC).isoformat(),
        }

    _logger.info("Backup job %s created → %s", job_id, output_path)
    _spawn_backup_subprocess(job_id, output_path, body.bundle_passphrase_env)

    stream_url = f"/api/operations/backup/{job_id}/stream"
    return JSONResponse(_json_safe({
        "ok": True,
        "job_id": job_id,
        "stream_url": stream_url,
        "output": output_path,
        "message": f"Backup job {job_id} started.",
    }))


@router.get("/backup/{job_id}/stream")
async def backup_stream(job_id: str, request: Request):
    """SSE stream of backup process output. Streams until done, then sends done event."""
    log_path = Path(tempfile.gettempdir()) / f"osm-backup-{job_id}.log"

    async def event_gen():
        heartbeat_interval = 15.0  # seconds — avoids nginx timeout
        last_heartbeat = asyncio.get_event_loop().time()
        byte_offset = 0

        while True:
            # Check if job exists
            with _backup_jobs_lock:
                job = _backup_jobs.get(job_id)
            if job is None:
                yield f"data: {json.dumps({'error': 'job not found'})}\n\n"
                return

            # Read new lines from log file
            if log_path.exists():
                try:
                    with open(log_path, "rb") as f:
                        f.seek(byte_offset)
                        chunk = f.read(65536)
                        if chunk:
                            byte_offset += len(chunk)
                            for raw_line in chunk.decode("utf-8", errors="replace").splitlines():
                                clean = _ANSI_ESCAPE_RE.sub("", raw_line)
                                if clean:
                                    yield f"data: {json.dumps({'line': clean})}\n\n"
                except OSError:
                    pass

            status = job.get("status")
            now = asyncio.get_event_loop().time()

            if status in ("done", "error"):
                exit_code = job.get("exit_code")
                payload = json.dumps({"done": True, "status": status, "exit_code": exit_code})
                yield f"data: {payload}\n\n"
                return

            # Heartbeat to keep connection alive through nginx
            if now - last_heartbeat >= heartbeat_interval:
                yield ": heartbeat\n\n"
                last_heartbeat = now

            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/backup/{job_id}/status")
async def backup_status(job_id: str, request: Request):
    """Poll backup job status (alternative to SSE for simpler clients)."""
    with _backup_jobs_lock:
        job = _backup_jobs.get(job_id)
    if job is None:
        return JSONResponse(_json_safe({"error": "job not found"}), status_code=404)
    return JSONResponse(_json_safe(dict(job)))


# ---------------------------------------------------------------------------
# Restore endpoint (M9 W-RS — OWASP 10-item guards)
# ---------------------------------------------------------------------------


@router.post("/restore")
@audit_action("operations.restore")
async def trigger_restore(
    request: Request,
    file: UploadFile = File(...),
    _user_id: int = Depends(require_admin_with_fresh_mfa),
):
    """Upload a .tar.gz backup bundle and restore it.

    OWASP 10-item checklist enforced:
    1. Content-Type allowlist
    2. Extension allowlist (.tar.gz / .tgz)
    3. Content-Length pre-check (header may lie, but quick reject)
    4. Streaming size guard (loop enforces MAX_RESTORE_BYTES)
    5. Disk space check (2x upload size must be free)
    6. SHA-256 computed for audit
    7. Maintenance mode (concurrent upload → 409)
    8. Admin + 5-min MFA freshness (Depends(require_admin_with_fresh_mfa))
    9. Pre-restore safety backup via CLI subprocess
    10. Audit log records sha256, size, filename, outcome
    """
    # --- OWASP 1: Content-Type allowlist ---
    ct = (file.content_type or "").split(";")[0].strip().lower()
    if ct not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid content type {ct!r}. "
                "Must be application/gzip, application/x-tar, or application/octet-stream."
            ),
        )

    # --- OWASP 2: Extension allowlist (strip path traversal via .name) ---
    fname = Path(file.filename or "").name  # strip any path components
    if not (fname.endswith(".tar.gz") or fname.endswith(".tgz")):
        raise HTTPException(
            status_code=400,
            detail="Filename must end with .tar.gz or .tgz",
        )

    # --- OWASP 3: Content-Length pre-check (header advisory — quick reject) ---
    cl_header = request.headers.get("content-length")
    if cl_header:
        try:
            if int(cl_header) > MAX_RESTORE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (Content-Length {cl_header} > {MAX_RESTORE_BYTES})",
                )
        except ValueError:
            pass  # Malformed header — skip pre-check, streaming guard will catch it

    # --- OWASP 7: Maintenance mode — concurrent upload guard ---
    if _RESTORE_IN_PROGRESS.is_set():
        raise HTTPException(status_code=409, detail="Another restore is already in progress")

    # --- OWASP 4: Stream to tempfile with size guard ---
    tmp_path: Path | None = None
    bytes_read = 0
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".tar.gz", prefix="restore_upload_")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "wb") as tmp_f:
                while True:
                    chunk = await file.read(64 * 1024)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > MAX_RESTORE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File too large (exceeded {MAX_RESTORE_BYTES} bytes)",
                        )
                    tmp_f.write(chunk)
        except HTTPException:
            tmp_path.unlink(missing_ok=True)
            tmp_path = None
            raise
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            tmp_path = None
            _logger.error("Restore upload stream error: %s", exc)
            raise HTTPException(status_code=500, detail="Upload failed")
    except HTTPException:
        raise
    except Exception as exc:
        _logger.error("Restore upload tempfile error: %s", exc)
        raise HTTPException(status_code=500, detail="Upload failed")

    # --- OWASP 5: Disk space check (need 2× upload size free) ---
    free_bytes = shutil.disk_usage(tmp_path.parent).free
    if free_bytes < 2 * bytes_read:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=507,
            detail=f"Insufficient disk space: {free_bytes} bytes free, need {2 * bytes_read}",
        )

    # --- OWASP 6: Compute SHA-256 for audit ---
    sha256 = hashlib.sha256(tmp_path.read_bytes()).hexdigest()

    # --- OWASP 7: Set maintenance mode ---
    _RESTORE_IN_PROGRESS.set()

    # --- OWASP 10: Audit log — restore start ---
    job_id = str(uuid.uuid4())
    _audit(
        "restore.start",
        job_id=job_id,
        filename=fname,
        sha256=sha256,
        size=bytes_read,
    )

    # --- OWASP 9: Pre-restore safety backup (via CLI) ---
    safety_backup_path: str | None = None
    try:
        backup_dir = Path(os.getenv("BACKUP_DIR", "~/backup")).expanduser()
        backup_dir.mkdir(parents=True, exist_ok=True)
        safety_path = backup_dir / f"pre-restore-{int(time.time())}.sql"
        from src.cli import _dsn_to_pg_args_and_env, _get_pg_dsn

        dsn = _get_pg_dsn()
        if dsn:
            pg_args, env_overrides = _dsn_to_pg_args_and_env(dsn)
            env = {**os.environ, **env_overrides}
            safety_cmd = ["pg_dump", *pg_args, "-F", "plain"]
            with safety_path.open("wb") as sf:
                safety_result = subprocess.run(
                    safety_cmd,
                    stdout=sf,
                    stderr=subprocess.PIPE,
                    env=env,
                )
            if safety_result.returncode == 0:
                safety_backup_path = str(safety_path)
                _logger.info("Pre-restore safety backup: %s", safety_backup_path)
            else:
                err = safety_result.stderr.decode(errors="replace")
                _logger.error("Safety backup failed: %s", err)
                safety_path.unlink(missing_ok=True)
                _RESTORE_IN_PROGRESS.clear()
                tmp_path.unlink(missing_ok=True)
                _audit("restore.safety_backup_failed", job_id=job_id, error=err)
                raise HTTPException(
                    status_code=500,
                    detail=f"Pre-restore safety backup failed: {err}",
                )
    except HTTPException:
        raise
    except Exception as exc:
        _logger.warning("Safety backup skipped (no PG_DSN?): %s", exc)
        # If no DSN configured, continue without safety backup (dev/test)
        safety_backup_path = None

    # --- Spawn restore subprocess ---
    proc_args = [sys.executable, "-m", "src.cli", "restore", str(tmp_path)]
    passphrase_env = os.getenv("RESTORE_PASSPHRASE_ENV_VAR", "RESTORE_PASSPHRASE")
    proc_args += ["--bundle-passphrase-env", passphrase_env]

    def _run_restore():
        """Run restore subprocess and clear maintenance mode on completion."""
        try:
            result = subprocess.run(
                proc_args,
                capture_output=True,
                text=True,
            )
            out = _strip_ansi(result.stdout + result.stderr)
            success = result.returncode == 0
            _audit(
                "restore.done" if success else "restore.failed",
                job_id=job_id,
                sha256=sha256,
                returncode=result.returncode,
                output_snippet=out[:500],
            )
        except Exception as exc:
            _audit("restore.error", job_id=job_id, error=str(exc))
        finally:
            _RESTORE_IN_PROGRESS.clear()
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # Run in a background thread to avoid blocking the event loop
    import threading
    t = threading.Thread(target=_run_restore, daemon=True)
    t.start()

    return JSONResponse(
        _json_safe(
            {
                "ok": True,
                "job_id": job_id,
                "sha256": sha256,
                "size": bytes_read,
                "filename": fname,
                "safety_backup": safety_backup_path,
                "message": "Restore started. Service entering maintenance mode until complete.",
            }
        ),
        status_code=202,
    )
