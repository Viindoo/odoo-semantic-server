# SPDX-License-Identifier: AGPL-3.0-or-later
"""A repo whose first clone failed is cloned again by the next index run (#383 item 2).

Incident 2026-08-07..09-25: repo 388 (themes.git, 19.0) was registered before
its branch existed, the Web UI clone failed (``clone_status='error'``), and from
then on every nightly ``index-repo --all`` raised ``FileNotFoundError`` for it
before the pre-scan fetch, logged a traceback and exited 1 - for seven weeks,
while nothing ever retried the clone.

Rules protected here (no database, no network: real git over ``file://`` bare
remotes in ``tmp_path``, the ``repos`` writes recorded by a fake store):

* a repo with no checkout whose clone never succeeded (``pending`` /
  ``error``, or ``manual`` at the clone directory OSM manages) is cloned by the
  run, through the cloner's own core, under the per-repo git lock and the
  clone-directory lock, and its row ends ``cloned`` with the new path;
* a checkout the operator chose is never replaced: a ``manual`` row
  registered by path whose directory is missing is reported, not cloned;
  a clone directory holding another repo's origin (same URL slug) is never
  refreshed or indexed for this repo;
* a clone that still fails is ONE warning line plus ``lifecycle_attention``
  and an ``unavailable_repos`` entry - never a traceback - and makes the CLI
  exit 3 (``OnFailure=`` mails the admins), while the other repos are indexed;
* ``--no-fetch`` stays offline (no clone attempt); a ``cloned`` row whose
  directory vanished is reported, not silently re-cloned; a clone another
  worker is running right now is skipped quietly.
"""
from __future__ import annotations

import contextlib
import logging
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import clone_ops
from src.git_utils import default_clone_dir
from src.indexer import pipeline as pipeline_mod
from src.indexer.pipeline_repo import RepoCheckoutUnavailable, _ensure_repo_checkout

BRANCH = "19.0"
PROFILE = "themes_19"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _origin(tmp_path: Path, branch: str) -> Path:
    """A bare remote whose only branch is *branch* (one commit, one module)."""
    work = tmp_path / "upstream"
    work.mkdir()
    _git(work, "init")
    _git(work, "checkout", "-b", branch)
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    (work / "theme_x").mkdir()
    (work / "theme_x" / "__manifest__.py").write_text("{'name': 'theme_x'}\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    bare = tmp_path / "themes.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True,
                   capture_output=True)
    return bare


class _Repos:
    """The ``repos`` writes the clone core makes (the cloner's lifecycle)."""

    def __init__(self) -> None:
        self.clone_status: list[tuple[int, str, str | None]] = []
        self.local_path: dict[int, str] = {}
        self.status: list[tuple[int, str, str | None]] = []

    def set_clone_status(self, repo_id, status, error_msg=None):
        self.clone_status.append((repo_id, status, error_msg))

    def update_repo_local_path(self, repo_id, local_path):
        self.local_path[repo_id] = local_path

    def update_repo_status(self, repo_id, status, error_msg=None):
        self.status.append((repo_id, status, error_msg))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """HOME in tmp_path (the clone directory is ``~/.local/share/...``), the
    fake ``repos`` store, and a per-repo lock that records who took it."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repos = _Repos()
    monkeypatch.setattr("src.db.pg.repo_store", lambda: repos)
    locks: list[tuple[object, object]] = []

    @contextmanager
    def lock(conn, repo_id):
        locks.append((conn, repo_id))
        yield

    @contextmanager
    def dir_lock(conn, target_dir):
        locks.append((conn, Path(target_dir)))
        yield

    monkeypatch.setattr(pipeline_mod, "_repo_git_lock", lock)
    monkeypatch.setattr(pipeline_mod, "_checkout_dir_lock", dir_lock)
    return repos, locks


def _row(url: str, *, clone_status: str, repo_id: int = 388) -> dict:
    return {
        "id": repo_id, "url": url, "branch": BRANCH, "ssh_key_id": None,
        "clone_status": clone_status,
        "local_path": str(default_clone_dir(PROFILE, url)),
        "odoo_version": BRANCH,
    }


@pytest.mark.parametrize("status", ["error", "pending", "manual"])
def test_a_repo_whose_clone_never_succeeded_is_cloned_by_the_next_run(
    tmp_path, env, status,
):
    repos, locks = env
    url = f"file://{_origin(tmp_path, BRANCH)}"
    repo = _row(url, clone_status=status)
    conn = object()

    _ensure_repo_checkout(repo, conn, refresh=True, profile_name=PROFILE)

    target = Path(repo["local_path"])
    assert (target / "theme_x" / "__manifest__.py").is_file(), "the run must clone the repo"
    assert _git(target, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
    assert [s for _rid, s, _m in repos.clone_status] == ["pending", "cloned"]
    assert repos.local_path == {388: str(target)}
    assert locks == [(conn, 388), (conn, target)], (
        "the clone must run under the run connection's repo lock and its directory lock"
    )


def test_a_branch_missing_on_the_remote_is_one_unavailable_report_with_the_cause(
    tmp_path, env,
):
    """The #383 root cause: the registered branch does not exist (yet)."""
    repos, _locks = env
    url = f"file://{_origin(tmp_path, 'main')}"
    repo = _row(url, clone_status="error")

    with pytest.raises(RepoCheckoutUnavailable) as caught:
        _ensure_repo_checkout(repo, object(), refresh=True, profile_name=PROFILE)

    exc = caught.value
    assert not exc.busy
    assert exc.cause.startswith("clone failed:") and BRANCH in exc.cause
    assert isinstance(exc, FileNotFoundError)  # callers that caught it keep working
    final = repos.clone_status[-1]
    assert final[1] == "error" and final[2], "the row must keep the error and its message"
    assert not Path(repo["local_path"]).exists()


