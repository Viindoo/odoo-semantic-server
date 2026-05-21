# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for src.db.repo_registry CRUD."""
import pytest

from src.db.migrate import run_migrations
from src.db.pg import repo_store

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def test_add_and_list_profile(migrated_pg):
    pid = repo_store().add_profile(name="viindoo_17", odoo_version="17.0")
    assert pid > 0
    profiles = repo_store().list_profiles()
    # Migration 0004 seeds 12 root profiles; this test adds one more.
    named = [p for p in profiles if p["name"] == "viindoo_17"]
    assert len(named) == 1
    assert named[0]["odoo_version"] == "17.0"


def test_add_repo_under_profile(migrated_pg):
    pid = repo_store().add_profile(name="viindoo_17", odoo_version="17.0")
    rid = repo_store().add_repo(
        profile_id=pid,
        url="github.com/odoo/odoo", branch="17.0",
        local_path="/home/user/git/odoo_17.0",
    )
    assert rid > 0
    repos = repo_store().get_repos_for_profile(profile_name="viindoo_17")
    assert len(repos) == 1
    assert repos[0]["url"] == "github.com/odoo/odoo"
    assert repos[0]["status"] == "pending"


def test_list_repos_returns_all(migrated_pg):
    pid = repo_store().add_profile("p1", "17.0")
    repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    repo_store().add_repo(pid, "github.com/c/d", "17.0", "/tmp/c")
    repos = repo_store().list_repos()
    assert len(repos) == 2


def test_update_repo_status(migrated_pg):
    pid = repo_store().add_profile("p1", "17.0")
    rid = repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    repo_store().update_repo_status(rid, status="indexed")
    repos = repo_store().list_repos()
    assert repos[0]["status"] == "indexed"
    assert repos[0]["last_indexed_at"] is not None


def test_update_repo_status_with_error(migrated_pg):
    pid = repo_store().add_profile("p1", "17.0")
    rid = repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    repo_store().update_repo_status(rid, status="error", error_msg="boom")
    repos = repo_store().list_repos()
    assert repos[0]["status"] == "error"
    assert repos[0]["error_msg"] == "boom"


def test_get_repos_for_unknown_profile_returns_empty(migrated_pg):
    assert repo_store().get_repos_for_profile(profile_name="nope") == []


def test_update_repo_status_unknown_id_raises(migrated_pg):
    with pytest.raises(ValueError, match="not found"):
        repo_store().update_repo_status(repo_id=99999, status="indexed")


def test_get_repo_head_sha_returns_none_when_unset(migrated_pg):
    pid = repo_store().add_profile("p1", "17.0")
    rid = repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    sha = repo_store().get_repo_head_sha(rid)
    assert sha is None


def test_update_and_get_repo_head_sha(migrated_pg):
    pid = repo_store().add_profile("p1", "17.0")
    rid = repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    repo_store().update_repo_head_sha(rid, "abc123def456")
    sha = repo_store().get_repo_head_sha(rid)
    assert sha == "abc123def456"


def test_update_repo_head_sha_bumps_last_indexed_at(migrated_pg):
    pid = repo_store().add_profile("p1", "17.0")
    rid = repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    # Get initial last_indexed_at (should be NULL)
    repos_before = repo_store().list_repos()
    initial_indexed_at = repos_before[0]["last_indexed_at"]
    assert initial_indexed_at is None
    # Update head_sha
    repo_store().update_repo_head_sha(rid, "abc123def456")
    # Get updated last_indexed_at (should now be set)
    repos_after = repo_store().list_repos()
    updated_indexed_at = repos_after[0]["last_indexed_at"]
    assert updated_indexed_at is not None


def test_add_repo_with_ssh_key_id_persists(migrated_pg):
    """Test add_repo with ssh_key_id kwarg persists correctly."""
    pid = repo_store().add_profile("p1", "17.0")
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
    repo_store().add_repo(
        pid, "github.com/a/b", "17.0", "/tmp/a", ssh_key_id=ssh_key_id
    )
    # Verify persisted
    repos = repo_store().list_repos()
    assert len(repos) == 1
    assert repos[0]["ssh_key_id"] == ssh_key_id


