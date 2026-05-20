# SPDX-License-Identifier: AGPL-3.0-or-later
# src/manager/__main__.py
"""Admin CLI for profiles + repos + API keys. M2.5 — replaced by Web UI in M5.

Usage:
    python -m src.manager add-profile NAME --version VERSION [--description TEXT]
    python -m src.manager add-repo --profile NAME --url URL --branch BRANCH --local-path PATH
    python -m src.manager list
    python -m src.manager create-api-key NAME
    python -m src.manager apply-preset PRESET [--repo-base-dir DIR] [--repo-map URL=PATH ...]
        [--dry-run]
"""

import argparse
import concurrent.futures
import getpass
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlparse

import psycopg2

from src import config
from src.db.pg import auth_store, repo_store
from src.indexer.version_presets import list_presets, load_preset

# Profile name: alphanumeric + underscore, 1-50 chars (matches database TEXT but
# enforces shell-safe + readable convention).
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,50}$")
# Odoo version: N.N (e.g. 17.0, 8.0, 18.0). Excludes patch suffix — Odoo
# convention uses major.minor only at registry level.
_VERSION_RE = re.compile(r"^\d{1,2}\.\d+$")


def _open_conn():
    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        print(config.dsn_missing_hint(), file=sys.stderr)
        sys.exit(1)
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as e:
        # Mask password in case psycopg2 echoes the DSN back in the error string
        msg = config.mask_dsn(str(e))
        print(f"✗ Cannot connect to PostgreSQL ({config.mask_dsn(dsn)}): {msg}", file=sys.stderr)
        sys.exit(1)
    conn.autocommit = True
    # Also initialize centralized pool so store accessors work:
    from src.db.pg import get_pool, init_pool
    try:
        get_pool()
    except RuntimeError:
        init_pool(dsn, min_conn=1, max_conn=3)
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
        pid = repo_store().add_profile(
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
    profiles = [p for p in repo_store().list_profiles() if p["name"] == args.profile]
    if not profiles:
        print(f"✗ Profile '{args.profile}' not found. Run add-profile first.", file=sys.stderr)
        return 2
    if not Path(args.local_path).is_dir():
        print(
            f"✗ local_path does not exist or is not a directory: {args.local_path}",
            file=sys.stderr,
        )
        return 1
    rid = repo_store().add_repo(
        profile_id=profiles[0]["id"],
        url=args.url,
        branch=args.branch,
        local_path=args.local_path,
    )
    print(f"✓ Repo (id={rid}) {args.url}@{args.branch} → {args.local_path}")
    return 0


def _cmd_list(_args, conn) -> int:
    profiles = repo_store().list_profiles()
    if not profiles:
        print("(no profiles yet — run: python -m src.manager add-profile <name> --version <ver>)")
        return 0
    for p in profiles:
        print(f"[{p['name']}] odoo_version={p['odoo_version']}")
        repos = repo_store().get_repos_for_profile(profile_name=p["name"])
        if not repos:
            print("    (no repos)")
            continue
        for r in repos:
            print(f"    - {r['url']}@{r['branch']} → {r['local_path']} [{r['status']}]")
    return 0


def _cmd_create_api_key(args, conn) -> int:
    if not args.name:
        print("✗ Name is required.", file=sys.stderr)
        return 1
    raw_key, key_prefix, key_id = auth_store().create_api_key(args.name)
    print(f"API key: {raw_key}")
    print(f"Key ID:  {key_id}")
    print(f"Prefix:  {key_prefix}")
    print("WARNING: This key will not be shown again. Store it securely.")
    return 0


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_@.\-]{1,64}$")


def _cmd_create_webui_user(args, conn) -> int:
    """Create or reset a Web UI user.

    Prompts for password interactively (getpass). Uses bcrypt cost=12.
    --reset allows overwriting an existing user's password.
    --admin sets is_admin=TRUE on creation.
    """
    from src.web_ui.auth import hash_password

    username = args.username.strip()
    if not _USERNAME_RE.match(username):
        print(
            f"✗ Username '{username}' invalid. "
            "Allowed: 1-64 chars, alphanumeric + _ @ . -",
            file=sys.stderr,
        )
        return 1

    try:
        pw1 = getpass.getpass(f"Password for '{username}': ")
        pw2 = getpass.getpass("Confirm password: ")
    except (KeyboardInterrupt, EOFError):
        print("\n✗ Aborted.", file=sys.stderr)
        return 1

    if pw1 != pw2:
        print("✗ Passwords do not match.", file=sys.stderr)
        return 1
    if not pw1:
        print("✗ Password must not be empty.", file=sys.stderr)
        return 1

    pw_hash = hash_password(pw1)

    existing_hash = auth_store().get_user_password_hash(username)
    if existing_hash is not None and not args.reset:
        print(
            f"✗ User '{username}' already exists. Use --reset to overwrite password.",
            file=sys.stderr,
        )
        return 2

    auth_store().set_user_password(username, pw_hash, is_admin=args.admin)
    if existing_hash is not None:
        print(f"✓ Password reset for '{username}'{'(admin)' if args.admin else ''}.")
    else:
        print(f"✓ Web UI user '{username}' created{'(admin)' if args.admin else ''}.")
    return 0