def test_no_fetch_keeps_the_run_offline(tmp_path, env):
    repos, locks = env
    url = f"file://{_origin(tmp_path, BRANCH)}"
    repo = _row(url, clone_status="error")

    with pytest.raises(RepoCheckoutUnavailable, match="--no-fetch"):
        _ensure_repo_checkout(repo, object(), refresh=False, profile_name=PROFILE)
    assert repos.clone_status == [] and locks == []
    assert not Path(repo["local_path"]).exists()


def test_a_cloned_repo_whose_directory_vanished_is_reported_not_recloned(tmp_path, env):
    """A missing mount must not be papered over by fresh clones on another disk."""
    repos, locks = env
    url = f"file://{_origin(tmp_path, BRANCH)}"
    repo = _row(url, clone_status="cloned")

    with pytest.raises(RepoCheckoutUnavailable, match="not re-cloned"):
        _ensure_repo_checkout(repo, object(), refresh=True, profile_name=PROFILE)
    assert repos.clone_status == [] and locks == []


def test_a_clone_another_worker_is_running_is_skipped_quietly(tmp_path, env, monkeypatch):
    repos, _locks = env
    url = f"file://{_origin(tmp_path, BRANCH)}"
    repo = _row(url, clone_status="pending")

    @contextmanager
    def busy(conn, repo_id):
        raise clone_ops.RepoGitLockBusy(f"repo id={repo_id} locked")
        yield  # pragma: no cover

    monkeypatch.setattr(pipeline_mod, "_repo_git_lock", busy)
    with pytest.raises(RepoCheckoutUnavailable) as caught:
        _ensure_repo_checkout(repo, object(), refresh=True, profile_name=PROFILE)
    assert caught.value.busy
    assert repos.clone_status == [], "the lock holder owns the row; nothing is written"


def test_a_manual_repo_registered_by_path_is_reported_never_replaced_by_a_clone(
    tmp_path, env,
):
    """apply-preset --base-dir registers the operator's checkout (clone_status
    'manual'). When that directory is missing at night (unmounted disk, renamed
    dir) the run must not clone the URL elsewhere and repoint local_path."""
    repos, locks = env
    url = f"file://{_origin(tmp_path, BRANCH)}"
    operator_dir = tmp_path / "mnt" / "odoo_19.0"
    repo = {**_row(url, clone_status="manual"), "local_path": str(operator_dir)}

    with pytest.raises(RepoCheckoutUnavailable, match="registered by hand") as caught:
        _ensure_repo_checkout(repo, object(), refresh=True, profile_name=PROFILE)

    assert not caught.value.busy
    assert repos.clone_status == [] and repos.local_path == {} and locks == []
    assert not default_clone_dir(PROFILE, url).exists(), "nothing may be cloned"
    assert repo["local_path"] == str(operator_dir)


