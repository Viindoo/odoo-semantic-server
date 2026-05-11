# tests/test_repo_registry_delete.py
"""Unit tests for delete_profile, delete_repo, reset_repo_head_sha (M8 W0).

Uses clean_pg fixture + run_migrations to start with an empty schema.
"""
import pytest

from src.db.migrate import run_migrations
from src.db.repo_registry import (
    add_profile,
    add_repo,
    delete_profile,
    delete_repo,
    reset_repo_head_sha,
    update_repo_head_sha,
)

pytestmark = pytest.mark.postgres


class TestDeleteProfile:
    def test_delete_profile_cascades_repos(self, clean_pg):
        """delete_profile removes both the profile and its child repos via PG CASCADE."""
        run_migrations(clean_pg)
        conn = clean_pg

        profile_id = add_profile(conn, "test_profile", "17.0")
        repo_id_1 = add_repo(conn, profile_id, "https://example.com/r1", "17.0", "/tmp/repo1")
        repo_id_2 = add_repo(conn, profile_id, "https://example.com/r2", "17.0", "/tmp/repo2")

        result = delete_profile(conn, profile_id)

        # Function should return info about deleted repos
        assert "repos" in result
        assert len(result["repos"]) == 2
        basenames = {r["repo_basename"] for r in result["repos"]}
        assert "repo1" in basenames
        assert "repo2" in basenames

        # Profile row gone
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM profiles WHERE id = %s", (profile_id,))
            assert cur.fetchone() is None

        # Both repo rows gone (CASCADE)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM repos WHERE id = ANY(%s)", ([repo_id_1, repo_id_2],))
            assert cur.fetchall() == []

    def test_delete_profile_returns_odoo_version(self, clean_pg):
        """delete_profile returns odoo_version for each repo."""
        run_migrations(clean_pg)
        conn = clean_pg

        profile_id = add_profile(conn, "ver_profile", "16.0")
        add_repo(conn, profile_id, "https://example.com/r", "16.0", "/srv/odoo_16.0")

        result = delete_profile(conn, profile_id)
        assert result["repos"][0]["odoo_version"] == "16.0"
        assert result["repos"][0]["repo_basename"] == "odoo_16.0"

    def test_delete_profile_not_found_raises(self, clean_pg):
        """delete_profile raises ValueError for non-existent profile."""
        run_migrations(clean_pg)
        with pytest.raises(ValueError, match="profile id=9999 not found"):
            delete_profile(clean_pg, 9999)


class TestDeleteRepo:
    def test_delete_repo_returns_basename_version(self, clean_pg):
        """delete_repo deletes the row and returns repo_basename + odoo_version."""
        run_migrations(clean_pg)
        conn = clean_pg

        profile_id = add_profile(conn, "rp_profile", "17.0")
        local_path = "/home/user/git/viindoo_17.0"
        repo_id = add_repo(conn, profile_id, "https://example.com/r", "17.0", local_path)

        result = delete_repo(conn, repo_id)

        assert result["repo_basename"] == "viindoo_17.0"
        assert result["odoo_version"] == "17.0"

        # Row gone
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM repos WHERE id = %s", (repo_id,))
            assert cur.fetchone() is None

    def test_delete_repo_does_not_affect_other_repos(self, clean_pg):
        """delete_repo only removes the targeted repo, leaving sibling repos intact."""
        run_migrations(clean_pg)
        conn = clean_pg

        profile_id = add_profile(conn, "multi_repo_prof", "17.0")
        repo_id_a = add_repo(conn, profile_id, "https://example.com/a", "17.0", "/tmp/repo_a")
        repo_id_b = add_repo(conn, profile_id, "https://example.com/b", "17.0", "/tmp/repo_b")

        delete_repo(conn, repo_id_a)

        # repo_b still exists
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM repos WHERE id = %s", (repo_id_b,))
            assert cur.fetchone() is not None

    def test_delete_repo_not_found_raises(self, clean_pg):
        """delete_repo raises ValueError for non-existent repo."""
        run_migrations(clean_pg)
        with pytest.raises(ValueError, match="repo id=9999 not found"):
            delete_repo(clean_pg, 9999)


class TestResetRepoHeadSha:
    def test_reset_repo_head_sha(self, clean_pg):
        """reset_repo_head_sha sets head_sha to NULL."""
        run_migrations(clean_pg)
        conn = clean_pg

        profile_id = add_profile(conn, "sha_profile", "17.0")
        repo_id = add_repo(conn, profile_id, "https://example.com/r", "17.0", "/tmp/sha_repo")

        # Set a non-null head_sha first
        update_repo_head_sha(conn, repo_id, "abc123def456")

        with conn.cursor() as cur:
            cur.execute("SELECT head_sha FROM repos WHERE id = %s", (repo_id,))
            assert cur.fetchone()[0] == "abc123def456"

        # Reset it
        reset_repo_head_sha(conn, repo_id)

        with conn.cursor() as cur:
            cur.execute("SELECT head_sha FROM repos WHERE id = %s", (repo_id,))
            assert cur.fetchone()[0] is None

    def test_reset_repo_head_sha_not_found_raises(self, clean_pg):
        """reset_repo_head_sha raises ValueError for non-existent repo."""
        run_migrations(clean_pg)
        with pytest.raises(ValueError, match="repo id=9999 not found"):
            reset_repo_head_sha(clean_pg, 9999)