def _audit_log(
    actor: str, action: str, target: str, success: bool, detail: dict | None = None
) -> None:
    """Write an audit event. Delegates to src.db.audit.write_audit_log.

    Kept for call-site backward-compatibility. Fire-and-forget — never raises.
    The 'actor' parameter is accepted but ignored; the canonical CLI actor is
    resolved by write_audit_log via resolve_actor(cli=True).
    """
    from src.db.audit import write_audit_log
    write_audit_log(
        actor=f"cli:{os.environ.get('USER', 'unknown')}",
        action=action,
        target=target,
        success=success,
        detail=detail,
    )


def _cmd_delete_profile(args, conn) -> int:
    """Delete a profile and all its repos (cascade).

    Prompts for interactive YES confirmation unless --yes is provided.
    """
    profile_name = args.name
    profiles = [p for p in repo_store().list_profiles() if p["name"] == profile_name]
    if not profiles:
        print(f"✗ Profile '{profile_name}' not found.", file=sys.stderr)
        return 2

    profile = profiles[0]
    if not args.yes:
        confirm = input(f"Delete profile '{profile_name}' and all its repos? Type YES: ")
        if confirm.strip() != "YES":
            print("✗ Aborted.", file=sys.stderr)
            return 0

    try:
        repo_store().delete_profile(profile["id"])
        _audit_log(
            "cli",
            "profile.delete",
            profile_name,
            True,
            {"profile_id": profile["id"], "yes_flag": args.yes},
        )
        print(f"✓ Deleted profile '{profile_name}' (id={profile['id']})")
        return 0
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        _audit_log("cli", "profile.delete", profile_name, False, {"error": str(e)})
        return 1


def _cmd_delete_repo(args, conn) -> int:
    """Delete a repo by ID or URL.

    Prompts for interactive YES confirmation unless --yes is provided.
    """
    id_or_url = args.id_or_url
    repo = None
    repo_id = None

    # Try parsing as int first
    try:
        repo_id = int(id_or_url)
        repo = repo_store().get_repo_by_id(repo_id)
    except ValueError:
        # Try as URL
        repos = repo_store().list_repos()
        for r in repos:
            if r.get("url") == id_or_url:
                repo = r
                repo_id = r["id"]
                break

    if repo is None:
        print(f"✗ Repo '{id_or_url}' not found (by ID or URL).", file=sys.stderr)
        return 2

    if not args.yes:
        confirm = input(f"Delete repo (id={repo['id']}, url={repo['url']})? Type YES: ")
        if confirm.strip() != "YES":
            print("✗ Aborted.", file=sys.stderr)
            return 0

    try:
        repo_store().delete_repo(repo_id)
        _audit_log(
            "cli",
            "repo.delete",
            str(repo_id),
            True,
            {"url": repo["url"], "yes_flag": args.yes},
        )
        print(f"✓ Deleted repo (id={repo_id}, url={repo['url']})")
        return 0
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        _audit_log("cli", "repo.delete", str(repo_id), False, {"error": str(e)})
        return 1


def _cmd_delete_webui_user(args, conn) -> int:
    """Delete a Web UI user by username.

    Prompts for interactive YES confirmation unless --yes is provided.
    """
    username = args.username.strip()
    if not _USERNAME_RE.match(username):
        print(
            f"✗ Username '{username}' invalid. "
            "Allowed: 1-64 chars, alphanumeric + _ @ . -",
            file=sys.stderr,
        )
        return 1

    # Check if user exists
    if auth_store().get_user_password_hash(username) is None:
        print(f"✗ User '{username}' not found.", file=sys.stderr)
        return 2

    if not args.yes:
        confirm = input(f"Delete Web UI user '{username}'? Type YES: ")
        if confirm.strip() != "YES":
            print("✗ Aborted.", file=sys.stderr)
            return 0

    try:
        auth_store().delete_user(username)
        _audit_log("cli", "webui_user.delete", username, True, {"yes_flag": args.yes})
        print(f"✓ Deleted Web UI user '{username}'")
        return 0
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        _audit_log("cli", "webui_user.delete", username, False, {"error": str(e)})
        return 1