def test_a_clone_directory_holding_another_repo_is_never_refreshed_for_this_one(
    tmp_path, env,
):
    """Two repos whose URLs end in the same slug map to one clone directory:
    the second must not 'refresh' the first one's checkout (wrong origin)."""
    repos, _locks = env
    first = tmp_path / "a"
    first.mkdir()
    first_url = f"file://{_origin(first, BRANCH)}"
    second = tmp_path / "b"
    second.mkdir()
    second_url = f"file://{_origin(second, BRANCH)}"
    target = default_clone_dir(PROFILE, first_url)
    assert target == default_clone_dir(PROFILE, second_url), "precondition: same slug"
    subprocess.run(["git", "clone", "-q", "-b", BRANCH, first_url, str(target)],
                   check=True, capture_output=True)
    first_head = _git(target, "rev-parse", "HEAD")
    repo = _row(second_url, clone_status="cloned", repo_id=389)
    assert repo["local_path"] == str(target), "registration recorded the shared directory"

    # Nightly: the directory exists, but it is the other repo's clone.
    with pytest.raises(RepoCheckoutUnavailable, match="holds a clone of") as caught:
        _ensure_repo_checkout(repo, object(), refresh=True, profile_name=PROFILE)
    assert first_url in caught.value.cause and not caught.value.busy

    # Cloner core (Web UI clone / clone-all): refuses to refresh it for this repo.
    outcome = clone_ops.ensure_checkout(
        repo, profile_name=PROFILE,
        lock=lambda _rid: contextlib.nullcontext(),
        dir_lock=lambda _d: contextlib.nullcontext(),
    )
    assert not outcome.ok and "holds a clone of" in (outcome.error or "")
    assert repos.clone_status[-1][1] == "error"

    assert _git(target, "config", "--get", "remote.origin.url") == first_url
    assert _git(target, "rev-parse", "HEAD") == first_head
    assert repos.local_path == {}, "the other repo's directory is never recorded as this one's"


def test_the_directory_lock_serializes_repos_that_share_a_clone_directory(tmp_path, env):
    """The per-repo locks of two same-slug repos differ; the directory lock is
    keyed by the resolved clone directory, so both contend for ONE lock - and
    a held directory lock makes the clone a quiet 'busy' skip."""
    _repos, _locks = env
    url_a = "git@github.com:Viindoo/themes.git"
    url_b = "https://example.com/other/themes.git"
    dir_a = default_clone_dir(PROFILE, url_a)
    assert clone_ops.repo_lock_id(388) != clone_ops.repo_lock_id(389)
    assert clone_ops.checkout_dir_lock_id(dir_a) == clone_ops.checkout_dir_lock_id(
        default_clone_dir(PROFILE, url_b))
    # One directory, however it is spelled (the key is the resolved path).
    assert clone_ops.checkout_dir_lock_id(dir_a) == clone_ops.checkout_dir_lock_id(
        dir_a.parent / ".." / dir_a.parent.name / dir_a.name)
    assert clone_ops.checkout_dir_lock_id(dir_a) != clone_ops.checkout_dir_lock_id(
        default_clone_dir("other_profile", url_a))


def test_a_held_directory_lock_skips_the_clone_quietly(tmp_path, env, monkeypatch):
    repos, _locks = env
    url = f"file://{_origin(tmp_path, BRANCH)}"
    repo = _row(url, clone_status="error")

    @contextmanager
    def busy_dir(conn, target_dir):
        raise clone_ops.RepoGitLockBusy(f"clone directory {target_dir} locked")
        yield  # pragma: no cover

    monkeypatch.setattr(pipeline_mod, "_checkout_dir_lock", busy_dir)
    with pytest.raises(RepoCheckoutUnavailable) as caught:
        _ensure_repo_checkout(repo, object(), refresh=True, profile_name=PROFILE)
    assert caught.value.busy
    assert repos.clone_status == [], "nothing is written while another repo's job runs"
    assert not Path(repo["local_path"]).exists()


def test_an_existing_checkout_is_left_to_the_pre_scan_refresh(tmp_path, env):
    repos, locks = env
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    repo = {**_row("file:///nowhere", clone_status="error"), "local_path": str(checkout)}

    _ensure_repo_checkout(repo, object(), refresh=True, profile_name=PROFILE)
    assert repo["local_path"] == str(checkout)
    assert repos.clone_status == [] and locks == []