def test_add_repo_default_clone_status_is_manual(migrated_pg):
    """Test add_repo without clone_status kwarg defaults to 'manual'."""
    pid = repo_store().add_profile("p1", "17.0")
    repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    repos = repo_store().list_repos()
    assert repos[0]["clone_status"] == "manual"


def test_set_clone_status_lifecycle(migrated_pg):
    """Test set_clone_status transitions: manual -> pending -> cloned."""
    pid = repo_store().add_profile("p1", "17.0")
    rid = repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    # Default is 'manual'
    assert repo_store().list_repos()[0]["clone_status"] == "manual"
    # Transition to pending
    repo_store().set_clone_status(rid, "pending")
    assert repo_store().list_repos()[0]["clone_status"] == "pending"
    # Transition to cloned
    repo_store().set_clone_status(rid, "cloned")
    assert repo_store().list_repos()[0]["clone_status"] == "cloned"


def test_set_clone_status_error_with_msg(migrated_pg):
    """Test set_clone_status with error status stores message in clone_error_msg.

    Cloner errors go to clone_error_msg (NOT error_msg) to avoid overwriting
    indexer errors. See ADR-0008 D7 and W4 Opus review fix 1.
    """
    pid = repo_store().add_profile("p1", "17.0")
    rid = repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    error_msg = "git clone failed: timeout"
    repo_store().set_clone_status(rid, "error", error_msg=error_msg)
    repos = repo_store().list_repos()
    assert repos[0]["clone_status"] == "error"
    assert repos[0]["clone_error_msg"] == error_msg


def test_get_repos_by_clone_status_filters_correctly(migrated_pg):
    """Test get_repos_by_clone_status returns only matching status."""
    pid = repo_store().add_profile("p1", "17.0")
    rid1 = repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    rid2 = repo_store().add_repo(
        pid, "github.com/c/d", "17.0", "/tmp/c", clone_status="pending"
    )
    rid3 = repo_store().add_repo(
        pid, "github.com/e/f", "17.0", "/tmp/e", clone_status="cloned"
    )
    # Query for 'pending' repos
    pending_repos = repo_store().get_repos_by_clone_status("p1", "pending")
    assert len(pending_repos) == 1
    assert pending_repos[0]["id"] == rid2
    # Query for 'manual' repos
    manual_repos = repo_store().get_repos_by_clone_status("p1", "manual")
    assert len(manual_repos) == 1
    assert manual_repos[0]["id"] == rid1
    # Query for 'cloned' repos
    cloned_repos = repo_store().get_repos_by_clone_status("p1", "cloned")
    assert len(cloned_repos) == 1
    assert cloned_repos[0]["id"] == rid3


def test_indexer_error_survives_cloner_success(migrated_pg):
    """Regression guard: cloner success must NOT clear an existing indexer error.

    Before the Fix-1 fix, set_clone_status wrote to repos.error_msg, so a successful
    clone (error_msg=None) would silently clear a prior indexer failure message.
    """
    pid = repo_store().add_profile("p1", "17.0")
    rid = repo_store().add_repo(pid, "github.com/a/b", "17.0", "/tmp/a")
    # Simulate a prior indexer failure
    repo_store().update_repo_status(rid, status="error", error_msg="OSError: indexer fail")
    # Cloner finishes successfully
    repo_store().set_clone_status(rid, "cloned")
    # Verify: indexer error_msg preserved; clone_error_msg NULL on success
    repo = repo_store().get_repo_by_id(rid)
    assert repo is not None
    assert repo["error_msg"] == "OSError: indexer fail", (
        "Indexer error_msg must be preserved after cloner success"
    )
    assert repo["clone_error_msg"] is None, (
        "clone_error_msg should be NULL on successful clone"
    )


