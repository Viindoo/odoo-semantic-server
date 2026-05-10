"""Background clone job invoked by web UI.

Lifecycle (per ADR-0008 D6):
    set_clone_status('pending') → clone_repo → set_clone_status('cloned')
                                  └→ on failure → set_clone_status('error', msg)

Invoked as: python -m src.cloner --repo-id <id>
The repo row must already exist with ssh_key_id (or NULL for HTTPS) populated.
Reads url, branch, ssh_key_id from the row; computes target_dir via
git_utils.default_clone_dir; on success writes target_dir to repos.local_path.

Exit code:
    0 = success (cloned)
    1 = failure (clone_status='error', error_msg captured)
    2 = misconfiguration (repo not found, ssh key missing, etc.)
"""
import argparse
import logging
import sys

import psycopg2

from src import config
from src.db.auth_registry import get_ssh_key_by_id
from src.db.repo_registry import (
    get_repo_by_id,
    set_clone_status,
    update_repo_local_path,
)
from src.git_utils import clone_repo, default_clone_dir, is_ssh_url
from src.web_ui.routes.ssh_keys import decrypt_private_key

logger = logging.getLogger(__name__)


def _open_conn():
    """Open PostgreSQL connection from PG_DSN env or odoo-semantic.conf."""
    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        logger.error(
            "PostgreSQL DSN missing. Set PG_DSN env var or pg_dsn in "
            "[database] section of odoo-semantic.conf."
        )
        sys.exit(2)
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as e:
        msg = config.mask_dsn(str(e))
        logger.error("Cannot connect to PostgreSQL (%s): %s", config.mask_dsn(dsn), msg)
        sys.exit(2)
    conn.autocommit = True
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.cloner",
        description="Background clone job: clone a repo registered in the DB.",
    )
    parser.add_argument("--repo-id", type=int, required=True, help="repos.id to clone")
    args = parser.parse_args(argv)

    conn = None
    try:
        conn = _open_conn()

        repo = get_repo_by_id(conn, args.repo_id)
        if repo is None:
            logger.error("repo id=%s not found", args.repo_id)
            return 2

        url: str = repo["url"]
        branch: str = repo["branch"]
        profile_name: str = repo["profile_name"]
        ssh_key_id: int | None = repo.get("ssh_key_id")

        # Validate SSH URL requirements BEFORE setting pending (misconfiguration = exit 2,
        # not a transient error that should leave the row in 'pending').
        if is_ssh_url(url) and ssh_key_id is None:
            logger.error(
                "SSH URL %s but no ssh_key_id set on repo id=%s", url, args.repo_id
            )
            set_clone_status(
                conn,
                args.repo_id,
                "error",
                error_msg="SSH URL but no ssh_key_id",
            )
            return 2

        # Mark pending as the FIRST DB write — ensures that any subsequent failure
        # (FERNET decrypt, key lookup, network) is caught by the outer except block
        # and transitions the row to 'error' rather than leaving it stuck in 'manual'.
        set_clone_status(conn, args.repo_id, "pending")

        target_dir = default_clone_dir(profile_name, url)

        # Lifecycle: pending → clone → cloned / error
        try:
            # Decrypt private key if SSH URL (FERNET errors handled here)
            private_key_pem: bytes | None = None
            if is_ssh_url(url):
                key_row = get_ssh_key_by_id(conn, ssh_key_id)
                if key_row is None:
                    raise ValueError(f"ssh_key_id={ssh_key_id} not found")
                private_key_pem = decrypt_private_key(key_row["private_key_encrypted"])

            clone_repo(url, branch, target_dir, private_key_pem=private_key_pem)
        except Exception as e:
            logger.exception("clone failed for repo id=%s", args.repo_id)
            set_clone_status(conn, args.repo_id, "error", error_msg=str(e)[:500])
            return 1

        update_repo_local_path(conn, args.repo_id, str(target_dir))
        set_clone_status(conn, args.repo_id, "cloned")
        logger.info("clone succeeded: repo id=%s → %s", args.repo_id, target_dir)
        return 0
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
