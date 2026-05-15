"""Integration tests for new CLI delete commands and list-webui-users."""
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


def _run(
    args: list[str],
    env_extra: dict | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run CLI command with optional environment overrides and stdin input.

    stdin_text: when set, fed to the subprocess via stdin. Required for
    commands that prompt via getpass (which falls back to reading stdin
    when no controlling TTY is attached — capture_output=True ensures no
    TTY).
    """
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "src.manager", *args],
        capture_output=True, text=True, env=env,
        input=stdin_text,
    )


def _setup_db_conf(tmp_path):
    """Create config file and return env dict."""
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    return {"ODOO_SEMANTIC_CONF": str(cfg)}


class TestCreateWebUIUserAdminFlag:
    """Test --admin flag for create-webui-user."""

    def test_create_user_admin_flag(self, migrated_pg, tmp_path, monkeypatch):
        """Create Web UI user with --admin flag sets is_admin=TRUE."""
        env = _setup_db_conf(tmp_path)
        # The CLI getpass.getpass() falls back to reading stdin when no TTY is
        # attached. Feed the password twice (prompt + confirm) via stdin.
        pw = "test_password_123\ntest_password_123\n"

        res = _run(
            ["create-webui-user", "testadmin", "--admin"],
            env_extra=env,
            stdin_text=pw,
        )
        assert res.returncode == 0, res.stderr
        assert "testadmin" in res.stdout

        # Verify in DB
        import psycopg2
        conn = psycopg2.connect(
            "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic"
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, is_admin FROM webui_users WHERE username = %s",
                ("testadmin",),
            )
            row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "testadmin"
        assert row[1] is True  # is_admin

    def test_create_user_without_admin_flag(self, migrated_pg, tmp_path, monkeypatch):
        """Create Web UI user without --admin flag sets is_admin=FALSE."""
        env = _setup_db_conf(tmp_path)
        pw = "test_password_123\ntest_password_123\n"

        res = _run(["create-webui-user", "regularuser"], env_extra=env, stdin_text=pw)
        assert res.returncode == 0, res.stderr

        # Verify in DB
        import psycopg2
        conn = psycopg2.connect(
            "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic"
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, is_admin FROM webui_users WHERE username = %s",
                ("regularuser",),
            )
            row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "regularuser"
        assert row[1] is False  # is_admin


class TestDeleteProfile:
    """Test delete-profile subcommand."""

    def test_delete_profile_not_found(self, migrated_pg, tmp_path):
        """delete-profile fails when profile doesn't exist."""
        env = _setup_db_conf(tmp_path)
        res = _run(["delete-profile", "nonexistent", "--yes"], env_extra=env)
        assert res.returncode == 2, f"Expected exit 2, got {res.returncode}: {res.stderr}"
        assert "not found" in res.stderr

    def test_delete_profile_with_yes_flag(self, migrated_pg, tmp_path):
        """delete-profile with --yes skips confirmation."""
        env = _setup_db_conf(tmp_path)
        _run(["add-profile", "profile1", "--version", "17.0"], env_extra=env)

        res = _run(["delete-profile", "profile1", "--yes"], env_extra=env)
        assert res.returncode == 0, res.stderr
        assert "Deleted" in res.stdout

        # Verify deleted
        import psycopg2
        conn = psycopg2.connect("postgresql://odoo_semantic:password@localhost:5432/odoo_semantic")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM profiles WHERE name = %s", ("profile1",))
            row = cur.fetchone()
        conn.close()
        assert row is None

    def test_delete_profile_cascades_repos(self, migrated_pg, tmp_path):
        """delete-profile cascades to delete child repos."""
        env = _setup_db_conf(tmp_path)
        repo_dir = tmp_path / "repo1"
        repo_dir.mkdir()

        _run(["add-profile", "profile1", "--version", "17.0"], env_extra=env)
        _run([
            "add-repo", "--profile", "profile1",
            "--url", "https://example.com/repo1", "--branch", "17.0",
            "--local-path", str(repo_dir),
        ], env_extra=env)

        res = _run(["delete-profile", "profile1", "--yes"], env_extra=env)
        assert res.returncode == 0

        # Verify repos also deleted
        import psycopg2
        conn = psycopg2.connect("postgresql://odoo_semantic:password@localhost:5432/odoo_semantic")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM repos WHERE url = %s", ("https://example.com/repo1",))
            row = cur.fetchone()
        conn.close()
        assert row is None


class TestDeleteRepo:
    """Test delete-repo subcommand."""

    def test_delete_repo_by_id(self, migrated_pg, tmp_path):
        """delete-repo can delete by numeric ID."""
        env = _setup_db_conf(tmp_path)
        repo_dir = tmp_path / "repo1"
        repo_dir.mkdir()

        _run(["add-profile", "profile1", "--version", "17.0"], env_extra=env)
        _run([
            "add-repo", "--profile", "profile1",
            "--url", "https://example.com/repo1", "--branch", "17.0",
            "--local-path", str(repo_dir),
        ], env_extra=env)

        # Get repo id
        import psycopg2
        conn = psycopg2.connect("postgresql://odoo_semantic:password@localhost:5432/odoo_semantic")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM repos WHERE url = %s", ("https://example.com/repo1",))
            repo_id = cur.fetchone()[0]
        conn.close()

        res = _run(["delete-repo", str(repo_id), "--yes"], env_extra=env)
        assert res.returncode == 0
        assert "Deleted" in res.stdout

    def test_delete_repo_by_url(self, migrated_pg, tmp_path):
        """delete-repo can delete by URL."""
        env = _setup_db_conf(tmp_path)
        repo_dir = tmp_path / "repo1"
        repo_dir.mkdir()

        _run(["add-profile", "profile1", "--version", "17.0"], env_extra=env)
        url = "https://example.com/repo1"
        _run([
            "add-repo", "--profile", "profile1",
            "--url", url, "--branch", "17.0",
            "--local-path", str(repo_dir),
        ], env_extra=env)

        res = _run(["delete-repo", url, "--yes"], env_extra=env)
        assert res.returncode == 0
        assert "Deleted" in res.stdout

    def test_delete_repo_not_found(self, migrated_pg, tmp_path):
        """delete-repo fails when repo doesn't exist."""
        env = _setup_db_conf(tmp_path)
        res = _run(["delete-repo", "99999", "--yes"], env_extra=env)
        assert res.returncode == 2, f"Expected exit 2, got {res.returncode}: {res.stderr}"
        assert "not found" in res.stderr


class TestDeleteWebUIUser:
    """Test delete-webui-user subcommand."""

    def test_delete_webui_user_with_yes_flag(self, migrated_pg, tmp_path, monkeypatch):
        """delete-webui-user with --yes skips confirmation."""
        env = _setup_db_conf(tmp_path)
        pw = "test_password_123\ntest_password_123\n"

        create_res = _run(
            ["create-webui-user", "testuser"], env_extra=env, stdin_text=pw
        )
        assert create_res.returncode == 0, create_res.stderr

        res = _run(["delete-webui-user", "testuser", "--yes"], env_extra=env)
        assert res.returncode == 0
        assert "Deleted" in res.stdout

        # Verify deleted
        import psycopg2
        conn = psycopg2.connect("postgresql://odoo_semantic:password@localhost:5432/odoo_semantic")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM webui_users WHERE username = %s", ("testuser",))
            row = cur.fetchone()
        conn.close()
        assert row is None

    def test_delete_webui_user_not_found(self, migrated_pg, tmp_path):
        """delete-webui-user fails when user doesn't exist."""
        env = _setup_db_conf(tmp_path)
        res = _run(["delete-webui-user", "nonexistent", "--yes"], env_extra=env)
        assert res.returncode == 2, f"Expected exit 2, got {res.returncode}: {res.stderr}"
        assert "not found" in res.stderr

    def test_delete_webui_user_invalid_username(self, migrated_pg, tmp_path):
        """delete-webui-user rejects invalid usernames."""
        env = _setup_db_conf(tmp_path)
        res = _run(["delete-webui-user", "!!!invalid!!!", "--yes"], env_extra=env)
        assert res.returncode == 1, f"Expected exit 1, got {res.returncode}"
        assert "invalid" in res.stderr


class TestListWebUIUsers:
    """Test list-webui-users subcommand."""

    def test_list_webui_users_empty(self, migrated_pg, tmp_path):
        """list-webui-users shows empty message when no users."""
        env = _setup_db_conf(tmp_path)
        res = _run(["list-webui-users"], env_extra=env)
        assert res.returncode == 0
        assert "no Web UI users" in res.stdout

    def test_list_webui_users_shows_table(self, migrated_pg, tmp_path, monkeypatch):
        """list-webui-users shows users in table format."""
        env = _setup_db_conf(tmp_path)
        pw = "test_password_123\ntest_password_123\n"

        r1 = _run(["create-webui-user", "user1"], env_extra=env, stdin_text=pw)
        assert r1.returncode == 0, r1.stderr
        r2 = _run(["create-webui-user", "user2", "--admin"], env_extra=env, stdin_text=pw)
        assert r2.returncode == 0, r2.stderr

        res = _run(["list-webui-users"], env_extra=env)
        assert res.returncode == 0
        assert "user1" in res.stdout
        assert "user2" in res.stdout
        assert "username" in res.stdout  # header