def test_ssh_key_delete_sets_repo_ssh_key_id_null(migrated_pg):
    """FK ON DELETE SET NULL: deleting an ssh_key_pair NULLs repos.ssh_key_id, not CASCADE."""
    pid = repo_store().add_profile("p1", "17.0")
    with migrated_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted) "
            "VALUES (%s, %s, %s) RETURNING id",
            ("deploy", "ssh-ed25519 AAAA...", "enc"),
        )
        key_id = cur.fetchone()[0]
    migrated_pg.commit()
    rid = repo_store().add_repo(
        pid, "git@host:org/repo.git", "main", "/tmp/r",
        ssh_key_id=key_id,
    )
    # Confirm ssh_key_id set
    repo_before = repo_store().get_repo_by_id(rid)
    assert repo_before is not None
    assert repo_before["ssh_key_id"] == key_id
    # Delete the SSH key
    with migrated_pg.cursor() as cur:
        cur.execute("DELETE FROM ssh_key_pairs WHERE id = %s", (key_id,))
    migrated_pg.commit()
    # Repo row must still exist with ssh_key_id NULLed (not cascaded away)
    repo_after = repo_store().get_repo_by_id(rid)
    assert repo_after is not None, "Repo row must not be deleted when ssh key is removed"
    assert repo_after["ssh_key_id"] is None, (
        "repos.ssh_key_id must be NULL after FK ON DELETE SET NULL"
    )


# ---------------------------------------------------------------------------
# M8 — Profile hierarchy tests
# ---------------------------------------------------------------------------

def test_get_ancestor_profile_names_three_tier(migrated_pg):
    """get_ancestor_profile_names returns [self, parent, grandparent] in order."""
    # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
    root_id = repo_store().add_profile("test_root_99", "99.0")
    mid_id = repo_store().add_profile("test_mid_99", "99.0")
    leaf_id = repo_store().add_profile("test_leaf_99", "99.0")

    repo_store().set_profile_parent(mid_id, root_id)
    repo_store().set_profile_parent(leaf_id, mid_id)

    names = repo_store().get_ancestor_profile_names("test_leaf_99")
    assert names == ["test_leaf_99", "test_mid_99", "test_root_99"]


def test_get_ancestor_repos_three_tier(migrated_pg):
    """get_ancestor_repos returns repos from all tiers, depth ASC order."""
    # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
    root_id = repo_store().add_profile("test_root_99", "99.0")
    mid_id = repo_store().add_profile("test_mid_99", "99.0")
    leaf_id = repo_store().add_profile("test_leaf_99", "99.0")

    repo_store().set_profile_parent(mid_id, root_id)
    repo_store().set_profile_parent(leaf_id, mid_id)

    # Add one repo per tier
    repo_store().add_repo(leaf_id, "github.com/internal/repo", "99.0", "/tmp/internal")
    repo_store().add_repo(mid_id, "github.com/acme_addons/repo", "99.0", "/tmp/acme")
    repo_store().add_repo(root_id, "github.com/odoo/odoo", "99.0", "/tmp/odoo")

    repos = repo_store().get_ancestor_repos("test_leaf_99")
    assert len(repos) == 3

    profile_names = [r["profile_name"] for r in repos]
    # self-tier repos come before parent tiers
    assert profile_names[0] == "test_leaf_99"
    assert profile_names[1] == "test_mid_99"
    assert profile_names[2] == "test_root_99"


def test_cycle_prevention_self_ref(migrated_pg):
    """set_profile_parent(A, A) raises ValueError — self-reference."""
    # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
    pid = repo_store().add_profile("test_root_99", "99.0")
    with pytest.raises(ValueError, match="self-reference"):
        repo_store().set_profile_parent(pid, pid)


