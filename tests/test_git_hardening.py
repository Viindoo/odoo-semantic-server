# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for WI-H git hardening (ADR-0035):
  - known_hosts pinning: StrictHostKeyChecking=yes replaces accept-new.
  - refresh_repo: git fetch + git reset --hard, stale lock cleanup.
  - Per-repo Postgres advisory lock (_repo_git_lock / _repo_lock_id).

Advisory-lock tests require Postgres (marked postgres).
Refresh/lock-file tests use local temp git repos (no network).
"""
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from src.git_utils import PINNED_KNOWN_HOSTS, _clean_git_locks, clone_repo, refresh_repo
from src.indexer.pipeline import _repo_git_lock, _repo_lock_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    """Run git command inside a repo, raising on failure."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _make_bare_repo_with_branch(tmp_path: Path, name: str, branch: str) -> Path:
    """Create a bare git repo at tmp_path/<name>.git with one commit on <branch>."""
    work = tmp_path / f"_work_{name}"
    work.mkdir()
    _git(work, "init")
    _git(work, "checkout", "-b", branch)
    _git(work, "config", "user.email", "test@x")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("v1\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "init")

    bare = tmp_path / f"{name}.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)],
        check=True,
        capture_output=True,
    )
    return bare


def _make_clone(bare_repo: Path, target: Path, branch: str) -> Path:
    """Clone bare_repo to target; return target."""
    subprocess.run(
        ["git", "clone", "--branch", branch, "--single-branch",
         f"file://{bare_repo}", str(target)],
        check=True,
        capture_output=True,
    )
    return target


def _push_update_to_bare(
    bare_repo: Path, tmp_path: Path, branch: str, filename: str, content: str
) -> None:
    """Push a new commit to bare_repo via a work clone."""
    pusher = tmp_path / "_pusher"
    if not pusher.exists():
        subprocess.run(
            ["git", "clone", f"file://{bare_repo}", str(pusher)],
            check=True,
            capture_output=True,
        )
    _git(pusher, "checkout", branch)
    _git(pusher, "config", "user.email", "test@x")
    _git(pusher, "config", "user.name", "Test")
    (pusher / filename).write_text(content)
    _git(pusher, "add", filename)
    _git(pusher, "commit", "-m", f"add {filename}")
    _git(pusher, "push", "origin", branch)


# ===========================================================================
# 1. known_hosts pinning — StrictHostKeyChecking=yes in GIT_SSH_COMMAND
# ===========================================================================