def _cmd_list_webui_users(args, conn) -> int:
    """List all Web UI users in a table format."""
    users = auth_store().list_users()
    if not users:
        print("(no Web UI users yet)")
        return 0

    print(f"{'username':<32} {'is_admin':<10} {'is_active':<10} {'created_at'}")
    print("-" * 80)
    for u in users:
        is_admin = str(u.get("is_admin", False))
        is_active = str(u.get("is_active", True))
        created_at = str(u.get("created_at", ""))
        print(f"{u['username']:<32} {is_admin:<10} {is_active:<10} {created_at}")
    return 0


def _cmd_seed_master_data(args, conn) -> int:
    """Seed (or reset) 26 master data profiles + their repos.

    Idempotent — INSERT ... ON CONFLICT DO NOTHING. Existing profiles trùng
    name (manual hoặc seed cũ) không bị overwrite. With ``--reset``, DELETE
    all profiles matching seed-name prefixes first (CASCADE removes child
    repos); requires interactive ``YES`` confirm.

    ``--reset`` and ``--profiles-only`` are mutually exclusive: combining them
    would CASCADE-delete child repos but then skip re-seeding them, leaving
    seeded profiles with no repos (silent foot-gun flagged by Opus review).
    """
    from src.db import seed_master_data

    if args.reset and args.profiles_only:
        print(
            "✗ --reset and --profiles-only cannot be combined: --reset would "
            "CASCADE-delete child repos, then --profiles-only would skip "
            "re-seeding them, leaving seeded profiles with no repos.\n"
            "  Use one flag at a time.",
            file=sys.stderr,
        )
        return 1

    if args.reset:
        print(
            "⚠ --reset will DELETE every profile whose name starts with "
            "'odoo_', 'standard_viindoo_', or 'viindoo_internal_'.\n"
            "  CASCADE will remove their child repos.\n"
            "  Manually-created profiles matching these prefixes WILL also be deleted.",
            file=sys.stderr,
        )
        try:
            confirm = input("Type 'YES' to confirm: ")
        except (KeyboardInterrupt, EOFError):
            print("\n✗ Aborted.", file=sys.stderr)
            return 1
        if confirm.strip() != "YES":
            print("✗ Aborted (confirmation did not match).", file=sys.stderr)
            return 1
        deleted = seed_master_data.reset_seeded_data(conn)
        print(f"✓ Reset: {deleted} seeded profile(s) deleted (CASCADE removed child repos)")

    if args.profiles_only:
        p_in, p_sk = seed_master_data.seed_profiles(conn)
        print(f"✓ Seeded master data: {p_in} profiles new, {p_sk} unchanged (repos skipped)")
    else:
        summary = seed_master_data.seed_all(conn)
        print(
            f"✓ Seeded master data: {summary['profiles_inserted']} profiles new, "
            f"{summary['profiles_skipped']} unchanged; "
            f"{summary['repos_inserted']} repos new, "
            f"{summary['repos_skipped']} unchanged"
        )
    return 0


def _short_circuit_file_url(repo_id: int, url: str) -> bool:
    """If *url* is a file:// URL pointing to an existing directory, mark it cloned.

    Returns True if short-circuited (no subprocess needed), False otherwise.
    """
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return False
    # urlparse puts the path in .path; netloc is empty for file:///abs/path,
    # or carries the host for file://host/abs/path (uncommon).
    local_path = parsed.netloc + parsed.path if parsed.netloc else parsed.path
    if not Path(local_path).is_dir():
        return False
    repo_store().update_repo_local_path(repo_id, local_path)
    repo_store().set_clone_status(repo_id, "cloned")
    return True