def test_cycle_prevention_a_b_a(migrated_pg):
    """A → B exists; B → A raises ValueError (would create cycle)."""
    # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
    a_id = repo_store().add_profile("test_root_99", "99.0")
    b_id = repo_store().add_profile("test_mid_99", "99.0")

    repo_store().set_profile_parent(b_id, a_id)  # A → B (B's parent = A)

    # Now try to make A's parent = B — that would create A → B → A cycle
    with pytest.raises(ValueError, match="cycle"):
        repo_store().set_profile_parent(a_id, b_id)


def test_version_mismatch_rejected(migrated_pg):
    """parent.odoo_version != child.odoo_version raises ValueError."""
    # Use non-conflicting names (all odoo_N are seeded by migration 0004).
    parent_id = repo_store().add_profile("test_mismatch_parent_99", "99.0")
    child_id = repo_store().add_profile("test_mismatch_child_98", "98.0")

    with pytest.raises(ValueError, match="version mismatch"):
        repo_store().set_profile_parent(child_id, parent_id)


def test_set_profile_parent_idempotent(migrated_pg):
    """Calling set_profile_parent twice with same value: first True, second False."""
    # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
    parent_id = repo_store().add_profile("test_root_99", "99.0")
    child_id = repo_store().add_profile("test_mid_99", "99.0")

    changed_first = repo_store().set_profile_parent(child_id, parent_id)
    assert changed_first is True

    changed_second = repo_store().set_profile_parent(child_id, parent_id)
    assert changed_second is False


def test_get_ancestor_profile_names_no_parent(migrated_pg):
    """Root profile returns just its own name."""
    # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
    repo_store().add_profile("test_root_99", "99.0")
    names = repo_store().get_ancestor_profile_names("test_root_99")
    assert names == ["test_root_99"]


def test_get_ancestor_profile_names_unknown(migrated_pg):
    """Non-existent profile returns empty list."""
    names = repo_store().get_ancestor_profile_names("nonexistent")
    assert names == []


def test_cycle_prevention_three_hop(migrated_pg):
    """A→B→C exists; C→A raises ValueError (n-hop cycle).

    Exercises the recursive CTE path of _validate_parent beyond the simple
    A→B→A case — confirms that the ancestor walk goes all the way to the root.
    """
    # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
    a_id = repo_store().add_profile("test_root_99", "99.0")
    b_id = repo_store().add_profile("test_mid_99", "99.0")
    c_id = repo_store().add_profile("test_leaf_99", "99.0")

    repo_store().set_profile_parent(b_id, a_id)   # B's parent = A  (A→B)
    repo_store().set_profile_parent(c_id, b_id)   # C's parent = B  (B→C)

    # Attempting to set A's parent = C would form A→B→C→A cycle
    with pytest.raises(ValueError, match="cycle"):
        repo_store().set_profile_parent(a_id, c_id)


def test_add_profile_version_mismatch_no_orphan(migrated_pg):
    """add_profile with version mismatch raises ValueError; no orphan row created.

    Regression for: checkout() sets autocommit=True so INSERT auto-commits
    before _validate_parent ran, leaving an orphan row with parent_profile_id=NULL.
    Fix: validate-before-insert.
    """
    # Use non-conflicting names (odoo_16 is seeded by migration 0004).
    parent_id = repo_store().add_profile("test_orphan_parent_16", "16.0")
    count_before = len(repo_store().list_profiles())

    with pytest.raises(ValueError, match="version mismatch"):
        repo_store().add_profile("child_17", "17.0", parent_id=parent_id)

    count_after = len(repo_store().list_profiles())
    assert count_after == count_before, (
        f"Orphan row inserted: profile count grew from {count_before} to {count_after}"
    )


def test_add_profile_nonexistent_parent_no_orphan(migrated_pg):
    """add_profile with nonexistent parent_id raises ValueError; no orphan row created."""
    count_before = len(repo_store().list_profiles())

    with pytest.raises(ValueError, match="not found"):
        repo_store().add_profile("child_17", "17.0", parent_id=99999)

    count_after = len(repo_store().list_profiles())
    assert count_after == count_before, (
        f"Orphan row inserted: profile count grew from {count_before} to {count_after}"
    )
