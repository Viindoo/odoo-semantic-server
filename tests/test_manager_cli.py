"""Integration tests for `python -m src.manager` CLI."""
import os
import subprocess
import sys

import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _run(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "src.manager", *args],
        capture_output=True, text=True, env=env,
    )


def test_add_profile_prints_id(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    res = _run(
        ["add-profile", "viindoo_17", "--version", "17.0"],
        env_extra={"ODOO_SEMANTIC_CONF": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    assert "viindoo_17" in res.stdout

    import psycopg2
    conn = psycopg2.connect("postgresql://odoo_semantic:password@localhost:5432/odoo_semantic")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM profiles")
        rows = [r[0] for r in cur.fetchall()]
    conn.close()
    assert "viindoo_17" in rows


def test_add_repo_attaches_to_profile(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    env = {"ODOO_SEMANTIC_CONF": str(cfg)}

    repo_dir = tmp_path / "fake_repo"
    repo_dir.mkdir()

    _run(["add-profile", "viindoo_17", "--version", "17.0"], env_extra=env)
    res = _run([
        "add-repo", "--profile", "viindoo_17",
        "--url", "github.com/odoo/odoo", "--branch", "17.0",
        "--local-path", str(repo_dir),
    ], env_extra=env)
    assert res.returncode == 0, res.stderr

    import psycopg2
    conn = psycopg2.connect("postgresql://odoo_semantic:password@localhost:5432/odoo_semantic")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT url FROM repos")
        rows = [r[0] for r in cur.fetchall()]
    conn.close()
    assert "github.com/odoo/odoo" in rows


def test_list_shows_profile_and_repo(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    env = {"ODOO_SEMANTIC_CONF": str(cfg)}
    repo_dir = tmp_path / "y"
    repo_dir.mkdir()

    _run(["add-profile", "viindoo_17", "--version", "17.0"], env_extra=env)
    _run([
        "add-repo", "--profile", "viindoo_17",
        "--url", "github.com/x/y", "--branch", "17.0",
        "--local-path", str(repo_dir),
    ], env_extra=env)
    res = _run(["list"], env_extra=env)
    assert res.returncode == 0
    assert "viindoo_17" in res.stdout
    assert "github.com/x/y" in res.stdout


def test_unknown_subcommand_exits_nonzero(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    res = _run(["nope-cmd"], env_extra={"ODOO_SEMANTIC_CONF": str(cfg)})
    assert res.returncode != 0


def test_add_repo_unknown_profile_exits_2(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    res = _run(
        ["add-repo", "--profile", "does_not_exist",
         "--url", "x", "--branch", "17.0", "--local-path", str(tmp_path)],
        env_extra={"ODOO_SEMANTIC_CONF": str(cfg)},
    )
    assert res.returncode == 2
    assert "not found" in res.stderr


# --- New validation tests (I1, I2, I3) --------------------------------------

def test_add_profile_rejects_invalid_name(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    res = _run(
        ["add-profile", "bad name with space!", "--version", "17.0"],
        env_extra={"ODOO_SEMANTIC_CONF": str(cfg)},
    )
    assert res.returncode == 1
    assert "invalid" in res.stderr.lower()


def test_add_profile_rejects_invalid_version(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    res = _run(
        ["add-profile", "viindoo17", "--version", "latest"],
        env_extra={"ODOO_SEMANTIC_CONF": str(cfg)},
    )
    assert res.returncode == 1
    assert "version" in res.stderr.lower()


def test_add_profile_duplicate_friendly_error(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    env = {"ODOO_SEMANTIC_CONF": str(cfg)}
    _run(["add-profile", "dupe17", "--version", "17.0"], env_extra=env)
    res = _run(["add-profile", "dupe17", "--version", "17.0"], env_extra=env)
    assert res.returncode == 2
    assert "already exists" in res.stderr.lower()


def test_add_repo_rejects_nonexistent_path(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    env = {"ODOO_SEMANTIC_CONF": str(cfg)}
    _run(["add-profile", "okprofile17", "--version", "17.0"], env_extra=env)
    res = _run([
        "add-repo", "--profile", "okprofile17",
        "--url", "x", "--branch", "17.0",
        "--local-path", str(tmp_path / "no_such_dir"),
    ], env_extra=env)
    assert res.returncode == 1
    assert "does not exist" in res.stderr.lower()


# ---------------------------------------------------------------------------
# clone-profile unit tests (no Docker — mocked repo_store + subprocess)
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    """Build a minimal argparse-like Namespace for _cmd_clone_profile."""
    import argparse
    defaults = {
        "profile_name": "myprofile",
        "include_ancestors": False,
        "ssh_key_id": None,
        "max_parallel": 4,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _make_repo(repo_id: int, url: str, clone_status: str = "manual"):
    return {
        "id": repo_id,
        "url": url,
        "branch": "17.0",
        "clone_status": clone_status,
        "ssh_key_id": None,
        "profile_id": 1,
    }


def test_clone_profile_short_circuits_existing_file_paths(tmp_path):
    """file:// URL pointing to an existing directory → clone_status='cloned', no subprocess."""
    from unittest.mock import MagicMock, patch

    from src.manager.__main__ import _cmd_clone_profile

    repo = _make_repo(1, f"file://{tmp_path}")
    store = MagicMock()
    store.get_repos_for_profile.return_value = [repo]

    with (
        patch("src.db.pg.repo_store", return_value=store),
        patch("src.manager.__main__.repo_store", return_value=store),
        patch("subprocess.run") as mock_subproc,
    ):
        rc = _cmd_clone_profile(_make_args(), conn=None)

    assert rc == 0
    store.update_repo_local_path.assert_called_once_with(1, str(tmp_path))
    store.set_clone_status.assert_called_once_with(1, "cloned")
    mock_subproc.assert_not_called()


def test_clone_profile_skips_already_cloned(tmp_path):
    """Repos with clone_status='cloned' are filtered out before processing."""
    from unittest.mock import MagicMock, patch

    from src.manager.__main__ import _cmd_clone_profile

    repo = _make_repo(2, f"file://{tmp_path}", clone_status="cloned")
    store = MagicMock()
    store.get_repos_for_profile.return_value = [repo]

    with (
        patch("src.db.pg.repo_store", return_value=store),
        patch("src.manager.__main__.repo_store", return_value=store),
        patch("subprocess.run") as mock_subproc,
    ):
        rc = _cmd_clone_profile(_make_args(), conn=None)

    assert rc == 0
    store.update_repo_local_path.assert_not_called()
    store.set_clone_status.assert_not_called()
    mock_subproc.assert_not_called()


def test_clone_profile_max_parallel_caps_workers(tmp_path):
    """--max-parallel 2 must not exceed 2 concurrent subprocesses for 8 repos."""
    import threading
    from unittest.mock import MagicMock, patch

    from src.manager.__main__ import _cmd_clone_profile

    # Use a nonexistent file:// path so they go through subprocess path
    repos = [_make_repo(i, f"https://github.com/org/repo{i}.git") for i in range(1, 9)]
    store = MagicMock()
    store.get_repos_for_profile.return_value = repos

    lock = threading.Lock()
    counter = {"current": 0, "peak": 0}

    def fake_run(*args, **kwargs):
        with lock:
            counter["current"] += 1
            counter["peak"] = max(counter["peak"], counter["current"])
        import time
        time.sleep(0.02)  # small sleep to allow concurrency to show
        with lock:
            counter["current"] -= 1
        result = MagicMock()
        result.returncode = 0
        return result

    with (
        patch("src.db.pg.repo_store", return_value=store),
        patch("src.manager.__main__.repo_store", return_value=store),
        patch("subprocess.run", side_effect=fake_run),
    ):
        rc = _cmd_clone_profile(_make_args(max_parallel=2), conn=None)

    assert rc == 0
    assert counter["peak"] <= 2, f"peak concurrency was {counter['peak']}, expected ≤ 2"
