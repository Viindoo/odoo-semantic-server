# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin CLI for odoo-semantic-mcp.

Usage:
    python -m src.cli backup --output backup/dump.tar.gz [--bundle-passphrase-env ENV_NAME]
    python -m src.cli restore <bundle.tar.gz | dump.sql>
    python -m src.cli rotate-fernet [--old-key-env OLD_FERNET_KEY] [--new-key-env NEW_FERNET_KEY]
    python -m src.cli diagnose [--json]

Structure (B1 refactor): the four subcommand handlers live in
``src/cli_commands/`` (one module per command). This file is the entrypoint:
it builds the argparse parser, dispatches to those handlers, and re-exports
their public symbols + the shared helpers so existing ``from src.cli import ...``
imports (e.g. ``src/diagnostics.py``, ``src/web_ui/routes/operations.py``) and
``patch("src.cli.<name>")`` test targets keep working unchanged. Subcommand
handlers reach the patch-sensitive helpers through this module object
(``from src import cli`` → ``cli._get_pg_dsn()``) so monkeypatching
``src.cli.<name>`` still intercepts the call.
"""
import argparse
import logging
import os
import shutil
import subprocess  # noqa: F401 — re-exported as src.cli.subprocess for tests
import sys

log = logging.getLogger(__name__)


def _resolve_postgres_tool(tool: str) -> list[str]:
    """Return command prefix: local binary if available on PATH, else docker exec wrapper."""
    if shutil.which(tool):
        return [tool]
    container = os.getenv("POSTGRES_CONTAINER", "odoo-semantic-mcp-postgres-1")
    # -e PGPASSWORD forwards the host env var by name into the container (no value = forward by ref)
    return ["docker", "exec", "-i", "-e", "PGPASSWORD", container, tool]


def _resolve_neo4j_tool(tool: str) -> list[str]:
    """Return command prefix for a Neo4j tool: local binary if on PATH, else docker exec.

    Parallel to _resolve_postgres_tool. When neo4j-admin is only available inside
    the Neo4j container (typical Docker-Compose deployments), docker exec is used
    so the dump is written to a path mounted or accessible inside the container.
    Set NEO4J_CONTAINER to override the default container name.
    """
    if shutil.which(tool):
        return [tool]
    container = os.getenv("NEO4J_CONTAINER", "odoo-semantic-mcp-neo4j-1")
    return ["docker", "exec", "-i", container, tool]


# --- Re-export shared helpers (SSOT in src/cli_commands/_common.py) ---
# These names must be bound on this module BEFORE the subcommand modules are
# imported below: the handlers look them up via the `src.cli` module object at
# call time, and external code / tests import or patch them as `src.cli.<name>`.
from src.cli_commands._common import (  # noqa: E402,F401  (re-exported for src.cli.* callers/patch targets)
    _NEO4J_UNREACHABLE_MARKERS,
    _dsn_to_pg_args_and_env,
    _export_neo4j_online,
    _get_neo4j_creds,
    _get_pg_dsn,
    _is_pg_container_running,
    _neo4j_unreachable_reason,
    _props_to_cypher,
    _restore_neo4j_cypher,
)

# --- Subcommand handlers + their public helpers (re-exported) ---
# Imported AFTER the shared helpers above are bound on this module, so the
# `from src import cli` inside each handler module resolves the patch-sensitive
# names against a fully-populated `src.cli` namespace.
from src.cli_commands.backup import (  # noqa: E402,F401  (re-exported for src.cli.* callers/patch targets)
    _DEFAULT_KEEP_BUNDLES,
    _backup_advisory_lock,
    _cmd_backup,
    _encrypt_with_passphrase,
    _get_latest_migration_version,
    _prune_old_bundles,
)
from src.cli_commands.diagnose import _cmd_diagnose, _diagnose_initdb_dir  # noqa: E402,F401
from src.cli_commands.restore import (  # noqa: E402,F401  (re-exported for src.cli.* callers/patch targets)
    _cmd_restore,
    _restore_bundle,
    _restore_sql_or_dump,
)
from src.cli_commands.rotate_fernet import _cmd_rotate_fernet, _key_fingerprint  # noqa: E402,F401


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bak = sub.add_parser("backup", help="Create complete backup bundle (.tar.gz).")
    bak.add_argument(
        "--output",
        required=True,
        help="Output path ending in .tar.gz (must be under BACKUP_DIR env var).",
    )
    bak.add_argument(
        "--bundle-passphrase-env",
        default="",
        metavar="ENV_VAR_NAME",
        help=(
            "Name of environment variable holding the passphrase used to encrypt "
            "FERNET_KEY in fernet.enc. The passphrase is never logged. "
            "Example: --bundle-passphrase-env BUNDLE_PASSPHRASE"
        ),
    )
    bak.add_argument(
        "--keep-bundles",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Keep the N newest .tar.gz bundles in BACKUP_DIR after a successful backup "
            "(default 14 — 2 weeks of daily backups). "
            "Precedence: CLI flag > OSM_BACKUP_KEEP env var > built-in default (14). "
            "Set to 0 to disable pruning."
        ),
    )

    rst = sub.add_parser("restore", help="Restore PostgreSQL from dump or .tar.gz bundle.")
    rst.add_argument("file", help="SQL dump file or .tar.gz bundle to restore.")
    rst.add_argument(
        "--bundle-passphrase-env",
        default=None,
        help="Env var name containing passphrase for fernet.enc in bundle (optional).",
    )

    rot = sub.add_parser(
        "rotate-fernet",
        help="Re-encrypt SSH keys + TOTP secrets with a new FERNET_KEY.",
    )
    rot.add_argument(
        "--old-key-env",
        default="OLD_FERNET_KEY",
        help="Name of env var holding the current FERNET key (default: OLD_FERNET_KEY).",
    )
    rot.add_argument(
        "--new-key-env",
        default="NEW_FERNET_KEY",
        help="Name of env var holding the new FERNET key (default: NEW_FERNET_KEY).",
    )

    diag = sub.add_parser(
        "diagnose",
        help="Cross-tier health check (PG, Neo4j, MCP /health, bind-mount types).",
    )
    diag.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text (for alert pipelines).",
    )

    return parser


def main(argv=None) -> int:
    from src import config
    config.init_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "backup":
        return _cmd_backup(args)
    elif args.cmd == "restore":
        return _cmd_restore(args)
    elif args.cmd == "rotate-fernet":
        return _cmd_rotate_fernet(args)
    elif args.cmd == "diagnose":
        return _cmd_diagnose(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