# ---------------------------------------------------------------------------
# index_profile: the unavailable repo is reported, the others are indexed
# ---------------------------------------------------------------------------

def _counters() -> dict:
    return dict.fromkeys(
        ("modules", "views", "qweb", "embeddings", "js_patches", "owl_comps"), 0,
    )


@pytest.fixture
def profile_run(monkeypatch, env):
    """index_profile over two repos with the graph, the lock and the ledger faked;
    repo 388 has no checkout and its clone fails, repo 7 indexes."""
    repos, _locks = env
    rows = [
        {"id": 388, "url": "git@github.com:Viindoo/themes.git", "odoo_version": BRANCH},
        {"id": 7, "url": "https://example.com/ok.git", "odoo_version": BRANCH},
    ]

    class _Store:
        def get_repos_for_profile(self, name):
            return [dict(r) for r in rows]

        def get_ancestor_profile_names(self, name):
            return [name]

        def __getattr__(self, name):
            return getattr(repos, name)

    store = _Store()
    monkeypatch.setattr(pipeline_mod, "repo_store", lambda: store)
    monkeypatch.setattr("src.db.pg.repo_store", lambda: store)

    @contextmanager
    def no_lock(conn, profile):
        yield

    monkeypatch.setattr(pipeline_mod, "_indexer_lock", no_lock)
    monkeypatch.setattr(pipeline_mod, "_neo4j_creds", lambda: ("bolt://x", "u", "p"))
    monkeypatch.setattr(pipeline_mod, "Neo4jWriter", lambda *a, **k: MagicMock())
    monkeypatch.setattr("src.indexer.seed_patterns.run", lambda **kw: {"skipped": True})
    attention: dict[int, str] = {}
    ledger = MagicMock()
    ledger.set_lifecycle_attention.side_effect = lambda rid, text: attention.__setitem__(rid, text)
    monkeypatch.setattr(pipeline_mod, "_presence_store", lambda: ledger)
    indexed: list[int] = []

    def fake_index_repo(repo, writer, **kw):
        if repo["id"] == 388:
            raise RepoCheckoutUnavailable(
                repo, "clone failed: Remote branch 19.0 not found in upstream origin",
            )
        indexed.append(repo["id"])
        return _counters()

    monkeypatch.setattr(pipeline_mod, "_index_repo", fake_index_repo)
    return repos, attention, indexed


@pytest.mark.parametrize("max_workers", [1, 2])
def test_an_unavailable_repo_is_reported_without_a_traceback_and_the_rest_indexes(
    profile_run, caplog, monkeypatch, max_workers,
):
    repos, attention, indexed = profile_run
    if max_workers > 1:
        monkeypatch.setattr(pipeline_mod, "open_production_pg", lambda: MagicMock())
    caplog.set_level(logging.INFO, logger="src.indexer.pipeline")

    summary = pipeline_mod.index_profile(
        object(), profile_name=PROFILE, reconcile=False, max_workers=max_workers,
    )

    assert indexed == [7], "the healthy repo must still be indexed"
    lifecycle = summary["lifecycle"]
    assert lifecycle["needs_attention"] is True
    assert len(lifecycle["unavailable_repos"]) == 1
    assert lifecycle["unavailable_repos"][0].startswith("repo id=388 ")
    assert "Remote branch 19.0 not found" in attention[388]
    assert (388, "error") in [(rid, s) for rid, s, _m in repos.status]
    assert (7, "indexed") in [(rid, s) for rid, s, _m in repos.status]
    warnings = [r for r in caplog.records if "388" in r.getMessage()
                and r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert all(r.exc_info is None for r in caplog.records), "no traceback may be logged"


def test_the_cli_exits_3_and_names_the_unavailable_repo(capsys):
    from src.indexer.__main__ import EXIT_LIFECYCLE_ATTENTION, _lifecycle_exit_code

    lifecycle = pipeline_mod._finish_lifecycle({
        **pipeline_mod._empty_lifecycle(),
        "unavailable_repos": ["repo id=388 (themes.git): clone failed: ..."],
    })
    assert _lifecycle_exit_code(lifecycle) == EXIT_LIFECYCLE_ATTENTION
    err = capsys.readouterr().err
    assert "unavailable_repos: repo id=388 (themes.git)" in err