class TestKnownHostsPinning:
    def test_clone_uses_strict_checking_yes(self, tmp_path, monkeypatch):
        """clone_repo must set StrictHostKeyChecking=yes (not accept-new)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        target = tmp_path / "out"
        captured_gsc: list[str] = []

        def fake_run(cmd, env=None, check=True, timeout=600):
            gsc = (env or {}).get("GIT_SSH_COMMAND", "")
            captured_gsc.append(gsc)
            from unittest.mock import MagicMock
            return MagicMock(returncode=0)

        with patch("src.git_utils.subprocess.run", side_effect=fake_run):
            clone_repo(
                "git@example.com:org/repo.git",
                "main",
                target,
                private_key_pem=b"fake-pem",
            )

        assert captured_gsc, "GIT_SSH_COMMAND not captured"
        gsc = captured_gsc[0]
        assert "StrictHostKeyChecking=yes" in gsc, (
            f"Expected StrictHostKeyChecking=yes, got: {gsc}"
        )
        assert "accept-new" not in gsc, (
            f"accept-new still present (TOFU not removed): {gsc}"
        )

    def test_pinned_known_hosts_contains_common_forges(self, tmp_path, monkeypatch):
        """PINNED_KNOWN_HOSTS must include entries for github.com, gitlab.com, bitbucket.org."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert "github.com" in PINNED_KNOWN_HOSTS
        assert "gitlab.com" in PINNED_KNOWN_HOSTS
        assert "bitbucket.org" in PINNED_KNOWN_HOSTS
        # Spot-check: each forge has at least one ed25519 key entry.
        lines = PINNED_KNOWN_HOSTS.splitlines()
        forge_ed25519 = {
            h: any(h in ln and "ssh-ed25519" in ln for ln in lines)
            for h in ("github.com", "gitlab.com", "bitbucket.org")
        }
        for forge, has_ed25519 in forge_ed25519.items():
            assert has_ed25519, f"No ed25519 entry for {forge} in PINNED_KNOWN_HOSTS"

    def test_unknown_host_is_rejected_not_accepted(self, tmp_path, monkeypatch):
        """A clone with StrictHostKeyChecking=yes must fail for an unknown host.

        This test verifies end-to-end rejection using a real but unreachable
        SSH host not in the pinned known_hosts file.  We use a local invalid
        SSH URL (localhost on a non-standard port with no server) to avoid
        network dependency, relying on the fact that StrictHostKeyChecking=yes
        causes ssh to exit non-zero when the host is not in known_hosts (which
        happens before any network connection for a non-existent host,
        OR at connection time if the host connects but isn't in known_hosts).

        Because we cannot easily start a real SSH server in tests, we verify
        the policy at the level of the GIT_SSH_COMMAND string:
        StrictHostKeyChecking=yes is set, and the known_hosts file does NOT
        contain the test host — meaning ssh would reject it.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        target = tmp_path / "out"
        captured_gsc: list[str] = []
        captured_kh_path: list[Path] = []

        def fake_run(cmd, env=None, check=True, timeout=600):
            gsc = (env or {}).get("GIT_SSH_COMMAND", "")
            captured_gsc.append(gsc)
            # Extract UserKnownHostsFile path from GIT_SSH_COMMAND
            for part in gsc.split():
                if part.startswith("/") and "known_hosts" in part:
                    captured_kh_path.append(Path(part))
            from unittest.mock import MagicMock
            return MagicMock(returncode=0)

        with patch("src.git_utils.subprocess.run", side_effect=fake_run):
            clone_repo(
                "git@unknown-private-forge.example.invalid:org/repo.git",
                "main",
                target,
                private_key_pem=b"fake-pem",
            )

        assert captured_gsc
        gsc = captured_gsc[0]
        # Policy: StrictHostKeyChecking=yes is always set.
        assert "StrictHostKeyChecking=yes" in gsc

        # Verify the known_hosts file does NOT contain the test host.
        if captured_kh_path:
            kh_content = captured_kh_path[0].read_text(encoding="utf-8")
            assert "unknown-private-forge.example.invalid" not in kh_content, (
                "Unknown host found in pinned known_hosts — would be trusted (wrong)"
            )


# ===========================================================================
# 2. refresh_repo — fetch + reset --hard + stale lock cleanup
# ===========================================================================

class TestRefreshRepo:
    def test_refresh_picks_up_new_commit(self, tmp_path, monkeypatch):
        """After a new commit is pushed to origin, refresh_repo advances the local clone."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        bare = _make_bare_repo_with_branch(tmp_path, "myrepo", "main")
        clone = _make_clone(bare, tmp_path / "local", "main")

        # Verify initial state
        assert (clone / "README.md").read_text() == "v1\n"

        # Push a new commit to the bare repo
        _push_update_to_bare(bare, tmp_path, "main", "newfile.txt", "hello\n")

        # Refresh the clone
        refresh_repo(clone, "main", private_key_pem=None)

        # The new file should now be present
        assert (clone / "newfile.txt").exists(), "refresh_repo did not pull in new commit"
        assert (clone / "newfile.txt").read_text() == "hello\n"

    def test_refresh_cleans_stale_lock(self, tmp_path, monkeypatch):
        """refresh_repo removes stale .git/*.lock files before operating."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        bare = _make_bare_repo_with_branch(tmp_path, "lockrepo", "main")
        clone = _make_clone(bare, tmp_path / "locked", "main")

        # Simulate stale lock left by SIGKILL
        stale_lock = clone / ".git" / "index.lock"
        stale_lock.write_text("stale\n")
        assert stale_lock.exists()

        # Refresh should succeed despite the stale lock
        refresh_repo(clone, "main", private_key_pem=None)

        # Lock file must be gone
        assert not stale_lock.exists(), "stale .git/index.lock was not cleaned"

    def test_refresh_raises_on_non_repo(self, tmp_path):
        """refresh_repo raises FileNotFoundError for a path that is not a git repo."""
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        with pytest.raises(FileNotFoundError, match="not a git repository"):
            refresh_repo(not_a_repo, "main", private_key_pem=None)

    def test_refresh_uses_strict_checking_yes(self, tmp_path, monkeypatch):
        """refresh_repo sets StrictHostKeyChecking=yes in GIT_SSH_COMMAND."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        bare = _make_bare_repo_with_branch(tmp_path, "sshrepo", "main")
        clone = _make_clone(bare, tmp_path / "ssh_local", "main")

        captured_gsc: list[str] = []

        original_run = subprocess.run

        def fake_run(cmd, env=None, check=True, timeout=600):
            gsc = (env or {}).get("GIT_SSH_COMMAND", "")
            if gsc:
                captured_gsc.append(gsc)
            # Actually run the git command (fetch/reset against local file://)
            return original_run(cmd, env=env, check=check, timeout=timeout)

        with patch("src.git_utils.subprocess.run", side_effect=fake_run):
            refresh_repo(
                clone,
                "main",
                private_key_pem=b"fake-pem",
            )

        assert captured_gsc, "GIT_SSH_COMMAND not set for SSH-keyed refresh"
        for gsc in captured_gsc:
            assert "StrictHostKeyChecking=yes" in gsc, (
                f"Expected StrictHostKeyChecking=yes, got: {gsc}"
            )
            assert "accept-new" not in gsc