def _cmd_clone_profile(args, conn) -> int:  # noqa: ARG001 (conn unused but required by dispatch)
    """Clone all pending/manual/error repos for a profile.

    Short-circuits file:// URLs that point to existing local directories by
    marking them 'cloned' directly (no subprocess).  All other URLs are cloned
    by spawning ``python -m src.cloner --repo-id <id>`` subprocesses, capped
    to ``--max-parallel`` concurrent workers.
    """
    profile_name: str = args.profile_name
    include_ancestors: bool = args.include_ancestors
    ssh_key_id_arg: int | None = args.ssh_key_id
    max_parallel: int = max(1, args.max_parallel)

    # 1. Resolve repo list
    if include_ancestors:
        all_repos = repo_store().get_ancestor_repos(profile_name)
    else:
        all_repos = repo_store().get_repos_for_profile(profile_name)

    if not all_repos:
        print(f"✗ No repos found for profile '{profile_name}'.", file=sys.stderr)
        return 2

    # 2. Filter to actionable clone statuses
    pending_statuses = {"manual", "pending", "error"}
    repos = [r for r in all_repos if r.get("clone_status", "manual") in pending_statuses]

    # 3. Assign ssh_key_id when requested and not already set
    if ssh_key_id_arg is not None:
        from src.db.pg import get_pool
        pool = get_pool()
        for r in repos:
            if r.get("ssh_key_id") is None and r.get("url", "").startswith("git@"):
                with pool.checkout() as c:
                    pool.execute(
                        c,
                        "UPDATE repos SET ssh_key_id = %s WHERE id = %s",
                        (ssh_key_id_arg, r["id"]),
                    )
                r["ssh_key_id"] = ssh_key_id_arg

    total = len(repos)
    if total == 0:
        print(f"✓ Nothing to clone for '{profile_name}' (all repos already cloned).")
        return 0

    cloned = 0
    short_circuited = 0
    failed = 0

    def _clone_one(item: tuple[int, str]) -> str:
        """Worker: clone one repo; returns 'short', 'ok', or 'error'."""
        idx, repo = item
        repo_id: int = repo["id"]
        url: str = repo["url"]
        print(f"[{idx}/{total}] cloning {url}")
        if _short_circuit_file_url(repo_id, url):
            return "short"
        result = subprocess.run(
            [sys.executable, "-m", "src.cloner", "--repo-id", str(repo_id)],
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_snippet = (result.stderr or b"").decode(errors="replace")[-200:]
            print(
                f"  ✗ repo id={repo_id} exit {result.returncode}: {stderr_snippet}",
                file=sys.stderr,
            )
            return "error"
        return "ok"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {
            executor.submit(_clone_one, (i + 1, r)): r
            for i, r in enumerate(repos)
        }
        for fut in concurrent.futures.as_completed(futures):
            outcome = fut.result()
            if outcome == "short":
                short_circuited += 1
                cloned += 1
            elif outcome == "ok":
                cloned += 1
            else:
                failed += 1

    print(
        f"Cloned: {cloned} | Short-circuited: {short_circuited} | Failed: {failed}"
    )
    return 0 if failed == 0 else 1


def _resolve_local_path(url: str, branch: str, base_dir: str, repo_map: dict[str, str]) -> str:
    """Resolve local path for a repo URL.

    Resolution order:
    1. Explicit mapping from --repo-map URL=PATH.
    2. Derived: base_dir / stem_branch (e.g. ~/git/odoo_17.0).
    """
    if url in repo_map:
        return repo_map[url]
    stem = Path(url).stem.removesuffix(".git")
    derived = os.path.join(base_dir, f"{stem}_{branch}")
    return derived


def _cmd_apply_preset_write(
    conn,
    *,
    profile_name: str,
    odoo_version: str,
    description: str,
    resolved_repos: list[dict],
) -> int:
    """Write pre-validated preset data to DB. Called after path validation succeeds."""
    try:
        pid = repo_store().add_profile(
            name=profile_name, odoo_version=odoo_version, description=description
        )
    except ValueError as e:
        print(f"✗ {e}. Use a different name or remove the existing profile first.", file=sys.stderr)
        return 2

    for r in resolved_repos:
        try:
            repo_store().add_repo(
                profile_id=pid,
                url=r["url"],
                branch=r["branch"],
                local_path=r["local_path"],
            )
        except psycopg2.errors.UniqueViolation:
            print(
                f"✗ Repo {r['url']}@{r['branch']} already registered under another profile.",
                file=sys.stderr,
            )
            return 2

    n = len(resolved_repos)
    print(
        f"✓ Profile {profile_name} registered with {n} repo{'s' if n != 1 else ''}. "
        f"Run 'python -m src.indexer index-repo --profile {profile_name}' to index."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    config.init_dotenv()
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

    p_key = sub.add_parser("create-api-key", help="Create a new API key for MCP access")
    p_key.add_argument("name", help="Descriptive name for this key (e.g. 'claude-code-laptop')")

    p_wuser = sub.add_parser(
        "create-webui-user",
        help="Create or reset a Web UI user (prompts for password)",
        epilog=textwrap.dedent("""
            Examples:
              python -m src.manager create-webui-user admin
              python -m src.manager create-webui-user admin --reset
              python -m src.manager create-webui-user admin --admin
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_wuser.add_argument("username", help="Username (1-64 chars, alphanumeric + _ @ . -)")
    p_wuser.add_argument(
        "--reset",
        action="store_true",
        help="Allow overwriting an existing user's password (for recovery)",
    )
    p_wuser.add_argument(
        "--admin",
        action="store_true",
        help="Grant is_admin=TRUE on creation",
    )

    p_del_profile = sub.add_parser(
        "delete-profile",
        help="Delete a profile and all its repos (cascade)",
        epilog=textwrap.dedent("""
            Deletes the profile and cascades to remove all attached repos.
            Requires interactive YES confirmation unless --yes is provided.

            Examples:
              python -m src.manager delete-profile odoo17
              python -m src.manager delete-profile odoo17 --yes
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_del_profile.add_argument("name", help="Profile name to delete")
    p_del_profile.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation",
    )

    p_del_repo = sub.add_parser(
        "delete-repo",
        help="Delete a repo by ID or URL",
        epilog=textwrap.dedent("""
            Deletes a single repo by its numeric ID or URL.
            Requires interactive YES confirmation unless --yes is provided.

            Examples:
              python -m src.manager delete-repo 123
              python -m src.manager delete-repo https://github.com/odoo/odoo --yes
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_del_repo.add_argument("id_or_url", help="Repo ID (integer) or URL (string)")
    p_del_repo.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation",
    )

    p_del_user = sub.add_parser(
        "delete-webui-user",
        help="Delete a Web UI user",
        epilog=textwrap.dedent("""
            Deletes a Web UI user by username.
            Requires interactive YES confirmation unless --yes is provided.

            Examples:
              python -m src.manager delete-webui-user testuser
              python -m src.manager delete-webui-user testuser --yes
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_del_user.add_argument("username", help="Username to delete")
    p_del_user.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation",
    )

    sub.add_parser(
        "list-webui-users",
        help="List all Web UI users",
    )

    p_seed = sub.add_parser(
        "seed-master-data",
        help="Seed (or reset) the 26 master data profiles + their repos",
        epilog=textwrap.dedent("""
            Idempotent — INSERT ... ON CONFLICT DO NOTHING. Existing profiles
            with the same name (manual or prior seed) are NOT overwritten.

            Examples:
              # Standard idempotent seed (also auto-run by `python -m src.db.migrate`)
              python -m src.manager seed-master-data

              # Seed only the profiles row, skip repos (rare — usually for QA)
              python -m src.manager seed-master-data --profiles-only

              # DESTRUCTIVE: drop all seeded profiles + their repos, then re-seed
              python -m src.manager seed-master-data --reset
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_seed.add_argument(
        "--profiles-only",
        action="store_true",
        dest="profiles_only",
        help="Seed profiles table only; skip repos seeding",
    )
    p_seed.add_argument(
        "--reset",
        action="store_true",
        help=(
            "DESTRUCTIVE: DELETE all seed-prefix profiles (CASCADE removes repos), "
            "then re-seed. Requires interactive YES confirmation."
        ),
    )

    p_clone = sub.add_parser(
        "clone-profile",
        help="Clone all pending/manual/error repos for a profile",
        epilog=textwrap.dedent("""
            Short-circuits file:// URLs pointing to existing local directories
            (marks them 'cloned' without running git clone — required for the
            48 seed repos that already live on disk).

            Examples:
              python -m src.manager clone-profile odoo17
              python -m src.manager clone-profile viindoo17 --include-ancestors
              python -m src.manager clone-profile myprofile --ssh-key-id 1 --max-parallel 4
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_clone.add_argument("profile_name", help="Profile name whose repos to clone")
    p_clone.add_argument(
        "--include-ancestors",
        action="store_true",
        dest="include_ancestors",
        help="Also clone repos from parent profiles (recursive)",
    )
    p_clone.add_argument(
        "--ssh-key-id",
        type=int,
        default=None,
        dest="ssh_key_id",
        metavar="N",
        help="Assign SSH key id N to any git@ repo that has no key set yet",
    )
    p_clone.add_argument(
        "--max-parallel",
        type=int,
        default=4,
        dest="max_parallel",
        metavar="N",
        help="Max concurrent clone subprocesses (default: 4)",
    )

    p_preset = sub.add_parser(
        "apply-preset",
        help="Register a bundled preset of profile + repos in one command",
        epilog=textwrap.dedent(f"""
            Available presets: {", ".join(list_presets())}

            Examples:
              # Auto-derive local paths from ~/git (must exist):
              python -m src.manager apply-preset viindoo-17.0

              # Override base directory:
              python -m src.manager apply-preset viindoo-17.0 --repo-base-dir /data/repos

              # Explicit per-repo path mapping:
              python -m src.manager apply-preset viindoo-17.0 \\
                  --repo-map https://github.com/odoo/odoo=/mnt/odoo17 \\
                  --repo-map https://github.com/Viindoo/tvtmaaddons=/mnt/viindoo17

              # Preview without writing to DB:
              python -m src.manager apply-preset viindoo-17.0 --dry-run
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_preset.add_argument("name", help=f"Preset name. Available: {', '.join(list_presets())}")
    p_preset.add_argument(
        "--repo-base-dir",
        dest="repo_base_dir",
        default=None,
        help="Base directory for derived local paths (default: ~/git)",
    )
    p_preset.add_argument(
        "--repo-map",
        action="append",
        metavar="URL=PATH",
        dest="repo_map",
        help="Explicit local path for a repo URL. Repeat for multiple repos.",
    )
    p_preset.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print planned operations without writing to DB",
    )

    args = parser.parse_args(argv)

    # apply-preset: validate preset name + local paths BEFORE opening DB connection.
    # This gives clean errors (missing path, bad preset name) without needing PG_DSN.
    if args.cmd == "apply-preset":
        try:
            preset = load_preset(args.name)
        except KeyError as e:
            print(f"✗ {e}", file=sys.stderr)
            sys.exit(1)

        repo_map: dict[str, str] = {}
        for mapping in args.repo_map or []:
            if "=" not in mapping:
                print(
                    f"✗ Invalid --repo-map value: {mapping!r}. Expected format: URL=PATH",
                    file=sys.stderr,
                )
                sys.exit(1)
            url_part, path_part = mapping.split("=", 1)
            repo_map[url_part.strip()] = path_part.strip()

        base_dir = os.path.expanduser(args.repo_base_dir or "~/git")
        resolved_repos: list[dict] = []
        for repo in preset["repos"]:
            url = repo["url"]
            branch = repo["branch"]
            local_path = _resolve_local_path(url, branch, base_dir, repo_map)
            if not Path(local_path).is_dir():
                print(
                    f"✗ Local path {local_path} does not exist for repo {url}@{branch}. "
                    f"Clone it first or pass --repo-map {url}=<path>.",
                    file=sys.stderr,
                )
                sys.exit(1)
            resolved_repos.append({"url": url, "branch": branch, "local_path": local_path})

        profile_name = preset["profile_name"]
        if args.dry_run:
            print(f"[dry-run] Profile: {profile_name}  odoo_version={preset['odoo_version']}")
            print(f"[dry-run] Description: {preset['description']}")
            print("[dry-run] Repos:")
            for r in resolved_repos:
                print(f"[dry-run]   {r['url']}@{r['branch']} → {r['local_path']}")
            print(
                f"[dry-run] Run 'python -m src.indexer index-repo"
                f" --profile {profile_name}' to index."
            )
            sys.exit(0)

        # Non-dry-run: open DB and write
        conn = _open_conn()
        try:
            rc = _cmd_apply_preset_write(
                conn,
                profile_name=profile_name,
                odoo_version=preset["odoo_version"],
                description=preset["description"],
                resolved_repos=resolved_repos,
            )
        finally:
            conn.close()
        return rc

    conn = _open_conn()
    try:
        return {
            "add-profile": _cmd_add_profile,
            "add-repo": _cmd_add_repo,
            "list": _cmd_list,
            "create-api-key": _cmd_create_api_key,
            "create-webui-user": _cmd_create_webui_user,
            "delete-profile": _cmd_delete_profile,
            "delete-repo": _cmd_delete_repo,
            "delete-webui-user": _cmd_delete_webui_user,
            "list-webui-users": _cmd_list_webui_users,
            "seed-master-data": _cmd_seed_master_data,
            "clone-profile": _cmd_clone_profile,
        }[args.cmd](args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
