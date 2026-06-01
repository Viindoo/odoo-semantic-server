# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared diagnostics logic — used by both CLI (`src/cli.py`) and the API endpoint.

SSOT: all check logic lives here. CLI and the HTTP endpoint both call
``run_diagnostics()`` and format the result for their respective output.
"""
from __future__ import annotations

import json as _json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


def _is_pg_container_running() -> bool | None:
    """Return True/False/None for PG container state.

    Imported from src.cli to avoid reimplementing — kept as a thin wrapper
    so callers don't need to know about cli.py internals.
    """
    from src.cli import _is_pg_container_running as _impl
    return _impl()


def _diagnose_initdb_dir() -> Path:
    """Resolve docker/initdb.d relative to the repo root (not runtime cwd)."""
    from src.cli import _diagnose_initdb_dir as _impl
    return _impl()


def run_diagnostics() -> dict:
    """Run cross-tier health checks and return a structured result dict.

    Returns:
        {
          "checks": [
            {"name": str, "status": "ok"|"error"|"skipped", "detail": str},
            ...
          ],
          "overall": "ok"|"degraded"
        }

    All checks are best-effort: exceptions are caught and recorded as errors.
    This function NEVER raises.

    Checks performed:
      1. pg_container_running  — docker inspect State.Running for PG container.
      2. neo4j_container_healthy — docker inspect State.Health.Status for Neo4j.
      3. mcp_health             — HTTP GET MCP_HEALTH_URL /health → status=ok.
      4. compose_initdb_mount   — docker/initdb.d is a real directory (regression
                                  guard for May-2026 file-vs-dir incident).
    """
    checks: list[dict] = []

    # Check 1: PG container running
    pg_container = os.getenv("POSTGRES_CONTAINER", "odoo-semantic-mcp-postgres-1")
    try:
        pg_running = _is_pg_container_running()
        if pg_running is None:
            checks.append({
                "name": "pg_container_running",
                "status": "skipped",
                "detail": "docker not available or container unknown",
            })
        elif pg_running:
            checks.append({
                "name": "pg_container_running",
                "status": "ok",
                "detail": pg_container,
            })
        else:
            checks.append({
                "name": "pg_container_running",
                "status": "error",
                "detail": f"{pg_container} not running",
            })
    except Exception as exc:
        checks.append({
            "name": "pg_container_running",
            "status": "error",
            "detail": f"check failed: {exc}",
        })

    # Check 2: Neo4j container healthy
    neo4j_container = os.getenv("NEO4J_CONTAINER", "odoo-semantic-mcp-neo4j-1")
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", neo4j_container],
            capture_output=True, text=True, shell=False,
        )
        if r.returncode != 0:
            checks.append({
                "name": "neo4j_container_healthy",
                "status": "skipped",
                "detail": f"{neo4j_container} not found",
            })
        elif r.stdout.strip() == "healthy":
            checks.append({
                "name": "neo4j_container_healthy",
                "status": "ok",
                "detail": neo4j_container,
            })
        else:
            checks.append({
                "name": "neo4j_container_healthy",
                "status": "error",
                "detail": f"{neo4j_container} state={r.stdout.strip() or 'unknown'}",
            })
    except FileNotFoundError:
        checks.append({
            "name": "neo4j_container_healthy",
            "status": "skipped",
            "detail": "docker not in PATH",
        })
    except Exception as exc:
        checks.append({
            "name": "neo4j_container_healthy",
            "status": "error",
            "detail": f"check failed: {exc}",
        })

    # Check 3: MCP /health endpoint reachable
    from src.constants import MCP_HEALTH_PROBE_TIMEOUT_SECONDS
    mcp_url = os.getenv("MCP_HEALTH_URL", "http://127.0.0.1:8002/health")
    try:
        with urllib.request.urlopen(
            mcp_url, timeout=MCP_HEALTH_PROBE_TIMEOUT_SECONDS,
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = _json.loads(body)
                health_status = parsed.get("status", "unknown")
            except _json.JSONDecodeError:
                health_status = "unparseable"
            # /health is now a pure liveness probe returning status="alive"
            # (changed in ADR-0046 / PR #227).  Accept both "alive" (new) and
            # "ok" (legacy servers) so diagnostics stays correct across versions.
            if resp.status == 200 and health_status in ("ok", "alive"):
                checks.append({
                    "name": "mcp_health",
                    "status": "ok",
                    "detail": f"HTTP {resp.status} status={health_status} (liveness ok)",
                })
            else:
                checks.append({
                    "name": "mcp_health",
                    "status": "error",
                    "detail": f"HTTP {resp.status} status={health_status} (expected 'alive')",
                })
    except urllib.error.URLError as exc:
        checks.append({
            "name": "mcp_health",
            "status": "error",
            "detail": f"unreachable: {str(exc)[:200]}",
        })
    except Exception as exc:
        checks.append({
            "name": "mcp_health",
            "status": "error",
            "detail": f"unexpected: {str(exc)[:200]}",
        })

    # Check 4: bind-mount source is a directory
    try:
        init_dir = _diagnose_initdb_dir()
        if init_dir.exists():
            if init_dir.is_dir():
                checks.append({
                    "name": "compose_initdb_mount_type",
                    "status": "ok",
                    "detail": f"{init_dir} is a directory",
                })
            else:
                checks.append({
                    "name": "compose_initdb_mount_type",
                    "status": "error",
                    "detail": f"{init_dir} exists but is NOT a directory — fix immediately",
                })
        else:
            checks.append({
                "name": "compose_initdb_mount_type",
                "status": "skipped",
                "detail": f"{init_dir} missing (repo not deployed here?)",
            })
    except Exception as exc:
        checks.append({
            "name": "compose_initdb_mount_type",
            "status": "error",
            "detail": f"check failed: {exc}",
        })

    has_errors = any(c["status"] == "error" for c in checks)
    return {
        "checks": checks,
        "overall": "degraded" if has_errors else "ok",
    }
