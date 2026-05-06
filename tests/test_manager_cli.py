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

    _run(["add-profile", "viindoo_17", "--version", "17.0"], env_extra=env)
    res = _run([
        "add-repo", "--profile", "viindoo_17",
        "--url", "github.com/odoo/odoo", "--branch", "17.0",
        "--local-path", "/home/user/git/odoo_17.0",
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
    _run(["add-profile", "viindoo_17", "--version", "17.0"], env_extra=env)
    _run([
        "add-repo", "--profile", "viindoo_17",
        "--url", "github.com/x/y", "--branch", "17.0",
        "--local-path", "/tmp/y",
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
