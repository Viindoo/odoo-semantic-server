"""Integration tests for src.db.repo_registry CRUD."""
import pytest

from src.db.migrate import run_migrations
from src.db.repo_registry import (
    add_profile,
    add_repo,
    get_repo_head_sha,
    get_repos_for_profile,
    list_profiles,
    list_repos,
    update_repo_head_sha,
    update_repo_status,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def test_add_and_list_profile(migrated_pg):
    pid = add_profile(migrated_pg, name="viindoo_17", odoo_version="17.0")
    assert pid > 0
    profiles = list_profiles(migrated_pg)
    assert len(profiles) == 1
    assert profiles[0]["name"] == "viindoo_17"
    assert profiles[0]["odoo_version"] == "17.0"


def test_add_repo_under_profile(migrated_pg):
    pid = add_profile(migrated_pg, name="viindoo_17", odoo_version="17.0")
    rid = add_repo(
        migrated_pg, profile_id=pid,
        url="github.com/odoo/odoo", branch="17.0",
        local_path="/home/user/git/odoo_17.0",
    )
    assert rid > 0
    repos = get_repos_for_profile(migrated_pg, profile_name="viindoo_17")
    assert len(repos) == 1
    assert repos[0]["url"] == "github.com/odoo/odoo"
    assert repos[0]["status"] == "pending"


def test_list_repos_returns_all(migrated_pg):
    pid = add_profile(migrated_pg, "p1", "17.0")
    add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    add_repo(migrated_pg, pid, "github.com/c/d", "17.0", "/tmp/c")
    repos = list_repos(migrated_pg)
    assert len(repos) == 2


def test_update_repo_status(migrated_pg):
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    update_repo_status(migrated_pg, rid, status="indexed")
    repos = list_repos(migrated_pg)
    assert repos[0]["status"] == "indexed"
    assert repos[0]["last_indexed_at"] is not None


def test_update_repo_status_with_error(migrated_pg):
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    update_repo_status(migrated_pg, rid, status="error", error_msg="boom")
    repos = list_repos(migrated_pg)
    assert repos[0]["status"] == "error"
    assert repos[0]["error_msg"] == "boom"


def test_get_repos_for_unknown_profile_returns_empty(migrated_pg):
    assert get_repos_for_profile(migrated_pg, profile_name="nope") == []


def test_update_repo_status_unknown_id_raises(migrated_pg):
    with pytest.raises(ValueError, match="not found"):
        update_repo_status(migrated_pg, repo_id=99999, status="indexed")


def test_get_repo_head_sha_returns_none_when_unset(migrated_pg):
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    sha = get_repo_head_sha(migrated_pg, rid)
    assert sha is None


def test_update_and_get_repo_head_sha(migrated_pg):
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    update_repo_head_sha(migrated_pg, rid, "abc123def456")
    sha = get_repo_head_sha(migrated_pg, rid)
    assert sha == "abc123def456"


def test_update_repo_head_sha_bumps_last_indexed_at(migrated_pg):
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    # Get initial last_indexed_at (should be NULL)
    repos_before = list_repos(migrated_pg)
    initial_indexed_at = repos_before[0]["last_indexed_at"]
    assert initial_indexed_at is None
    # Update head_sha
    update_repo_head_sha(migrated_pg, rid, "abc123def456")
    # Get updated last_indexed_at (should now be set)
    repos_after = list_repos(migrated_pg)
    updated_indexed_at = repos_after[0]["last_indexed_at"]
    assert updated_indexed_at is not None