class TestCleanGitLocks:
    def test_cleans_multiple_lock_files(self, tmp_path):
        """_clean_git_locks removes all .lock files under .git/."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock1 = git_dir / "index.lock"
        lock2 = git_dir / "HEAD.lock"
        lock1.write_text("stale")
        lock2.write_text("stale")

        _clean_git_locks(tmp_path)

        assert not lock1.exists()
        assert not lock2.exists()

    def test_no_error_when_no_locks(self, tmp_path):
        """_clean_git_locks is a no-op (not an error) when no .lock files exist."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        _clean_git_locks(tmp_path)  # must not raise

    def test_no_error_when_git_dir_missing(self, tmp_path):
        """_clean_git_locks is a no-op when .git directory does not exist."""
        _clean_git_locks(tmp_path)  # must not raise


# ===========================================================================
# 3. Concurrent refresh serialization via per-repo advisory lock
# ===========================================================================

pytestmark_pg = pytest.mark.postgres


class TestRepoGitLock:
    """Per-repo Postgres advisory lock tests (require Postgres testcontainer)."""

    pytestmark = pytest.mark.postgres

    def test_repo_lock_id_is_deterministic(self):
        """_repo_lock_id must return the same value for the same repo_id."""
        assert _repo_lock_id(42) == _repo_lock_id(42)
        assert _repo_lock_id(1) != _repo_lock_id(2)

    def test_repo_lock_id_differs_from_profile_lock_id(self):
        """Repo lock namespace must not collide with profile lock namespace."""
        from src.indexer.pipeline import _profile_lock_id
        # Check that no small integer repo_id collides with a typical profile name.
        for repo_id in range(1, 50):
            for profile in ("default", "viindoo17", "odoo17", "test"):
                assert _repo_lock_id(repo_id) != _profile_lock_id(profile), (
                    f"Namespace collision: repo_id={repo_id} == profile={profile!r}"
                )

    def test_acquire_and_release(self, pg_conn):
        """Lock can be acquired and released without error."""
        with _repo_git_lock(pg_conn, repo_id=9001):
            with pg_conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone()[0] == 1
        # After context exit, lock is released

    def test_same_repo_blocks_second_acquire(self, pg_conn):
        """Two concurrent workers on the same repo_id: second must fail."""
        import psycopg2

        dsn = os.environ.get(
            "PG_TEST_DSN",
            "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
        )
        conn2 = psycopg2.connect(dsn)
        conn2.autocommit = True
        try:
            with _repo_git_lock(pg_conn, repo_id=9002):
                # While first lock is held, second connection must fail to acquire.
                lock_id = _repo_lock_id(9002)
                with conn2.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
                    acquired = cur.fetchone()[0]
                assert acquired is False, (
                    "Second connection must not acquire the same repo lock while first holds it"
                )
        finally:
            conn2.close()

    def test_different_repos_do_not_block(self, pg_conn):
        """Two different repo_ids must NOT block each other."""
        id_a = _repo_lock_id(9003)
        id_b = _repo_lock_id(9004)
        assert id_a != id_b

        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (id_a,))
            assert cur.fetchone()[0] is True
        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (id_b,))
            assert cur.fetchone()[0] is True
        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (id_a,))
            cur.execute("SELECT pg_advisory_unlock(%s)", (id_b,))

    def test_lock_releases_on_exception(self, pg_conn):
        """Lock is released even when an exception is raised inside the context."""
        with pytest.raises(ValueError, match="simulated"):
            with _repo_git_lock(pg_conn, repo_id=9005):
                raise ValueError("simulated")

        # Should be re-acquirable now
        with _repo_git_lock(pg_conn, repo_id=9005):
            pass

    def test_concurrent_same_repo_refreshes_serialize(self, tmp_path, pg_conn):
        """Two concurrent refresh calls on the same repo: one succeeds, one is blocked.

        The lock uses ``pg_try_advisory_lock`` (non-blocking, like the profile lock):
        the second worker gets a RuntimeError rather than waiting.  This is the
        correct behavior per ADR-0035 D2 — it prevents racing on ``.git/index.lock``
        without deadlocking, and the caller must retry/re-schedule.

        This test verifies:
          - Exactly one worker succeeds.
          - The failing worker raises RuntimeError (not a corrupt git state).
          - The git working tree is intact after both workers complete.
        """
        import psycopg2

        dsn = os.environ.get(
            "PG_TEST_DSN",
            "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
        )

        bare = _make_bare_repo_with_branch(tmp_path, "concurrent", "main")
        clone = _make_clone(bare, tmp_path / "concurrent_local", "main")

        runtime_errors: list[RuntimeError] = []
        other_errors: list[Exception] = []
        results: list[str] = []
        # barrier ensures both threads try to acquire the lock simultaneously
        barrier = threading.Barrier(2)

        def _refresh_worker(worker_id: int) -> None:
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            try:
                barrier.wait(timeout=10)  # both threads start at the same time
                with _repo_git_lock(conn, repo_id=99001):
                    refresh_repo(clone, "main", private_key_pem=None)
                    results.append(f"worker-{worker_id}-done")
            except RuntimeError as exc:
                runtime_errors.append(exc)
            except Exception as exc:
                other_errors.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=_refresh_worker, args=(1,))
        t2 = threading.Thread(target=_refresh_worker, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not other_errors, f"Unexpected non-RuntimeError errors: {other_errors}"
        # Exactly one worker succeeded; the other got a RuntimeError (lock held).
        assert len(results) == 1, f"Expected exactly 1 success, got: {results}"
        assert len(runtime_errors) == 1, (
            f"Expected exactly 1 RuntimeError (lock-busy), got: {runtime_errors}"
        )
        assert "Git mutation already in progress" in str(runtime_errors[0])
        # Verify git repo is intact (no stale .git/index.lock left by the worker).
        assert not (clone / ".git" / "index.lock").exists(), (
            "Stale index.lock left after concurrent refresh"
        )

    def test_concurrent_different_repo_refreshes_parallel(self, tmp_path, pg_conn):
        """Refreshes on DIFFERENT repos run in parallel (no blocking each other)."""
        import psycopg2

        dsn = os.environ.get(
            "PG_TEST_DSN",
            "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
        )

        bare_a = _make_bare_repo_with_branch(tmp_path, "repo_a", "main")
        bare_b = _make_bare_repo_with_branch(tmp_path, "repo_b", "main")
        clone_a = _make_clone(bare_a, tmp_path / "clone_a", "main")
        clone_b = _make_clone(bare_b, tmp_path / "clone_b", "main")

        enter_events: list[str] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def _worker(repo_id: int, clone: Path) -> None:
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            try:
                with _repo_git_lock(conn, repo_id=repo_id):
                    enter_events.append(f"enter-{repo_id}")
                    # Synchronize: both workers must be inside their locks simultaneously.
                    # If locks were shared they would deadlock here.
                    barrier.wait(timeout=10)
                    refresh_repo(clone, "main", private_key_pem=None)
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=_worker, args=(99010, clone_a))
        t2 = threading.Thread(target=_worker, args=(99011, clone_b))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not errors, f"Parallel refresh errors: {errors}"
        # Both workers must have been inside their critical section simultaneously.
        assert len(enter_events) == 2, (
            "Workers did not enter locks concurrently — different repos should not block each other"
        )
