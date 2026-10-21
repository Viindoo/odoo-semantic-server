# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background clone job invoked by web UI.

Lifecycle (per ADR-0008 D6), implemented once in ``src.clone_ops.ensure_checkout``
(the nightly indexer retries a failed first clone through the same function,
issue #383 item 2):
    lock -> set_clone_status('pending') -> clone_repo/refresh_repo -> set_clone_status('cloned')
                                          (on failure -> set_clone_status('error', msg))

Mutating git ops run under a per-repo Postgres advisory lock (ADR-0035 D2) so
concurrent jobs for the same repo serialize. If the target is already a clone,
the job refreshes it in place via fetch + reset --hard (ADR-0035 D4) rather
than erroring on a non-empty dir.

Invoked as: python -m src.cloner --repo-id <id>
The repo row must already exist with ssh_key_id (or NULL for HTTPS) populated.
Reads url, branch, ssh_key_id from the row; computes target_dir via
git_utils.default_clone_dir; on success writes target_dir to repos.local_path.

SSH host-key policy (ADR-0035 D3): clone_repo uses pre-pinned known_hosts for
common forges (GitHub/GitLab/Bitbucket) + StrictHostKeyChecking=yes.  Unknown
hosts are rejected — add their key to PINNED_KNOWN_HOSTS in git_utils.py.

Exit code:
    0 = success (cloned)
    1 = failure (clone_status='error', error_msg captured), or another worker
        holds the repo lock (nothing written; the holder records its outcome)
    2 = misconfiguration (repo not found, ssh key missing, etc.)
"""
import argparse
import logging
import sys
from contextlib import contextmanager

from src import clone_ops, config

logger = logging.getLogger(__name__)


@contextmanager
def _repo_clone_lock(repo_id: int):
    """Hold the per-repo Postgres advisory lock for a clone/refresh (ADR-0035 D2).

    Checks out a dedicated pooled connection (session-scoped advisory lock) and
    acquires the repo lock; raises ``clone_ops.RepoGitLockBusy`` (a
    RuntimeError) if another worker is already mutating the same repo.
    """
    from src.clone_ops import repo_git_lock
    from src.db.pg import get_pool
    with get_pool().checkout() as conn:
        with repo_git_lock(conn, repo_id):
            yield


@contextmanager
def _clone_dir_lock(target_dir):
    """Hold the clone-directory advisory lock (two repos whose URLs end in the
    same slug share a clone directory); raises ``clone_ops.RepoGitLockBusy``
    when another worker holds it."""
    from src.clone_ops import checkout_dir_lock
    from src.db.pg import get_pool
    with get_pool().checkout() as conn:
        with checkout_dir_lock(conn, target_dir):
            yield


def _init_pg() -> None:
    """Initialize centralized PG pool from config. Exits on failure."""
    from src.db.pg import get_pool, init_pool
    try:
        get_pool()
        return
    except RuntimeError:
        pass
    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        # Print to stderr (multi-line, operator-facing); also log a single-line
        # summary so journalctl tail stays readable when run under systemd.
        print(config.dsn_missing_hint(), file=sys.stderr)
        logger.error("PostgreSQL DSN missing (see stderr for fix options)")
        sys.exit(2)
    try:
        init_pool(dsn, min_conn=1, max_conn=3)
    except Exception as e:
        msg = config.mask_dsn(str(e))
        logger.error("Cannot connect to PostgreSQL (%s): %s", config.mask_dsn(dsn), msg)
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    config.init_dotenv()
    parser = argparse.ArgumentParser(
        prog="src.cloner",
        description="Background clone job: clone a repo registered in the DB.",
    )
    parser.add_argument("--repo-id", type=int, required=True, help="repos.id to clone")
    args = parser.parse_args(argv)

    _init_pg()
    from src.db.pg import repo_store

    repo = repo_store().get_repo_by_id(args.repo_id)
    if repo is None:
        logger.error("repo id=%s not found", args.repo_id)
        return 2

    result = clone_ops.ensure_checkout(
        repo, profile_name=repo["profile_name"], lock=_repo_clone_lock,
        dir_lock=_clone_dir_lock,
    )
    if result.outcome == clone_ops.OUTCOME_MISCONFIGURED:
        logger.error("repo id=%s misconfigured: %s", args.repo_id, result.error)
        return 2
    if not result.ok:
        logger.error("clone failed for repo id=%s: %s", args.repo_id, result.error)
        return 1
    logger.info(
        "clone succeeded (%s): repo id=%s -> %s",
        result.outcome, args.repo_id, result.target_dir,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
