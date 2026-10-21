# SPDX-License-Identifier: AGPL-3.0-or-later
# src/clone_ops.py
"""Clone or refresh ONE registered repo - the core shared by the on-demand
cloner (``python -m src.cloner``, Web UI clone / clone-all) and the nightly
indexer (``pipeline_repo._ensure_repo_checkout``, issue #383 item 2).

One implementation, so the two paths cannot diverge on the clone lifecycle,
the lock or the SSH credential handling:

- the per-repo Postgres advisory lock (:func:`repo_git_lock`, ADR-0035 D2) is
  held around every mutating git op; contention raises :class:`RepoGitLockBusy`
  (a ``RuntimeError``, the type callers always caught). A second lock keyed by
  the clone DIRECTORY (:func:`checkout_dir_lock`) is held inside it: two repos
  whose URLs end in the same slug map to one clone directory, and their
  per-repo locks (different ids) would not serialize them. A directory that
  already holds a clone of ANOTHER origin is never refreshed for this repo;
- the SSH key comes from the shared SSOT ``src.ssh_key_resolve`` (FERNET
  decrypt in ``src.crypto``); ``src.git_utils.clone_repo`` /
  ``refresh_repo`` pass it through ``GIT_SSH_COMMAND`` with the pinned
  ``known_hosts`` and ``StrictHostKeyChecking=yes`` (ADR-0008 / ADR-0035);
- ``repos.clone_status``: ``pending`` once the lock is held, then ``cloned``
  (with ``local_path`` = the clone directory) or ``error`` +
  ``clone_error_msg``. A busy lock writes nothing: the holder owns the row.

Layering: a leaf below the indexer and the Web UI. It imports
``src.git_utils``, ``src.ssh_key_resolve`` and (lazily) ``src.db.pg`` only,
so ``src/indexer`` may import it (tests/test_pipeline_import_discipline.py).
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from src.git_utils import clone_repo, default_clone_dir, is_ssh_url, refresh_repo
from src.ssh_key_resolve import SshKeyUnavailable, resolve_ssh_key_pem

_logger = logging.getLogger(__name__)

# clone_status values a clone is (re)tried for: never cloned (``manual``), a
# job that died mid-clone (``pending``), a failed clone (``error``). The Web UI
# clone-all route selects the same set. The nightly indexer retries ``manual``
# only for a row whose ``local_path`` is the clone directory OSM manages
# (:func:`is_managed_clone_dir`); a ``manual`` row registered by path (an
# operator's checkout, ``apply-preset --base-dir`` / ``--repo-map``) is never
# replaced by a fresh clone.
CLONE_RETRY_STATUSES = frozenset({"manual", "pending", "error"})

OUTCOME_CLONED = "cloned"
OUTCOME_REFRESHED = "refreshed"
OUTCOME_BUSY = "busy"
OUTCOME_FAILED = "error"
OUTCOME_MISCONFIGURED = "misconfigured"

# Bound of ``repos.clone_error_msg`` (the cloner always wrote 500 chars).
_ERROR_MSG_LIMIT = 500


class RepoGitLockBusy(RuntimeError):
    """Another worker holds the per-repo git lock (a clone or fetch is running)."""


def repo_lock_id(repo_id: int) -> int:
    """31-bit Postgres advisory lock id of a repo (ADR-0035 D2).

    Namespace ``osm-repo-`` (the profile lock uses ``odoo-semantic-``), so the
    two key spaces never collide even for a profile named like a repo id.
    """
    return int(hashlib.md5(f"osm-repo-{repo_id}".encode()).hexdigest(), 16) % (2**31)


@contextmanager
def repo_git_lock(pg_conn, repo_id: int):
    """Per-repo advisory lock guarding mutating git ops (clone, fetch, reset).

    Keyed by repo id, so different repos run in parallel. Read-only git ops
    (rev-parse, diff, ls-tree) never take it. Raises :class:`RepoGitLockBusy`
    when another worker holds it.
    """
    from src.db.pg import advisory_lock

    lock_id = repo_lock_id(repo_id)
    with advisory_lock(pg_conn, lock_id) as acquired:
        if not acquired:
            raise RepoGitLockBusy(
                f"Git mutation already in progress for repo id={repo_id} "
                f"(Postgres advisory lock {lock_id} held). "
                "Wait for the other worker to finish."
            )
        yield


def checkout_dir_lock_id(target_dir: Path | str) -> int:
    """31-bit advisory lock id of a clone directory (namespace ``osm-clonedir-``,
    disjoint from the repo and profile lock key spaces)."""
    resolved = Path(target_dir).expanduser().resolve()
    key = f"osm-clonedir-{resolved}"
    return int(hashlib.md5(key.encode()).hexdigest(), 16) % (2**31)


@contextmanager
def checkout_dir_lock(pg_conn, target_dir: Path | str):
    """Advisory lock on one clone directory (held inside the repo lock).

    Raises :class:`RepoGitLockBusy` when another worker holds it - a clone or
    refresh of another repo that maps to the same directory is running.
    """
    from src.db.pg import advisory_lock

    lock_id = checkout_dir_lock_id(target_dir)
    with advisory_lock(pg_conn, lock_id) as acquired:
        if not acquired:
            raise RepoGitLockBusy(
                f"Git mutation already in progress for clone directory {target_dir} "
                f"(Postgres advisory lock {lock_id} held by another repo's job)."
            )
        yield


def is_managed_clone_dir(local_path: str | Path | None, profile_name: str, url: str) -> bool:
    """True when *local_path* is the clone directory OSM itself manages for
    *url* under *profile_name* (``git_utils.default_clone_dir``), the path a
    Web UI registration records; False for a checkout registered by path."""
    if not local_path:
        return False
    try:
        return (Path(local_path).expanduser().resolve()
                == default_clone_dir(profile_name, url).resolve())
    except (OSError, ValueError):
        return False


def _origin_url(target_dir: Path) -> str | None:
    """``remote.origin.url`` of the clone in *target_dir*, or None when git
    cannot read it."""
    try:
        result = subprocess.run(
            ["git", "-C", str(target_dir), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def foreign_clone_origin(
    local_path: str | Path, url: str, profile_name: str | None,
) -> str | None:
    """The origin of the clone at *local_path* when it is the clone directory
    OSM manages for *url* (:func:`is_managed_clone_dir`) but holds a clone of
    ANOTHER origin - two repos whose URLs end in the same slug; else None.

    A checkout registered by path is never judged (its origin may name the
    same repository in another form, ssh vs https).
    """
    if not profile_name or not is_managed_clone_dir(local_path, profile_name, url):
        return None
    if not (Path(local_path) / ".git").exists():
        return None
    origin = _origin_url(Path(local_path))
    return origin if origin is not None and origin != url.strip() else None


@dataclass(frozen=True)
class CloneOutcome:
    """What :func:`ensure_checkout` did. ``target_dir`` is the checkout
    directory (``repos.local_path`` after a success); ``error`` the one-line
    cause of a failure (also in ``repos.clone_error_msg``)."""

    outcome: str
    target_dir: Path
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (OUTCOME_CLONED, OUTCOME_REFRESHED)


def _one_line(exc: BaseException) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text[:_ERROR_MSG_LIMIT]


def ensure_checkout(
    repo: dict,
    *,
    profile_name: str,
    lock: Callable[[int], AbstractContextManager],
    dir_lock: Callable[[Path], AbstractContextManager],
) -> CloneOutcome:
    """Clone *repo* (or refresh it when its clone directory is already a git
    repository of the same origin) and record the outcome on its ``repos`` row.

    *repo*: a ``repos`` row (``id``, ``url``, ``branch``, ``ssh_key_id``).
    *profile_name*: the owning profile (the clone directory is
    ``git_utils.default_clone_dir(profile_name, url)``, as at registration).
    *lock*: ``lock(repo_id)`` -> the per-repo git lock context manager
    (:func:`repo_git_lock` on a connection the caller owns). *dir_lock*:
    ``dir_lock(target_dir)`` -> the clone-directory lock
    (:func:`checkout_dir_lock`), taken inside the repo lock.

    Outcomes: ``misconfigured`` (an SSH URL without ``ssh_key_id``: status
    ``error``, nothing run), ``busy`` (either lock is held by another
    worker: nothing written), ``error`` (key resolution, decrypt or git
    failed, or the clone directory holds a clone of another origin: status
    ``error`` + message), ``cloned`` / ``refreshed`` (status ``cloned``,
    ``local_path`` = the clone directory). Never raises for a git, network,
    key or lock failure; a ``repos`` write failure propagates.
    """
    from src.db.pg import repo_store

    repo_id: int = repo["id"]
    url: str = repo.get("url") or ""
    branch: str = repo.get("branch") or ""
    target_dir = default_clone_dir(profile_name, url)

    # Misconfiguration, not a transient error: decided before the lock and
    # before ``pending`` (the cloner exits 2 on it).
    if is_ssh_url(url) and repo.get("ssh_key_id") is None:
        message = "SSH URL but no ssh_key_id"
        repo_store().set_clone_status(repo_id, "error", error_msg=message)
        return CloneOutcome(OUTCOME_MISCONFIGURED, target_dir, message)

    outcome = OUTCOME_CLONED
    try:
        with lock(repo_id), dir_lock(target_dir):
            # First write under the locks: any later failure (key lookup,
            # FERNET decrypt, network, missing branch) turns it into 'error'
            # instead of leaving the row as it was.
            repo_store().set_clone_status(repo_id, "pending")
            try:
                if (target_dir / ".git").exists():
                    origin = _origin_url(target_dir)
                    if origin is not None and origin != url.strip():
                        # Two repos whose URLs end in the same slug: the
                        # directory is the other repo's clone. Refreshing it
                        # would index the other origin under this repo.
                        raise ValueError(
                            f"clone directory {target_dir} holds a clone of {origin}, "
                            f"not {url}; register one of the two repos under another "
                            "profile or remove the stale directory"
                        )
                try:
                    private_key_pem: bytes | None = resolve_ssh_key_pem(repo)
                except SshKeyUnavailable as exc:
                    raise ValueError(str(exc)) from exc
                if (target_dir / ".git").exists():
                    refresh_repo(target_dir, branch, private_key_pem=private_key_pem)
                    outcome = OUTCOME_REFRESHED
                else:
                    clone_repo(url, branch, target_dir, private_key_pem=private_key_pem)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded
                message = _one_line(exc)
                repo_store().set_clone_status(repo_id, "error", error_msg=message)
                return CloneOutcome(OUTCOME_FAILED, target_dir, message)
    except RepoGitLockBusy as exc:
        return CloneOutcome(OUTCOME_BUSY, target_dir, _one_line(exc))

    repo_store().update_repo_local_path(repo_id, str(target_dir))
    repo_store().set_clone_status(repo_id, "cloned")
    return CloneOutcome(outcome, target_dir)
