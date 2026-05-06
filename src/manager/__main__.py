# src/manager/__main__.py
"""Admin CLI for profiles + repos. M2.5 only — replaced by Web UI in M5.

Usage:
    python -m src.manager add-profile NAME --version VERSION [--description TEXT]
    python -m src.manager add-repo --profile NAME --url URL --branch BRANCH --local-path PATH
    python -m src.manager list
"""
import argparse
import sys

import psycopg2

from src import config
from src.db import repo_registry


def _open_conn() -> psycopg2.extensions.connection:
    dsn = config.get(
        "database", "pg_dsn",
        fallback="postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    )
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as e:
        print(f"✗ Cannot connect to PostgreSQL: {e}", file=sys.stderr)
        sys.exit(1)
    conn.autocommit = True
    return conn


def _cmd_add_profile(args, conn) -> int:
    pid = repo_registry.add_profile(
        conn, name=args.name, odoo_version=args.version,
        description=args.description or "",
    )
    print(f"✓ Profile '{args.name}' (id={pid}) odoo_version={args.version}")
    return 0


def _cmd_add_repo(args, conn) -> int:
    profiles = [p for p in repo_registry.list_profiles(conn) if p["name"] == args.profile]
    if not profiles:
        print(f"✗ Profile '{args.profile}' not found. Run add-profile first.", file=sys.stderr)
        return 2
    rid = repo_registry.add_repo(
        conn, profile_id=profiles[0]["id"],
        url=args.url, branch=args.branch, local_path=args.local_path,
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

    p_add = sub.add_parser("add-profile", help="Register a new profile")
    p_add.add_argument("name")
    p_add.add_argument("--version", required=True, help="e.g. 17.0")
    p_add.add_argument("--description", default="")

    p_repo = sub.add_parser("add-repo", help="Attach a repo to a profile")
    p_repo.add_argument("--profile", required=True)
    p_repo.add_argument("--url", required=True)
    p_repo.add_argument("--branch", required=True)
    p_repo.add_argument("--local-path", required=True, dest="local_path")

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
