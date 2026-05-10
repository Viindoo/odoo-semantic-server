"""Integration tests for src.db.repo_registry CRUD."""
import pytest

from src.db.migrate import run_migrations
from src.db.repo_registry import (
    add_profile,
    add_repo,
    get_repo_by_id,
    get_repo_head_sha,
    get_repos_by_clone_status,
    get_repos_for_profile,
    list_profiles,
    list_repos,
    set_clone_status,
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


def test_add_repo_with_ssh_key_id_persists(migrated_pg):
    """Test add_repo with ssh_key_id kwarg persists correctly."""
    pid = add_profile(migrated_pg, "p1", "17.0")
    # First create an ssh_key_pairs row to reference
    with migrated_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted) "
            "VALUES (%s, %s, %s) RETURNING id",
            ("test_key", "ssh-ed25519 AAAA...", "encrypted_data"),
        )
        ssh_key_id = cur.fetchone()[0]
    migrated_pg.commit()
    # Add repo with ssh_key_id
    add_repo(
        migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a", ssh_key_id=ssh_key_id
    )
    # Verify persisted
    repos = list_repos(migrated_pg)
    assert len(repos) == 1
    assert repos[0]["ssh_key_id"] == ssh_key_id


def test_add_repo_default_clone_status_is_manual(migrated_pg):
    """Test add_repo without clone_status kwarg defaults to 'manual'."""
    pid = add_profile(migrated_pg, "p1", "17.0")
    add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    repos = list_repos(migrated_pg)
    assert repos[0]["clone_status"] == "manual"


def test_set_clone_status_lifecycle(migrated_pg):
    """Test set_clone_status transitions: manual -> pending -> cloned."""
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    # Default is 'manual'
    assert list_repos(migrated_pg)[0]["clone_status"] == "manual"
    # Transition to pending
    set_clone_status(migrated_pg, rid, "pending")
    assert list_repos(migrated_pg)[0]["clone_status"] == "pending"
    # Transition to cloned
    set_clone_status(migrated_pg, rid, "cloned")
    assert list_repos(migrated_pg)[0]["clone_status"] == "cloned"


def test_set_clone_status_error_with_msg(migrated_pg):
    """Test set_clone_status with error status stores message in clone_error_msg.

    Cloner errors go to clone_error_msg (NOT error_msg) to avoid overwriting
    indexer errors. See ADR-0008 D7 and W4 Opus review fix 1.
    """
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    error_msg = "git clone failed: timeout"
    set_clone_status(migrated_pg, rid, "error", error_msg=error_msg)
    repos = list_repos(migrated_pg)
    assert repos[0]["clone_status"] == "error"
    assert repos[0]["clone_error_msg"] == error_msg


def test_get_repos_by_clone_status_filters_correctly(migrated_pg):
    """Test get_repos_by_clone_status returns only matching status."""
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid1 = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    rid2 = add_repo(
        migrated_pg, pid, "github.com/c/d", "17.0", "/tmp/c", clone_status="pending"
    )
    rid3 = add_repo(
        migrated_pg, pid, "github.com/e/f", "17.0", "/tmp/e", clone_status="cloned"
    )
    # Query for 'pending' repos
    pending_repos = get_repos_by_clone_status(migrated_pg, "p1", "pending")
    assert len(pending_repos) == 1
    assert pending_repos[0]["id"] == rid2
    # Query for 'manual' repos
    manual_repos = get_repos_by_clone_status(migrated_pg, "p1", "manual")
    assert len(manual_repos) == 1
    assert manual_repos[0]["id"] == rid1
    # Query for 'cloned' repos
    cloned_repos = get_repos_by_clone_status(migrated_pg, "p1", "cloned")
    assert len(cloned_repos) == 1
    assert cloned_repos[0]["id"] == rid3


def test_indexer_error_survives_cloner_success(migrated_pg):
    """Regression guard: cloner success must NOT clear an existing indexer error.

    Before the Fix-1 fix, set_clone_status wrote to repos.error_msg, so a successful
    clone (error_msg=None) would silently clear a prior indexer failure message.
    """
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    # Simulate a prior indexer failure
    update_repo_status(migrated_pg, rid, status="error", error_msg="OSError: indexer fail")
    # Cloner finishes successfully
    set_clone_status(migrated_pg, rid, "cloned")
    # Verify: indexer error_msg preserved; clone_error_msg NULL on success
    repo = get_repo_by_id(migrated_pg, rid)
    assert repo is not None
    assert repo["error_msg"] == "OSError: indexer fail", (
        "Indexer error_msg must be preserved after cloner success"
    )
    assert repo["clone_error_msg"] is None, (
        "clone_error_msg should be NULL on successful clone"
    )


def test_ssh_key_delete_sets_repo_ssh_key_id_null(migrated_pg):
    """FK ON DELETE SET NULL: deleting an ssh_key_pair NULLs repos.ssh_key_id, not CASCADE."""
    pid = add_profile(migrated_pg, "p1", "17.0")
    with migrated_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted) "
            "VALUES (%s, %s, %s) RETURNING id",
            ("deploy", "ssh-ed25519 AAAA...", "enc"),
        )
        key_id = cur.fetchone()[0]
    migrated_pg.commit()
    rid = add_repo(
        migrated_pg, pid, "git@host:org/repo.git", "main", "/tmp/r",
        ssh_key_id=key_id,
    )
    # Confirm ssh_key_id set
    repo_before = get_repo_by_id(migrated_pg, rid)
    assert repo_before is not None
    assert repo_before["ssh_key_id"] == key_id
    # Delete the SSH key
    with migrated_pg.cursor() as cur:
        cur.execute("DELETE FROM ssh_key_pairs WHERE id = %s", (key_id,))
    migrated_pg.commit()
    # Repo row must still exist with ssh_key_id NULLed (not cascaded away)
    repo_after = get_repo_by_id(migrated_pg, rid)
    assert repo_after is not None, "Repo row must not be deleted when ssh key is removed"
    assert repo_after["ssh_key_id"] is None, (
        "repos.ssh_key_id must be NULL after FK ON DELETE SET NULL"
    )
