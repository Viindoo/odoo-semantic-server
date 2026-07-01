# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-unit tests for the nightly pre-scan refresh (fetch-before-reindex).

NO Docker, NO live DB, NO Neo4j. Everything that touches Postgres, Neo4j, or the
network is mocked. Real git repos are built in ``tmp_path`` (git is a plain
subprocess, safe in the unit lane - mirrors tests/test_incremental.py).

Business rules under test (the fix's contract):

  1. CORE REGRESSION - a repo whose LOCAL HEAD == repos.head_sha (looks
     unchanged) but whose upstream advanced: after refresh advances the local
     clone A -> B, ``_index_repo`` with ``refresh=True`` picks up the new HEAD
     and scans the changed module (the module that was invisible now appears).
  2. FAIL-SAFE - a fetch failure (CalledProcessError / TimeoutExpired /
     FileNotFoundError / unexpected) is logged as WARNING and swallowed;
     ``_index_repo`` proceeds against the on-disk state and does NOT raise.
  3. ``refresh=False`` (CLI ``--no-fetch``) - ``refresh_repo`` is NOT called;
     the old local-only behaviour is preserved.
  4. Advisory-lock - the mutating refresh runs UNDER ``_repo_git_lock(pg_conn,
     repo_id)`` (same lock the cloner uses) with the correct ``repo_id``.
  5. SSH-key resolution - an ssh_key_id repo decrypts via the SSOT
     ``src.crypto.decrypt_private_key`` and passes the key to ``refresh_repo``;
     an HTTPS repo (ssh_key_id=None) passes ``private_key_pem=None``.
"""
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# NOTE: no pytestmark - this is the pure-unit lane. Never mark neo4j/postgres:
# the prod box shares a live Postgres+Neo4j and those markers can DROP prod data.


# ---------------------------------------------------------------------------
# git helpers (real repos, no network)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_repo(path: Path, branch: str = "17.0") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "checkout", "-b", branch)
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "T")
    (path / ".gitkeep").write_text("")
    _git(path, "add", ".gitkeep")
    _git(path, "commit", "-m", "init")
    return path


def _add_module(repo: Path, name: str) -> None:
    """Add a minimal Odoo module dir with a manifest and commit it."""
    module = repo / name
    module.mkdir(parents=True, exist_ok=True)
    (module / "__manifest__.py").write_text(
        f"{{'name': '{name}', 'version': '17.0.1.0.0', 'depends': []}}"
    )
    (module / "models").mkdir(exist_ok=True)
    (module / "models" / "__init__.py").write_text("")
    (module / "models" / f"{name}.py").write_text(textwrap.dedent(f"""
        from odoo import models, fields

        class FooModel(models.Model):
            _name = '{name}.foo'
            x = fields.Char()
    """).strip())
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Fake collaborators so _index_repo runs fully in-memory (no Neo4j / no pg / no embed)
# ---------------------------------------------------------------------------

class _FakeEmptyParseResult:
    """Duck-types every parse-result the _index_repo loop reads."""
    def __init__(self, module):
        self.module = module
        self.views = []
        self.reports = []
        self.qweb = []
        self.contributions = []
        self.patches = []
        self.components = []
        self.lint_violations = []
        self.test_classes = []


def _install_inmemory_parsers(stack, registry):
    """Patch build_registry / topological_sort / every parser used by the loop.

    Returns nothing - the caller uses ``registry`` to control which modules the
    scan yields. All parsers return empty results so the writer (a MagicMock)
    just records calls; there is zero DB / Neo4j / embedding activity.
    """
    import src.indexer.pipeline as _pipeline
    import src.indexer.pipeline_repo as _pr

    stack.enter_context(patch.object(_pipeline, "build_registry", return_value=registry))
    stack.enter_context(
        patch.object(_pipeline, "topological_sort", side_effect=lambda mods: list(mods.keys()))
    )
    # Parsers referenced by module identity inside pipeline_repo.
    for mod, fn in [
        (_pr.parser_python, "parse_module"),
        (_pr.parser_test, "parse_module"),
        (_pr.parser_xml, "parse_module"),
        (_pr.parser_qweb, "parse_module"),
        (_pr.parser_js, "parse_module_graph"),
    ]:
        stack.enter_context(
            patch.object(mod, fn, side_effect=lambda info, **kw: _FakeEmptyParseResult(info))
        )
    stack.enter_context(
        patch.object(
            _pr.parser_assets, "parse_assets",
            side_effect=lambda info: _FakeEmptyParseResult(info),
        )
    )
    stack.enter_context(
        patch.object(_pr.parser_js_test, "parse_module_js_tests", return_value=[])
    )
    for css_mod in (_pr.parser_css, _pr.parser_scss, _pr.parser_less):
        stack.enter_context(patch.object(css_mod, "parse_module", return_value=([], [])))


def _module_info(name: str, abs_path: str, version: str = "17.0"):
    """Build a real ModuleInfo the loop + live_paths comprehension can consume."""
    from src.indexer.models import ModuleInfo
    return ModuleInfo(
        name=name, odoo_version=version, repo="repo", path=abs_path, depends=[],
    )


# ---------------------------------------------------------------------------
# Rule 1 - CORE REGRESSION: upstream advanced, local HEAD looked unchanged
# ---------------------------------------------------------------------------

def test_refresh_picks_up_upstream_advance(tmp_path):
    """Local HEAD == stored head_sha, but refresh advances A -> B and the newly
    appeared module is scanned.

    Simulation: the repo starts at sha A (only mod_old on disk). ``repo_store``
    reports head_sha == A (looks unchanged -> old code would skip). We mock
    ``refresh_repo`` to actually commit mod_new (moving the working clone A -> B),
    exactly what ``git fetch + reset --hard origin/<branch>`` would do when the
    upstream branch advanced. With refresh=True the incremental diff A..B then
    sees mod_new and scans it.
    """
    import contextlib

    from src.indexer.pipeline import _index_repo

    repo = _make_repo(tmp_path / "repo", branch="17.0")
    _add_module(repo, "mod_old")
    sha_a = _head(repo)

    def _fake_refresh(local_path, branch, *, private_key_pem, timeout=None):
        # Simulate upstream having advanced: add mod_new and move HEAD A -> B.
        _add_module(Path(local_path), "mod_new")

    writer = MagicMock()
    repo_row = {
        "id": 7, "local_path": str(repo), "odoo_version": "17.0",
        "url": "file://local", "branch": "17.0", "ssh_key_id": None,
    }

    # repo_store(): first run reports head_sha == sha_a (looks unchanged).
    fake_store = MagicMock()
    fake_store.get_repo_head_sha.return_value = sha_a
    # No cross-repo dep propagation targets.
    fake_store.get_repo_ids_by_local_path_basenames.return_value = []

    # A non-None pg_conn triggers the incremental (head_sha) path; the advisory
    # lock is a no-op context manager so we don't need a real connection.
    pg_conn = object()

    with contextlib.ExitStack() as stack:
        import src.indexer.pipeline as _pipeline
        stack.enter_context(patch.object(_pipeline, "repo_store", return_value=fake_store))
        stack.enter_context(
            patch("src.git_utils.refresh_repo", side_effect=_fake_refresh)
        )
        # advisory lock -> no-op (no real pg); assert it was entered with repo id.
        lock_cm = MagicMock()
        lock_cm.__enter__ = MagicMock(return_value=None)
        lock_cm.__exit__ = MagicMock(return_value=False)
        lock = stack.enter_context(
            patch.object(_pipeline, "_repo_git_lock", return_value=lock_cm)
        )
        # cross_repo import happens inside _index_repo on the incremental path.
        stack.enter_context(
            patch("src.indexer.cross_repo.find_dependent_repos", return_value=[])
        )

        # Registry is built AFTER refresh, so it must reflect BOTH modules on disk.
        registry = {
            "17.0": {
                "mod_old": _module_info("mod_old", str(repo / "mod_old")),
                "mod_new": _module_info("mod_new", str(repo / "mod_new")),
            }
        }
        _install_inmemory_parsers(stack, registry)

        counters = _index_repo(repo_row, writer, pg_conn=pg_conn, refresh=True)

    # mod_new appeared upstream -> incremental diff A..B must include exactly it.
    assert counters["modules"] == 1, (
        "refresh must advance HEAD so the newly-merged module is scanned; "
        f"got {counters['modules']}"
    )
    # The advisory lock was entered with the repo's id (Rule 4).
    lock.assert_called_once_with(pg_conn, 7)
    # head_sha advanced to the post-refresh HEAD (B), not left at A.
    fake_store.update_repo_head_sha.assert_called_once()
    advanced_to = fake_store.update_repo_head_sha.call_args.args[1]
    assert advanced_to == _head(repo) != sha_a


# ---------------------------------------------------------------------------
# Rule 2 - FAIL-SAFE: fetch failure is non-fatal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(128, ["git", "fetch"]),
    subprocess.TimeoutExpired(["git", "fetch"], 30),
    FileNotFoundError("not a git repo"),
    OSError("some unexpected subprocess error"),
])
def test_fetch_failure_is_non_fatal(tmp_path, caplog, exc):
    """refresh_repo raises a git failure -> refresh_before_scan logs WARNING and does
    NOT raise; _index_repo proceeds against the on-disk state.

    NOTE: RuntimeError is deliberately NOT in this list. refresh_repo never raises
    RuntimeError (it raises CalledProcessError/TimeoutExpired/FileNotFoundError), so
    a RuntimeError inside the `with lock_cm:` block can ONLY be lock contention -
    covered by test_lock_contention_is_info_not_warning_and_non_fatal.
    """
    import contextlib
    import logging

    from src.indexer.pipeline import _index_repo

    repo = _make_repo(tmp_path / "repo", branch="17.0")
    _add_module(repo, "mod_ondisk")

    writer = MagicMock()
    repo_row = {
        "id": 3, "local_path": str(repo), "odoo_version": "17.0",
        "url": "file://local", "branch": "17.0", "ssh_key_id": None,
    }
    fake_store = MagicMock()
    fake_store.get_repo_head_sha.return_value = None  # first run -> full reindex
    pg_conn = object()

    with contextlib.ExitStack() as stack:
        import src.indexer.pipeline as _pipeline
        stack.enter_context(patch.object(_pipeline, "repo_store", return_value=fake_store))
        stack.enter_context(patch("src.git_utils.refresh_repo", side_effect=exc))
        lock_cm = MagicMock()
        lock_cm.__enter__ = MagicMock(return_value=None)
        lock_cm.__exit__ = MagicMock(return_value=False)
        stack.enter_context(patch.object(_pipeline, "_repo_git_lock", return_value=lock_cm))
        registry = {"17.0": {"mod_ondisk": _module_info("mod_ondisk", str(repo / "mod_ondisk"))}}
        _install_inmemory_parsers(stack, registry)

        with caplog.at_level(logging.WARNING, logger="src.indexer.pipeline"):
            # Must NOT raise despite the fetch error.
            counters = _index_repo(repo_row, writer, pg_conn=pg_conn, refresh=True)

    assert counters["modules"] == 1, "on-disk module must still be indexed after a fetch failure"
    assert "fetch failed" in caplog.text.lower(), "a WARNING must record the fetch failure"


# ---------------------------------------------------------------------------
# Rule 2b - BENIGN lock contention: logged at INFO (not WARNING), non-fatal
# ---------------------------------------------------------------------------

def test_lock_contention_is_info_not_warning_and_non_fatal(tmp_path, caplog):
    """A concurrent clone-all holding the per-repo lock makes _repo_git_lock raise
    RuntimeError at acquisition (i.e. at `with lock_cm:` __enter__). That is EXPECTED
    contention: it must be logged at INFO (never WARNING - operators must not be
    alarmed), refresh_repo must NOT run, and indexing proceeds on-disk (non-fatal).
    """
    import contextlib
    import logging

    from src.indexer.pipeline import _index_repo

    repo = _make_repo(tmp_path / "repo", branch="17.0")
    _add_module(repo, "mod_ondisk")

    writer = MagicMock()
    repo_row = {
        "id": 88, "local_path": str(repo), "odoo_version": "17.0",
        "url": "file://local", "branch": "17.0", "ssh_key_id": None,
    }
    fake_store = MagicMock()
    fake_store.get_repo_head_sha.return_value = None  # first run -> full reindex
    pg_conn = object()

    with contextlib.ExitStack() as stack:
        import src.indexer.pipeline as _pipeline
        stack.enter_context(patch.object(_pipeline, "repo_store", return_value=fake_store))
        refresh = stack.enter_context(patch("src.git_utils.refresh_repo"))
        # Real _repo_git_lock is a @contextmanager that raises RuntimeError INSIDE the
        # body when the lock is already held -> the raise surfaces at __enter__, i.e.
        # when the `with` statement is entered. Model that with a CM whose __enter__
        # raises (NOT the factory call - the factory returns the CM fine).
        lock_cm = MagicMock()
        lock_cm.__enter__ = MagicMock(
            side_effect=RuntimeError("Git mutation already in progress for repo id=88")
        )
        lock_cm.__exit__ = MagicMock(return_value=False)
        stack.enter_context(patch.object(_pipeline, "_repo_git_lock", return_value=lock_cm))
        registry = {"17.0": {"mod_ondisk": _module_info("mod_ondisk", str(repo / "mod_ondisk"))}}
        _install_inmemory_parsers(stack, registry)

        with caplog.at_level(logging.INFO, logger="src.indexer.pipeline"):
            counters = _index_repo(repo_row, writer, pg_conn=pg_conn, refresh=True)

    # Non-fatal: on-disk module still indexed; the fetch itself never ran.
    assert counters["modules"] == 1
    refresh.assert_not_called()

    # The contention line must be INFO, NOT WARNING.
    contention_records = [
        r for r in caplog.records
        if "another git op in progress" in r.getMessage()
    ]
    assert contention_records, "contention must be logged (INFO) so operators see the skip"
    assert all(r.levelno == logging.INFO for r in contention_records), (
        "benign lock contention must log at INFO, never WARNING"
    )
    # And it must NOT be recorded on the alarming 'failed'/'unexpectedly' wording.
    assert "fetch failed" not in caplog.text.lower(), (
        "benign contention must not use the WARNING 'fetch failed' wording"
    )


def test_key_resolution_runtimeerror_is_warning_not_contention(tmp_path, caplog):
    """Finding #3 regression guard: a RuntimeError while RESOLVING the SSH key (e.g.
    missing FERNET_KEY) must land on the WARNING path and must NOT be mislabeled as
    benign lock contention (INFO). The key is resolved BEFORE the lock, so the
    contention branch can never see this error. refresh_repo must NOT run.
    """
    import contextlib
    import logging

    from src.indexer.pipeline_repo import refresh_before_scan

    repo = _make_repo(tmp_path / "repo", branch="17.0")
    repo_row = {
        "id": 91, "local_path": str(repo), "url": "git@github.com:o/r.git",
        "branch": "17.0", "ssh_key_id": 5,
    }
    pg_conn = object()

    with contextlib.ExitStack() as stack:
        import src.indexer.pipeline as _pipeline
        refresh = stack.enter_context(patch("src.git_utils.refresh_repo"))
        lock = stack.enter_context(patch.object(_pipeline, "_repo_git_lock"))
        # resolve_ssh_key_pem raises RuntimeError (simulating FERNET/decrypt failure).
        stack.enter_context(
            patch(
                "src.ssh_key_resolve.resolve_ssh_key_pem",
                side_effect=RuntimeError("FERNET_KEY is not set"),
            )
        )
        with caplog.at_level(logging.INFO, logger="src.indexer.pipeline"):
            refresh_before_scan(repo_row, pg_conn)  # must not raise

    # Key error is a real failure -> WARNING wording, never the INFO contention line.
    assert "fetch failed" in caplog.text.lower(), (
        "a key-resolution RuntimeError must land on the WARNING 'fetch failed' path"
    )
    assert "another git op in progress" not in caplog.text.lower(), (
        "a key-resolution RuntimeError must NOT be mislabeled as benign contention"
    )
    # Never reach the lock or the fetch when key resolution failed.
    refresh.assert_not_called()
    lock.assert_not_called()


# ---------------------------------------------------------------------------
# Rule 3 - refresh=False / --no-fetch: refresh_repo is NOT called
# ---------------------------------------------------------------------------

def test_no_fetch_does_not_call_refresh_repo(tmp_path):
    """refresh=False -> refresh_repo is never invoked; local-only behaviour."""
    import contextlib

    from src.indexer.pipeline import _index_repo

    repo = _make_repo(tmp_path / "repo", branch="17.0")
    _add_module(repo, "mod_only")

    writer = MagicMock()
    repo_row = {
        "id": 1, "local_path": str(repo), "odoo_version": "17.0",
        "url": "file://local", "branch": "17.0", "ssh_key_id": None,
    }
    fake_store = MagicMock()
    fake_store.get_repo_head_sha.return_value = None
    pg_conn = object()

    with contextlib.ExitStack() as stack:
        import src.indexer.pipeline as _pipeline
        stack.enter_context(patch.object(_pipeline, "repo_store", return_value=fake_store))
        refresh = stack.enter_context(patch("src.git_utils.refresh_repo"))
        registry = {"17.0": {"mod_only": _module_info("mod_only", str(repo / "mod_only"))}}
        _install_inmemory_parsers(stack, registry)

        _index_repo(repo_row, writer, pg_conn=pg_conn, refresh=False)

    refresh.assert_not_called()


# ---------------------------------------------------------------------------
# Rule 5 - SSH-key resolution (SSOT decrypt) + HTTPS None-key
# ---------------------------------------------------------------------------

def test_ssh_repo_decrypts_key_via_ssot_and_passes_to_refresh(tmp_path):
    """An SSH-url repo with a usable key resolves the PEM via the shared SSOT and
    passes it to refresh_repo; the advisory lock is used."""
    import contextlib

    from src.indexer.pipeline_repo import refresh_before_scan

    repo = _make_repo(tmp_path / "repo", branch="17.0")
    repo_row = {
        "id": 42, "local_path": str(repo), "url": "git@github.com:o/r.git",
        "branch": "17.0", "ssh_key_id": 9,
    }
    pg_conn = object()

    with contextlib.ExitStack() as stack:
        import src.indexer.pipeline as _pipeline
        # Patch the shared resolver (used by both refresh + cloner) at its call site.
        resolve = stack.enter_context(
            patch("src.ssh_key_resolve.resolve_ssh_key_pem", return_value=b"PEM-BYTES")
        )
        refresh = stack.enter_context(patch("src.git_utils.refresh_repo"))
        lock_cm = MagicMock()
        lock_cm.__enter__ = MagicMock(return_value=None)
        lock_cm.__exit__ = MagicMock(return_value=False)
        lock = stack.enter_context(patch.object(_pipeline, "_repo_git_lock", return_value=lock_cm))

        refresh_before_scan(repo_row, pg_conn)

    resolve.assert_called_once_with(repo_row)
    refresh.assert_called_once()
    assert refresh.call_args.kwargs["private_key_pem"] == b"PEM-BYTES"
    lock.assert_called_once_with(pg_conn, 42)


def test_https_repo_passes_none_key(tmp_path):
    """An HTTPS repo (ssh_key_id=None) resolves to private_key_pem=None (no SSH
    credential); refresh_repo is called with None."""
    import contextlib

    from src.indexer.pipeline_repo import refresh_before_scan

    repo = _make_repo(tmp_path / "repo", branch="17.0")
    repo_row = {
        "id": 5, "local_path": str(repo), "url": "https://github.com/o/r.git",
        "branch": "17.0", "ssh_key_id": None,
    }

    with contextlib.ExitStack() as stack:
        # Let the REAL resolver run (https -> None, no auth_store/decrypt touched).
        auth = stack.enter_context(patch("src.db.pg.auth_store"))
        refresh = stack.enter_context(patch("src.git_utils.refresh_repo"))
        # No pg_conn -> no advisory lock needed; the fetch still happens once.
        refresh_before_scan(repo_row, None)

    auth.assert_not_called()
    refresh.assert_called_once()
    assert refresh.call_args.kwargs["private_key_pem"] is None


def test_ssh_url_without_key_surfaces_warning_and_skips(tmp_path, caplog):
    """Finding #1 regression guard: an SSH-scheme URL with ssh_key_id=None must NOT
    run a keyless SSH fetch. resolve_ssh_key_pem raises SshKeyUnavailable ->
    refresh_before_scan surfaces a WARNING and skips (index on-disk); refresh_repo
    is never called (no doomed keyless fetch, no silent stale-forever)."""
    import contextlib
    import logging

    from src.indexer.pipeline_repo import refresh_before_scan

    repo = _make_repo(tmp_path / "repo", branch="17.0")
    repo_row = {
        "id": 7, "local_path": str(repo), "url": "git@github.com:o/r.git",
        "branch": "17.0", "ssh_key_id": None,
    }
    pg_conn = object()

    with contextlib.ExitStack() as stack:
        import src.indexer.pipeline as _pipeline
        # Let the REAL resolver run: SSH url + no ssh_key_id -> SshKeyUnavailable.
        refresh = stack.enter_context(patch("src.git_utils.refresh_repo"))
        lock = stack.enter_context(patch.object(_pipeline, "_repo_git_lock"))
        with caplog.at_level(logging.INFO, logger="src.indexer.pipeline"):
            refresh_before_scan(repo_row, pg_conn)  # must not raise

    refresh.assert_not_called()  # NO keyless SSH fetch
    lock.assert_not_called()     # never reach the lock
    # Surfaced (WARNING), not swallowed at INFO / not the benign-contention line.
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no ssh_key_id" in r.getMessage() for r in warn_records), (
        "SSH-url-without-key must be surfaced as a WARNING, not run keyless"
    )
    assert "another git op in progress" not in caplog.text.lower()


def test_missing_branch_skips_refresh(tmp_path):
    """A repo row with no branch cannot reset --hard origin/<branch> -> skip
    (warn) rather than crash the nightly run."""
    import contextlib

    from src.indexer.pipeline_repo import refresh_before_scan

    repo = _make_repo(tmp_path / "repo", branch="17.0")
    repo_row = {"id": 1, "local_path": str(repo), "url": "x", "branch": None, "ssh_key_id": None}

    with contextlib.ExitStack() as stack:
        refresh = stack.enter_context(patch("src.git_utils.refresh_repo"))
        refresh_before_scan(repo_row, None)

    refresh.assert_not_called()
