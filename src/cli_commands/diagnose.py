# SPDX-License-Identifier: AGPL-3.0-or-later
"""``diagnose`` subcommand — cross-tier health diagnostic.

All check logic lives in :func:`src.diagnostics.run_diagnostics` (SSOT); this
module only formats the result for the CLI. ``_diagnose_initdb_dir`` is
re-exported from ``src/cli.py`` because :mod:`src.diagnostics` imports it from
``src.cli`` at call time (and tests monkeypatch ``src.cli._diagnose_initdb_dir``).
"""
from pathlib import Path


def _diagnose_initdb_dir() -> Path:
    """Resolve `docker/initdb.d` against the repo root (NOT runtime cwd).

    Same pattern as `src/db/migrate.py`'s `_MIGRATIONS_DIR`: anchor to
    `__file__` so the check works under systemd (`WorkingDirectory=/`), cron,
    or any caller. Exposed as a function (rather than a module constant) so
    tests can monkeypatch it cleanly.
    """
    return Path(__file__).resolve().parent.parent.parent / "docker" / "initdb.d"


def _cmd_diagnose(args) -> int:
    """Cross-tier health diagnostic. Reports PG container, Neo4j container,
    MCP /health endpoint, and bind-mount source types declared in compose.

    Delegates all check logic to ``src.diagnostics.run_diagnostics()`` (SSOT)
    so the HTTP endpoint can reuse the same checks without code duplication.

    Output: human-readable text by default; `--json` emits a single object
    suitable for piping into a remote alert pipeline.

    Exit codes:
        0  all checks passed (or all checks skipped because docker absent)
        1  at least one check FAILED — see output for which
    """
    import json as _json

    from src.diagnostics import run_diagnostics
    result = run_diagnostics()
    checks = result["checks"]

    # Map shared status names to CLI legacy names for human-readable output
    _status_symbol = {"ok": "✓", "error": "✗", "skipped": "~"}
    errors = [c for c in checks if c["status"] == "error"]

    if getattr(args, "json", False):
        # Emit JSON using the shared schema (name/status/detail) + failure count
        print(_json.dumps({"checks": checks, "failures": len(errors)}, indent=2))
    else:
        print("=== osm diagnose ===")
        for c in checks:
            symbol = _status_symbol.get(c["status"], "?")
            print(f"  {symbol} {c['name']:<30} {c['detail']}")
        print(f"\n{len(errors)} failure(s) of {len(checks)} checks")

    return 1 if errors else 0
