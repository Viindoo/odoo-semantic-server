# src/manager/__main__.py
"""Admin CLI for profiles + repos. M2.5 only — replaced by Web UI in M5.

Usage:
    python -m src.manager add-profile NAME --version VERSION [--description TEXT]
    python -m src.manager add-repo --profile NAME --url URL --branch BRANCH --local-path PATH
    python -m src.manager list
"""

import argparse
import re
import sys
import textwrap
from pathlib import Path

import psycopg2

from src import config
from src.db import repo_registry

# Profile name: alphanumeric + underscore, 1-50 chars (matches database TEXT but
# enforces shell-safe + readable convention).
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,50}$")
# Odoo version: N.N (e.g. 17.0, 8.0, 18.0). Excludes patch suffix — Odoo
# convention uses major.minor only at registry level.
_VERSION_RE = re.compile(r"^\d{1,2}\.\d+$")


def _open_conn() -> psycopg2.extensions.connection:
    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        print(
            "✗ PostgreSQL DSN missing. Set PG_DSN env var OR `pg_dsn` in "
            "[database] section of odoo-semantic.conf.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as e:
        # Mask password in case psycopg2 echoes the DSN back in the error string
        msg = config.mask_dsn(str(e))
        print(f"✗ Cannot connect to PostgreSQL ({config.mask_dsn(dsn)}): {msg}", file=sys.stderr)
        sys.exit(1)
    conn.autocommit = True
    return conn


def _cmd_add_profile(args, conn) -> int:
    if not _PROFILE_NAME_RE.match(args.name):
        print(
            f"✗ Profile name '{args.name}' invalid. "
            "Required: 1-50 chars, alphanumeric + underscore only "
            "(e.g. 'odoo17', 'viindoo_18').",
            file=sys.stderr,
        )
        return 1
    if not _VERSION_RE.match(args.version):
        print(
            f"✗ Version '{args.version}' invalid. Required format: N.N (e.g. 17.0, 18.0).",
            file=sys.stderr,
        )
        return 1
    try:
        pid = repo_registry.add_profile(
            conn,
            name=args.name,
            odoo_version=args.version,
            description=args.description or "",
        )
    except ValueError as e:
        print(f"✗ {e}. Use a different name or remove the existing profile first.", file=sys.stderr)
        return 2
    print(f"✓ Profile '{args.name}' (id={pid}) odoo_version={args.version}")
    return 0


def _cmd_add_repo(args, conn) -> int:
    profiles = [p for p in repo_registry.list_profiles(conn) if p["name"] == args.profile]
    if not profiles:
        print(f"✗ Profile '{args.profile}' not found. Run add-profile first.", file=sys.stderr)
        return 2
    if not Path(args.local_path).is_dir():
        print(
            f"✗ local_path does not exist or is not a directory: {args.local_path}",
            file=sys.stderr,
        )
        return 1
    rid = repo_registry.add_repo(
        conn,
        profile_id=profiles[0]["id"],
        url=args.url,
        branch=args.branch,
        local_path=args.local_path,
    )
    print(f"✓ Repo (id={rid}) {args.url}@{args.branch} → {args.local_path}")
    return 0


def _cmd_list(_args, conn) -> int:
    profiles = repo_registry.list_profiles(conn)
    if not profiles:
        print("(no profiles yet — run: python -m src.manager add-profile <name> --version <ver>)")
        return 0
    for p in profiles:
        print(f"[{p['name']}] odoo_version={p['odoo_version']}")
        repos = repo_registry.get_repos_for_profile(conn, profile_name=p["name"])
        if not repos:
            print("    (no repos)")
            continue
        for r in repos:
            print(f"    - {r['url']}@{r['branch']} → {r['local_path']} [{r['status']}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser(
        "add-profile",
        help="Register a new profile (e.g. Odoo 17.0)",
        epilog=textwrap.dedent("""
            Examples:
              python -m src.manager add-profile odoo17 --version 17.0
              python -m src.manager add-profile viindoo18 --version 18.0 \\
                  --description "Viindoo addons on Odoo 18"

            Name must be 1-50 chars, alphanumeric + underscore.
            Version must be N.N (e.g. 17.0, 18.0).
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_add.add_argument("name", help="Profile name (a-z, 0-9, _)")
    p_add.add_argument("--version", required=True, help="Odoo version, e.g. 17.0")
    p_add.add_argument("--description", default="")

    p_repo = sub.add_parser(
        "add-repo",
        help="Attach a repo to an existing profile",
        epilog=textwrap.dedent("""
            Examples:
              python -m src.manager add-repo --profile odoo17 \\
                  --url https://github.com/odoo/odoo --branch 17.0 \\
                  --local-path ~/git/odoo_17.0
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_repo.add_argument("--profile", required=True, help="Existing profile name")
    p_repo.add_argument(
        "--url", required=True, help="Repo URL (informational; indexer reads local_path)"
    )
    p_repo.add_argument("--branch", required=True, help="Git branch")
    p_repo.add_argument(
        "--local-path",
        required=True,
        dest="local_path",
        help="Absolute path to local checkout (must exist)",
    )

    sub.add_parser("list", help="List all profiles + their repos")

    args = parser.parse_args(argv)
    conn = _open_conn()
    try:
        return {
            "add-profile": _cmd_add_profile,
            "add-repo": _cmd_add_repo,
            "list": _cmd_list,
        }[args.cmd](args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
