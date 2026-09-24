# SPDX-License-Identifier: AGPL-3.0-or-later
"""WI-4 behavior tests: profile_inspect discriminator tool (#260, #259 chain).

Acceptance criteria:
  (a) method='summary' renders ancestor chain + children + repos + module_count.
  (b) method='modules' paginates (>limit rows across multiple pages, dedup stable).
  (c) method='repos' dedup DISTINCT ON (url, branch) — same repo in 2 profiles
      appears only once.
  (d) Non-owned/empty-allowed profile denied under tenant choke (0 rows).
      This test MUST FAIL if the _scope/_effective_allowed choke is removed.
  (e) Invalid method returns Error: message (unit-only, no DB needed).
  (f) tool name-set test: importing server exposes exactly the expected 31 MCP tools.

Tests (a)-(d) require Neo4j + Postgres.
Test (e) is DB-free.
Test (f) is DB-free.

DB versions: TEST_VERSION = "99.0" (shared conftest) + PG seed via conftest pg_conn.
"""
import asyncio
import re
import sys
from contextlib import contextmanager

import pytest

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]

# Use a per-file unique version to avoid conflicts with other neo4j tests.
_WI4_VERSION = "97.0"  # distinct from 93.0 (wi7), 99.0 (conftest default)


# ---------------------------------------------------------------------------
# Helpers: PG profile/repo seeding
# ---------------------------------------------------------------------------

def _cleanup(pg_conn):
    with pg_conn.cursor() as cur:
        # Delete repos first (FK -> profiles)
        cur.execute(
            "DELETE FROM repos WHERE profile_id IN "
            r"(SELECT id FROM profiles WHERE name LIKE 'wi4\_%%')"
        )
        cur.execute(r"DELETE FROM profiles WHERE name LIKE 'wi4\_%%'")
        cur.execute(r"DELETE FROM tenants WHERE name LIKE 'wi4\_%%'")
    if not pg_conn.autocommit:
        pg_conn.commit()


def _profile(pg_conn, name, *, version=_WI4_VERSION, tenant_id=None, parent_id=None) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, parent_profile_id, tenant_id)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (name, version, parent_id, tenant_id),
        )
        pid = cur.fetchone()[0]
    if not pg_conn.autocommit:
        pg_conn.commit()
    return pid


def _repo(pg_conn, profile_id, *, url, branch="17.0", status="indexed") -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path, status)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (profile_id, url, branch, f"/tmp/wi4/{url.split('/')[-1]}", status),
        )
        rid = cur.fetchone()[0]
    if not pg_conn.autocommit:
        pg_conn.commit()
    return rid


def _tenant(pg_conn, name: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,))
        tid = cur.fetchone()[0]
    if not pg_conn.autocommit:
        pg_conn.commit()
    return tid


# ---------------------------------------------------------------------------
# Fixtures: seed Neo4j modules + PG profiles/repos
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wi4_db(neo4j_driver, pg_conn):
    """Seed:
      PG:  parent profile (wi4_odoo_97) with 1 repo
           child profile (wi4_viindoo_97) with 2 repos (1 shared URL with parent)
           grandchild profile (wi4_internal_97) with 0 repos
           tenant + api_key for tenant isolation test
      Neo4j: 60 Module nodes stamped with wi4_viindoo_97 profile
             (>50 so pagination is required at limit=50).
    """
    from src.db.migrate import run_migrations
    run_migrations(pg_conn)
    _cleanup(pg_conn)

    # PG profiles ---
    parent_id = _profile(pg_conn, "wi4_odoo_97")
    child_id = _profile(pg_conn, "wi4_viindoo_97", parent_id=parent_id)
    _profile(pg_conn, "wi4_internal_97", parent_id=child_id)

    # PG repos ---
    # parent has 1 repo
    _repo(pg_conn, parent_id, url="https://github.com/odoo/odoo")
    # child has 2 repos: one new, one with SAME url as parent (dedup test)
    _repo(pg_conn, child_id, url="https://github.com/Viindoo/viindoo")
    _repo(pg_conn, child_id, url="https://github.com/odoo/odoo")  # same url, dedup must fire

    # Tenant + tenant profile (for isolation test, not in ancestor chain)
    tid = _tenant(pg_conn, "wi4_tenant")
    _profile(pg_conn, "wi4_tenant_17", tenant_id=tid)

    # Neo4j: 60 Module nodes stamped with wi4_viindoo_97 profile
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_WI4_VERSION)
        for i in range(60):
            mod_name = f"wi4_mod_{i:03d}"
            session.run(
                """
                MERGE (m:Module {name: $name, odoo_version: $v})
                SET m.profile = ['wi4_viindoo_97', 'wi4_odoo_97'],
                    m.edition = 'community',
                    m.repo = 'odoo_test',
                    m.repo_url = 'https://github.com/Viindoo/viindoo',
                    m.repo_id = 1
                """,
                name=mod_name, v=_WI4_VERSION,
            )

    # Make server load with correct Neo4j env
    sys.modules.pop("src.mcp.server", None)

    yield {
        "pg_conn": pg_conn,
        "parent_name": "wi4_odoo_97",
        "child_name": "wi4_viindoo_97",
        "grandchild_name": "wi4_internal_97",
        "tenant_profile": "wi4_tenant_17",
        "tenant_id": tid,
        "version": _WI4_VERSION,
    }

    # Teardown
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_WI4_VERSION)
    _cleanup(pg_conn)


# ---------------------------------------------------------------------------
# (e) Invalid method - unit test (no DB)
# ---------------------------------------------------------------------------


def test_invalid_method_returns_error():
    """Invalid method= returns 'Error: unknown method' (router unit test)."""
    from src.mcp.inspect import _profile_inspect
    result = _profile_inspect(name="any_profile", method="nonexistent", odoo_version="17.0")
    assert result.startswith("Error: unknown method"), (
        f"Expected 'Error: unknown method ...' but got: {result!r}"
    )
    assert "profile_inspect" in result
    assert (
        "summary" in result and "repos" in result
        and "modules" in result and "coverage" in result
    )


# ---------------------------------------------------------------------------
# (f) Tool name-set inventory (no DB)
# ---------------------------------------------------------------------------

# Canonical tool name-set for the test-surface-index milestone:
# 25 baseline tools + 6 added by WI-4 (find_test_examples, tests_covering,
# test_class_inspect, test_base_classes, test_coverage_audit, js_test_inspect).
# This is a NAME INVENTORY — complementary to the count guard in
# test_tool_count_sync.py (which reads constants.ts). A tool renamed or replaced
# with a synonym breaks this guard but not the count, so the two tests cover
# different drift modes.
_EXPECTED_TOOL_NAMES = frozenset({
    "api_version_diff",
    "check_module_exists",
    "cli_help",
    "describe_module",
    "entity_lookup",
    "find_deprecated_usage",
    "find_examples",
    "find_override_point",
    "find_style_override",
    "find_test_examples",
    "impact_analysis",
    "js_test_inspect",
    "lint_check",
    "list_available_profiles",
    "list_available_versions",
    "lookup_core_api",
    "model_inspect",
    "module_inspect",
    "profile_inspect",
    "resolve_orm_chain",
    "resolve_stylesheet",
    "set_active_profile",
    "set_active_version",
    "suggest_pattern",
    "test_base_classes",
    "test_class_inspect",
    "test_coverage_audit",
    "tests_covering",
    "validate_depends",
    "validate_domain",
    "validate_relation",
})


def test_tool_name_set_matches_expected():
    """Registered MCP tool names must exactly match the expected inventory.

    Catches renames, accidental removals, and unannounced additions that a
    plain count check (test_tool_count_sync.py) cannot detect: e.g. replacing
    'find_test_examples' with 'search_test_examples' keeps the count at 31 but
    breaks this guard.
    """
    from src.mcp.server import mcp
    real_names = frozenset(t.name for t in asyncio.run(mcp.list_tools()))
    missing = _EXPECTED_TOOL_NAMES - real_names
    extra = real_names - _EXPECTED_TOOL_NAMES
    assert not missing and not extra, (
        f"MCP tool name-set mismatch.\n"
        f"  Missing (expected but absent): {sorted(missing)}\n"
        f"  Extra   (present but unexpected): {sorted(extra)}\n"
        "Update _EXPECTED_TOOL_NAMES in this file AND TOOL_COUNT in "
        "site/src/lib/constants.ts when adding/removing/renaming tools."
    )


# ---------------------------------------------------------------------------
# (a) summary: ancestor chain + children + repos + module_count
# ---------------------------------------------------------------------------


def _call_profile_inspect(**kwargs):
    """Call _profile_inspect via the inspect module (avoids @offload wrapper)."""
    from src.mcp.inspect import _profile_inspect
    return _profile_inspect(**kwargs)


def test_summary_renders_ancestor_chain_and_children(wi4_db):
    """method='summary' on child profile shows ancestor chain + grandchild children."""
    result = _call_profile_inspect(
        name="wi4_viindoo_97", method="summary", odoo_version=_WI4_VERSION,
    )
    assert "wi4_viindoo_97" in result, f"Profile name missing in summary: {result}"
    assert "wi4_odoo_97" in result, f"Ancestor chain must include parent: {result}"
    assert "wi4_internal_97" in result, f"Direct child must appear in summary: {result}"
    assert "Ancestor chain" in result


def test_summary_shows_ancestor_chain(wi4_db):
    """Ancestor chain in summary goes child -> parent (depth-ascending)."""
    result = _call_profile_inspect(
        name="wi4_viindoo_97", method="summary", odoo_version=_WI4_VERSION,
    )
    # Ancestor chain: wi4_viindoo_97 -> wi4_odoo_97
    assert "wi4_viindoo_97" in result
    assert "wi4_odoo_97" in result
    assert "Ancestor chain" in result


def test_summary_shows_children(wi4_db):
    """Summary for child profile discloses its direct children."""
    result = _call_profile_inspect(
        name="wi4_viindoo_97", method="summary", odoo_version=_WI4_VERSION,
    )
    assert "wi4_internal_97" in result, (
        f"Direct child 'wi4_internal_97' must appear in summary of wi4_viindoo_97: {result}"
    )


def test_summary_for_leaf_shows_no_children(wi4_db):
    """Summary for a profile with no children reports 'Children: none'."""
    result = _call_profile_inspect(
        name="wi4_internal_97", method="summary", odoo_version=_WI4_VERSION,
    )
    assert "Children: none" in result, (
        f"Leaf profile must report 'Children: none': {result}"
    )


def test_summary_repos_deduped(wi4_db):
    """method='summary' deduplicates repos when same URL appears in parent+child."""
    result = _call_profile_inspect(
        name="wi4_viindoo_97", method="summary", odoo_version=_WI4_VERSION,
    )
    # https://github.com/odoo/odoo appears in both parent and child repos.
    # After dedup, it must appear exactly ONCE.
    count = result.count("github.com/odoo/odoo")
    assert count == 1, (
        f"github.com/odoo/odoo must appear exactly once (dedup). "
        f"Appeared {count} times in:\n{result}"
    )


def test_summary_module_count_non_negative(wi4_db):
    """method='summary' reports a module_count >= 0 (not an error or crash)."""
    result = _call_profile_inspect(
        name="wi4_viindoo_97", method="summary", odoo_version=_WI4_VERSION,
    )
    assert "Module count" in result, f"Module count line missing from summary: {result}"
    # 60 module nodes were seeded with wi4_viindoo_97 in their profile array.
    assert "60" in result, (
        f"Expected module_count=60 for wi4_viindoo_97 (60 modules seeded): {result}"
    )


# ---------------------------------------------------------------------------
# (b) modules: pagination > limit rows
# ---------------------------------------------------------------------------


def test_modules_first_page(wi4_db):
    """method='modules' returns first 50 rows when 60 modules exist."""
    result = _call_profile_inspect(
        name="wi4_viindoo_97",
        method="modules",
        odoo_version=_WI4_VERSION,
        start_index=0,
        limit=50,
    )
    assert "wi4_mod_" in result, f"Expected module rows, got: {result}"
    assert "Showing rows 1-50 of 60" in result, (
        f"Expected pagination 'Showing rows 1-50 of 60'. Got:\n{result}"
    )
    assert "and 10 more" in result, (
        f"Expected '... and 10 more' overflow disclosure. Got:\n{result}"
    )


def test_modules_cap_enforced_when_limit_exceeds_cap(wi4_db):
    """H1 (#260): a caller-supplied limit ABOVE the disclosed cap (50) must NOT
    return more than 50 rows.

    The docstring discloses 'default 50, max 50' and the project invariant is
    'caps never raised' (ADR-0023 §3). With 60 modules seeded, requesting
    limit=10000 must still page at 50 (rows 1-50 of 60, '... and 10 more'),
    NOT dump all 60. Fail-able: removing the min(limit, _PROFILE_MODULES_CAP)
    clamp returns 60 rows and breaks 'Showing rows 1-50'.
    """
    result = _call_profile_inspect(
        name="wi4_viindoo_97",
        method="modules",
        odoo_version=_WI4_VERSION,
        start_index=0,
        limit=10000,  # far above the cap
    )
    assert "Showing rows 1-50 of 60" in result, (
        f"Cap must hold the page at 50 even when limit=10000. Got:\n{result}"
    )
    assert "and 10 more" in result, (
        f"Overflow must still disclose '... and 10 more' (cap enforced). Got:\n{result}"
    )
    # Count rendered module rows — must be exactly 50, never 60.
    rendered_modules = result.count("wi4_mod_")
    assert rendered_modules == 50, (
        f"Cap enforcement: exactly 50 module rows must render, got {rendered_modules}."
        f"\n{result}"
    )
    # The continuation cursor must advance by the effective cap (50), not 10000,
    # so the next page starts at row 51 (no skipped rows).
    assert "start_index=50" in result, (
        f"Continuation cursor must advance by the effective cap (50), not the "
        f"raw limit. Got:\n{result}"
    )


def test_modules_second_page(wi4_db):
    """method='modules' start_index=50 returns the remaining 10 rows."""
    result = _call_profile_inspect(
        name="wi4_viindoo_97",
        method="modules",
        odoo_version=_WI4_VERSION,
        start_index=50,
        limit=50,
    )
    assert "Showing rows 51-60 of 60" in result, (
        f"Expected 'Showing rows 51-60 of 60'. Got:\n{result}"
    )
    assert "End of list" in result, (
        f"Last page must show 'End of list'. Got:\n{result}"
    )


def test_modules_dedup_stable(wi4_db):
    """Paginating through all modules produces stable, non-overlapping rows.

    Row names seen on page 1 must NOT appear on page 2 (dedup by position, not name).
    Verifies that ORDER BY m.name ASC is deterministic.
    """
    result_p1 = _call_profile_inspect(
        name="wi4_viindoo_97", method="modules", odoo_version=_WI4_VERSION,
        start_index=0, limit=50,
    )
    result_p2 = _call_profile_inspect(
        name="wi4_viindoo_97", method="modules", odoo_version=_WI4_VERSION,
        start_index=50, limit=50,
    )
    # Collect wi4_mod_XXX names from each page.
    import re
    p1_names = set(re.findall(r"wi4_mod_\d+", result_p1))
    p2_names = set(re.findall(r"wi4_mod_\d+", result_p2))
    overlap = p1_names & p2_names
    assert not overlap, (
        f"Pages must not overlap. Overlapping modules: {overlap}"
    )
    assert len(p1_names) == 50, f"Page 1 must have 50 unique modules. Got: {len(p1_names)}"
    assert len(p2_names) == 10, f"Page 2 must have 10 unique modules. Got: {len(p2_names)}"


# ---------------------------------------------------------------------------
# (c) repos: DISTINCT ON (url, branch) dedup
# ---------------------------------------------------------------------------


def test_repos_dedup_distinct_on_url_branch(wi4_db):
    """method='repos' for wi4_viindoo_97 returns 2 unique repos (not 3).

    The profile has 2 repos, parent also contributes 1 repo with the SAME URL.
    After DISTINCT ON (url, branch) dedup, only 2 unique (url, branch) pairs remain.
    """
    result = _call_profile_inspect(
        name="wi4_viindoo_97", method="repos", odoo_version=_WI4_VERSION,
    )
    assert "Repos" in result
    # github.com/odoo/odoo must appear exactly once (same URL in parent + child)
    count = result.count("github.com/odoo/odoo")
    assert count == 1, (
        f"Dedup failed: github.com/odoo/odoo appears {count} times (expected 1). "
        f"Result:\n{result}"
    )
    # github.com/Viindoo/viindoo must appear once
    assert "github.com/Viindoo/viindoo" in result
    # Total: 2 unique repos
    assert "2 unique" in result, (
        f"Expected '2 unique' repos header. Got:\n{result}"
    )


def test_repos_for_none_name_returns_all_visible(wi4_db):
    """method='repos' with name=None returns repos across all visible profiles."""
    result = _call_profile_inspect(
        name=None, method="repos", odoo_version=_WI4_VERSION,
    )
    # Both github URLs should appear (at least once)
    assert "github.com/odoo/odoo" in result
    assert "github.com/Viindoo/viindoo" in result


def test_repos_next_hint_uses_real_version_not_empty(wi4_db):
    """L2: the repos-method Next hint must carry a usable odoo_version.

    Regression: the repos branch passed ver='' into hints_for, so the pasted
    follow-up read `odoo_version=''`. The branch already receives a real
    odoo_version, so the hint must interpolate it (never an empty string).
    """
    result = _call_profile_inspect(
        name="wi4_viindoo_97", method="repos", odoo_version=_WI4_VERSION,
    )
    assert "odoo_version=''" not in result, (
        f"repos Next hint leaked an empty odoo_version. Result:\n{result}"
    )
    # If a Next footer rendered at all, it must reference the real version.
    if "Next:" in result and "odoo_version=" in result:
        assert f"odoo_version='{_WI4_VERSION}'" in result


# ---------------------------------------------------------------------------
# (d) Tenant isolation: non-owned profile denied (choke ADR-0034)
# ---------------------------------------------------------------------------


def test_tenant_isolation_denied_profile(wi4_db):
    """A scoped tenant CANNOT see modules under a profile it does not own.

    This test MUST fail if the _scope/_effective_allowed choke is removed.
    The tenant owns wi4_tenant_17 (seeded in wi4_db fixture); it MUST NOT
    see modules under wi4_viindoo_97 (a different tenant's profile).

    Method: call _profile_inspect with a mocked tenant scope that only allows
    wi4_tenant_17, then request method='modules' for wi4_viindoo_97.
    Expected: choke denies -> "Not found or not authorized" message.
    """
    from unittest.mock import patch

    # Mock _effective_allowed to simulate the tenant's restricted scope
    # (own=[wi4_tenant_17], shared=[] - no shared profiles in test data).
    # _effective_allowed(profile_name='wi4_viindoo_97') should return []
    # because wi4_viindoo_97 is NOT in the tenant's allowed set.
    def mock_effective_allowed(profile_name):
        """Only wi4_tenant_17 is in own; wi4_viindoo_97 is NOT."""
        allowed = ["wi4_tenant_17"]  # the tenant's own profiles
        if profile_name is None:
            return allowed
        if profile_name in allowed:
            return [profile_name]
        return []  # deny-all for out-of-scope profile

    with patch("src.mcp.server._effective_allowed", side_effect=mock_effective_allowed):
        result = _call_profile_inspect(
            name="wi4_viindoo_97",  # NOT owned by this tenant
            method="modules",
            odoo_version=_WI4_VERSION,
        )

    assert "Not found or not authorized" in result or "not visible" in result, (
        f"Tenant choke (ADR-0034) MUST deny wi4_viindoo_97 to a tenant that doesn't own it. "
        f"Got: {result!r}"
    )


def test_tenant_isolation_summary_denied(wi4_db):
    """A scoped tenant CANNOT get summary for a profile it does not own."""
    from unittest.mock import patch

    def mock_effective_allowed(profile_name):
        allowed = ["wi4_tenant_17"]
        if profile_name is None:
            return allowed
        return [profile_name] if profile_name in allowed else []

    with patch("src.mcp.server._effective_allowed", side_effect=mock_effective_allowed):
        result = _call_profile_inspect(
            name="wi4_viindoo_97",
            method="summary",
            odoo_version=_WI4_VERSION,
        )

    assert "Not found or not authorized" in result or "not visible" in result, (
        f"Summary choke MUST deny wi4_viindoo_97 to a non-owning tenant. Got: {result!r}"
    )


def test_inquery_choke_name_none_foreign_module_absent(wi4_db):
    """Neo4j in-query _scope_pred choke (ADR-0034): name=None path relies SOLELY
    on the Neo4j all()-choke, NOT on the Python _effective_allowed pre-check.

    Why this is the critical test (see wave2-review M1):
    - method='modules', name=None: the Python pre-check guard is
        ``if name and allowed is not None and name not in allowed``
      which is FALSE when name=None, so it is BYPASSED entirely.
    - The ONLY layer that can deny modules is the in-query _scope_pred predicate
      injected via srv._scope(None) in the Cypher WHERE clause.

    Mechanics:
    - Patch _get_tenant_id to return the test tenant_id (scoped tenant context).
    - Patch _session.resolve_tenant_scope to return (own=['wi4_tenant_17'], shared=[]).
      This means own/shared contain NO profile matching the seeded modules'
      profile array (['wi4_viindoo_97', 'wi4_odoo_97']).
    - _scope_pred evaluates:
        all(__p IN ['wi4_viindoo_97','wi4_odoo_97']
            WHERE __p IN ['wi4_tenant_17'] OR __p IN []) -> FALSE
      -> modules are denied by the Neo4j layer.
    - Assert: no wi4_mod_* modules appear in the result (0 rows).

    MUTATION CHECK: commenting out the _scope_pred line in _profile_modules makes
    this test RED (modules are returned without the choke).
    """
    from unittest.mock import patch

    tid = wi4_db["tenant_id"]
    _OWN = ["wi4_tenant_17"]
    _SHARED: list = []

    with patch("src.mcp.server._get_tenant_id", return_value=tid), \
         patch("src.mcp.session.resolve_tenant_scope", return_value=(_OWN, _SHARED)):
        result = _call_profile_inspect(
            name=None,
            method="modules",
            odoo_version=_WI4_VERSION,
        )

    # Modules stamped with ['wi4_viindoo_97', 'wi4_odoo_97'] must NOT appear.
    # The in-query Neo4j _scope_pred choke is the ONLY layer that denies them
    # on the name=None path.
    assert "wi4_mod_" not in result, (
        "ADR-0034 in-query Neo4j choke FAILED: foreign-tenant modules visible "
        f"to a scoped tenant that should not see them.\nResult:\n{result}"
    )


def test_inquery_choke_name_not_none_partial_scope_denies(wi4_db):
    """L5: name!=None path — the in-query Neo4j _scope_pred choke denies a module
    whose profile[] is not fully within own∪shared, EVEN when the Python pre-gate
    passes.

    Why this complements test_tenant_isolation_denied_profile (which mocks
    _effective_allowed and only exercises the PRE-gate at line 706): here the
    Python pre-gate is made to PASS, so the ONLY layer that can deny is the
    in-query _scope_pred predicate (built from _scope(None)).

    Mechanics:
    - resolve_allowed_profiles → ['wi4_viindoo_97']  → _effective_allowed('wi4_viindoo_97')
      returns ['wi4_viindoo_97'] (name IS in allowed) → pre-gate PASSES (no early return).
    - resolve_tenant_scope → (own=['wi4_viindoo_97'], shared=[]).
      Seeded modules carry profile=['wi4_viindoo_97','wi4_odoo_97']; the in-query
      predicate evaluates:
        all(__p IN ['wi4_viindoo_97','wi4_odoo_97']
            WHERE __p IN ['wi4_viindoo_97'] OR __p IN [])  →  FALSE  (wi4_odoo_97 absent)
      → every module is denied by the Neo4j layer → 0 rows.

    MUTATION CHECK: deleting the `AND {srv._scope_pred('m')}` line in
    _profile_modules makes this test RED (modules become visible despite the
    partial scope), proving it exercises the in-query choke, not the pre-gate.
    """
    from unittest.mock import patch

    tid = wi4_db["tenant_id"]

    with patch("src.mcp.server._get_tenant_id", return_value=tid), \
         patch("src.mcp.session.resolve_allowed_profiles",
               return_value=["wi4_viindoo_97"]), \
         patch("src.mcp.session.resolve_tenant_scope",
               return_value=(["wi4_viindoo_97"], [])):
        result = _call_profile_inspect(
            name="wi4_viindoo_97",      # pre-gate PASSES (in allowed)
            method="modules",
            odoo_version=_WI4_VERSION,
        )

    # Pre-gate did NOT short-circuit (the not-authorized message must be absent)...
    assert "not authorized" not in result and "not visible" not in result, (
        "Pre-gate should PASS so the in-query choke is the layer under test. "
        f"Got:\n{result}"
    )
    # ...and the in-query choke denied every module (partial-scope mismatch).
    assert "wi4_mod_" not in result, (
        "ADR-0034 in-query Neo4j choke FAILED on the name!=None path: modules "
        "whose profile[] is not fully within own∪shared leaked to a scoped "
        f"tenant.\nResult:\n{result}"
    )


def test_inquery_choke_mutation_guard(wi4_db):
    """Companion to test_inquery_choke_name_none_foreign_module_absent.

    This test verifies that the companion test exercises the REAL Neo4j choke
    by confirming that WITHOUT the scope restriction (admin scope), the same
    modules ARE visible. If the companion test passes AND this test passes,
    it proves the choke actually filters — not that modules are simply absent.

    Without scope restriction (_get_tenant_id returns None -> admin/unrestricted):
    the 60 seeded modules MUST appear in the result.
    """
    result = _call_profile_inspect(
        name=None,
        method="modules",
        odoo_version=_WI4_VERSION,
    )
    assert "wi4_mod_" in result, (
        "Sanity check failed: wi4_mod_* modules must be visible to an admin "
        f"(unrestricted) caller. If they are missing, the companion isolation "
        f"test is vacuous.\nResult:\n{result}"
    )


# ---------------------------------------------------------------------------
# Session-pin narrowing (ADR-0029 #251): profile_inspect(modules, name=None)
# inherits the per-session pinned profile via _scope(None) -> _resolve_profile.
# ---------------------------------------------------------------------------


@contextmanager
def _pinned(server, sess, *, api_key_id, mcp_session_id, profile, tenant_id=None):
    """Run as (api_key_id, mcp_session_id) with *profile* pinned, as *tenant_id*."""
    assert sess.set_active_profile_db(api_key_id, profile, mcp_session_id), (
        "Pin must be stored for a numeric api_key_id (precondition).")
    sess.invalidate_allowed_profiles()
    tok_key = server._api_key_id_var.set(api_key_id)
    tok_sid = server._mcp_session_id_var.set(mcp_session_id)
    tok_tid = server._tenant_id_var.set(tenant_id)
    try:
        yield
    finally:
        server._tenant_id_var.reset(tok_tid)
        server._api_key_id_var.reset(tok_key)
        server._mcp_session_id_var.reset(tok_sid)
        sess._cache_invalidate(api_key_id, mcp_session_id)
        sess.invalidate_allowed_profiles()


# Sort before wi4_mod_* so they land on the first page (ORDER BY name).
_PRIVATE_MODULES = [f"wi4_acme_{i:03d}" for i in range(3)]


def _page_rows(out: str) -> tuple[str, list[str]]:
    """(the "Showing rows ..." line, the module names listed on the page)."""
    showing = [ln for ln in out.splitlines() if "Showing rows" in ln]
    return (showing[0] if showing else ""), re.findall(r"\bwi4_(?:mod|acme)_\d{3}\b", out)


def test_modules_name_none_narrowed_by_active_session_pin(wi4_db, neo4j_driver):
    """name=None + a session pin narrows the admin view to what a tenant pinned
    to the same profile sees (ADR-0034, #251).

    Rewritten (lane-mcpfix defect 6). The old test pinned an admin to
    wi4_internal_97 and required EVERY wi4_mod_* to disappear. Those modules
    carry [wi4_viindoo_97, wi4_odoo_97], and every wi4_* profile is shared
    (tenant_id NULL), so a tenant pinned to wi4_internal_97 sees them: the old
    expectation encoded the admin-only defect (admin narrowing dropped the
    shared list). The rule now: for the same pinned profile, the admin and a
    tenant see exactly the same rows.

    To keep proving the pin is applied at all, three modules owned by the
    tenant-private profile wi4_tenant_17 are added: an unpinned admin sees
    them (precondition), a pin to wi4_internal_97 hides them from the admin and
    from the tenant alike.

    MUTATION CHECK: deleting the `profile_name = _resolve_profile(None)`
    injection in server._scope makes the admin unrestricted again and the
    private modules reappear (RED). Reverting the admin shared-narrowing makes
    the shared wi4_mod_* rows vanish for the admin only (RED).
    """
    import importlib

    from src.mcp import session as sess

    server = importlib.import_module("src.mcp.server")
    pinned = "wi4_internal_97"  # shared profile, NOT on any module's profile[]

    with neo4j_driver.session() as s:
        for name in _PRIVATE_MODULES:
            s.run(
                "MERGE (m:Module {name: $name, odoo_version: $v}) "
                "SET m.profile = ['wi4_tenant_17'], m.edition = 'community', "
                "m.repo = 'tenant_repo'",
                name=name, v=_WI4_VERSION,
            )
    try:
        unpinned_admin = _call_profile_inspect(
            name=None, method="modules", odoo_version=_WI4_VERSION, limit=50)
        assert "wi4_acme_000" in unpinned_admin, (
            "precondition: an unpinned admin sees the tenant-private modules\n"
            f"{unpinned_admin}")

        with _pinned(server, sess, api_key_id="424242", mcp_session_id="wi4-pin-admin",
                     profile=pinned):
            admin = _call_profile_inspect(
                name=None, method="modules", odoo_version=_WI4_VERSION, limit=50)
        with _pinned(server, sess, api_key_id="424243", mcp_session_id="wi4-pin-tenant",
                     profile=pinned, tenant_id=wi4_db["tenant_id"]):
            tenant = _call_profile_inspect(
                name=None, method="modules", odoo_version=_WI4_VERSION, limit=50)
    finally:
        with neo4j_driver.session() as s:
            s.run("MATCH (m:Module {odoo_version: $v}) WHERE m.name IN $names "
                  "DETACH DELETE m", v=_WI4_VERSION, names=_PRIVATE_MODULES)

    assert "wi4_acme_" not in admin, (
        f"a pin to {pinned} must hide modules owned only by another profile:\n{admin}")
    assert "wi4_mod_000" in admin, (
        "modules stamped only with shared profiles stay visible under the pin, "
        f"as they are for a tenant:\n{admin}")
    assert _page_rows(admin) == _page_rows(tenant), (
        f"admin and tenant pinned to {pinned} must see the same rows.\n"
        f"ADMIN:\n{admin}\nTENANT:\n{tenant}")


# ---------------------------------------------------------------------------
# ADR-0023 tree grammar (lane-mcpfix defect 2) and truthful count labels
# (defect 5)
# ---------------------------------------------------------------------------

_PROFILE_OUTPUTS = [
    pytest.param(dict(name="wi4_viindoo_97", method="summary"), id="summary"),
    pytest.param(dict(name="wi4_internal_97", method="summary"), id="summary-leaf"),
    pytest.param(dict(name="wi4_viindoo_97", method="modules", start_index=0, limit=50),
                 id="modules-first-page"),
    pytest.param(dict(name="wi4_viindoo_97", method="modules", start_index=50, limit=50),
                 id="modules-last-page"),
    pytest.param(dict(name="wi4_viindoo_97", method="repos"), id="repos"),
    pytest.param(dict(name="wi4_viindoo_97", method="coverage"), id="coverage"),
    pytest.param(dict(name="wi4_internal_97", method="modules"), id="modules-empty"),
]


def _assert_one_closing_root_line(out: str) -> None:
    """Header, then tree items only; exactly one root-level └─ and it is the last line."""
    lines = out.rstrip("\n").split("\n")
    assert len(lines) >= 2, out
    assert not lines[0].startswith(("├", "└", "│", " ")), f"header missing:\n{out}"
    for ln in lines[1:]:
        assert ln.startswith(("├─ ", "└─ ", "│", "    ")), (
            f"line is not part of the tree: {ln!r}\n{out}")
    closing = [ln for ln in lines[1:] if ln.startswith("└─ ")]
    assert len(closing) == 1, f"expected exactly one root └─, got {len(closing)}:\n{out}"
    assert lines[-1] == closing[0], f"the root └─ must be the last line:\n{out}"


@pytest.mark.parametrize("kwargs", _PROFILE_OUTPUTS)
def test_every_profile_inspect_answer_closes_its_root_exactly_once(wi4_db, kwargs):
    """ADR-0023 §1: one root └─, last. Pre-fix summary and modules printed a
    second root └─ (Module count / '... and N more') before the Next footer."""
    out = _call_profile_inspect(odoo_version=_WI4_VERSION, **kwargs)
    _assert_one_closing_root_line(out)


@pytest.mark.parametrize("kwargs", [
    pytest.param(dict(name="wi4_viindoo_97", method="summary"), id="summary"),
    pytest.param(dict(name="wi4_internal_97", method="summary"), id="summary-leaf"),
    pytest.param(dict(name="wi4_viindoo_97", method="modules", start_index=0, limit=50),
                 id="modules-first-page"),
    pytest.param(dict(name="wi4_viindoo_97", method="repos"), id="repos"),
    pytest.param(dict(name="wi4_viindoo_97", method="coverage"), id="coverage"),
    pytest.param(dict(name="wi4_internal_97", method="coverage"), id="coverage-leaf"),
    pytest.param(dict(name="wi4_internal_97", method="modules"), id="modules-empty"),
])
def test_profile_inspect_answers_pass_the_adr0023_tree_validator(wi4_db, kwargs):
    """The full ADR-0023 validator (tests/test_mcp_module_lifecycle_read.py).

    Round 2 (F1, 79914c5): the round-1 xfail(strict) marks are dropped. Every
    sub-list indents with pipe + 3 spaces (ADR-0023 §1.3) and the coverage
    legend is a root item with its own connector, so every answer - including
    the two-level Module count block and the ancestor-labelled coverage block -
    passes the full validator.
    """
    from tests.test_mcp_module_lifecycle_read import _assert_adr0023_tree

    _assert_adr0023_tree(_call_profile_inspect(odoo_version=_WI4_VERSION, **kwargs))


@pytest.mark.parametrize("method", ["summary", "modules", "repos", "coverage"])
def test_profile_not_visible_answer_is_a_two_line_tree(wi4_db, method):
    """A denied profile answers with the header and ONE closing └─ line that
    carries the advice. Pre-fix the advice sat on a third, connector-less line."""
    from unittest.mock import patch

    from tests.test_mcp_module_lifecycle_read import _assert_adr0023_tree

    def only_own(profile_name):
        return [] if profile_name else ["wi4_tenant_17"]

    with patch("src.mcp.server._effective_allowed", side_effect=only_own):
        out = _call_profile_inspect(name="wi4_viindoo_97", method=method,
                                    odoo_version=_WI4_VERSION)
    lines = out.rstrip("\n").split("\n")
    assert len(lines) == 2, out
    assert lines[1].startswith("└─ Not found or not authorized"), out
    assert "list_available_profiles()" in lines[1], out
    _assert_adr0023_tree(out)


def _count_block(out: str) -> list[str]:
    """The Module count root item and its sub-items."""
    lines = out.splitlines()
    heads = [i for i, ln in enumerate(lines) if ln.startswith("├─ Module count")]
    assert len(heads) == 1, out
    i = heads[0]
    block = [lines[i]]
    for ln in lines[i + 1:]:
        if not ln.startswith("│   "):
            break
        block.append(ln)
    return block


_COVERAGE_ROW = re.compile(
    r"^│   [├└]─ (?P<cat>.+?): own=(?P<own>\d+), with_ancestors=(?P<chain>\d+),"
    r" indexed_elsewhere=(?P<elsewhere>\d+)(?P<flag>  \[may be incomplete\])?$")


def _coverage_rows(out: str) -> dict[str, tuple[int, int, int, bool]]:
    rows = {}
    for ln in out.splitlines():
        m = _COVERAGE_ROW.match(ln)
        if m:
            rows[m["cat"]] = (int(m["own"]), int(m["chain"]), int(m["elsewhere"]),
                              bool(m["flag"]))
    return rows


def test_module_count_label_does_not_claim_parent_profile_content(wi4_db):
    """Defect 5 + round-2 owner decision C3: each number says what it counts.

    wi4_internal_97 owns no module; its parent chain (wi4_viindoo_97 ->
    wi4_odoo_97) holds the 60. Round 1 pinned a single "... parent profiles not
    counted: 0" line. The owner then decided (C3, f7d4e22) that the summary
    shows TWO labelled numbers: owned by this profile (0) and including the
    ancestor profiles, naming the chain (60). The protection is unchanged: the
    0 is never presented as an inheritance-inclusive count, and the parent's
    modules are reported where they are counted.
    """
    block = _count_block(_call_profile_inspect(
        name="wi4_internal_97", method="summary", odoo_version=_WI4_VERSION))
    assert block == [
        f"├─ Module count (version {_WI4_VERSION}):",
        "│   ├─ Owned by this profile: 0",
        "│   └─ Including ancestor profiles"
        " (wi4_internal_97 -> wi4_viindoo_97 -> wi4_odoo_97): 60",
    ], "\n".join(block)
    assert not any("inheritance-inclusive" in ln for ln in block), block


def test_coverage_label_does_not_claim_parent_profile_content(wi4_db):
    """Defect 5 + round-2 owner decision C3 for the coverage block.

    Round 1 asserted the single-number label said parent profiles were not
    counted. After C3 each category row carries own and with_ancestors
    separately, and the legend names the chain the second number walks. For
    wi4_internal_97 the 60 parent-chain modules are with_ancestors, not own,
    and nothing visible lies outside the chain (indexed_elsewhere=0, so no
    "may be incomplete" flag).
    """
    out = _call_profile_inspect(name="wi4_internal_97", method="coverage",
                                odoo_version=_WI4_VERSION)
    assert list(_coverage_rows(out).values()) == [(0, 60, 0, False)], out
    legend = [ln for ln in out.splitlines() if ln.startswith("├─ Legend:")]
    assert len(legend) == 1, out
    assert "(wi4_internal_97 -> wi4_viindoo_97 -> wi4_odoo_97)" in legend[0], out
    assert "inheritance-inclusive" not in out, out


# ---------------------------------------------------------------------------
# Round 2 C2 (0b4fda3): a session pin never narrows an explicitly named profile
# ---------------------------------------------------------------------------

_C2_PROFILE = "wi4_c2_second"          # second profile owned by wi4_tenant
_C2_MODULES = [f"wi4_c2mod_{i:03d}" for i in range(3)]
_C2_CATEGORY = "C2 Tenant Domain"


@pytest.fixture
def c2_world(wi4_db, neo4j_driver):
    """wi4_tenant owns wi4_tenant_17 AND wi4_c2_second (3 modules, one category)."""
    pg = wi4_db["pg_conn"]
    _profile(pg, _C2_PROFILE, tenant_id=wi4_db["tenant_id"])
    with neo4j_driver.session() as s:
        for name in _C2_MODULES:
            s.run("MERGE (m:Module {name: $name, odoo_version: $v}) "
                  "SET m.profile = [$p], m.edition = 'custom', m.repo = 'tenant_repo_2', "
                  "m.category = $cat",
                  name=name, v=_WI4_VERSION, p=_C2_PROFILE, cat=_C2_CATEGORY)
    from src.mcp import session as sess
    sess.invalidate_allowed_profiles()
    yield
    with neo4j_driver.session() as s:
        s.run("MATCH (m:Module {odoo_version: $v}) WHERE m.name IN $names DETACH DELETE m",
              v=_WI4_VERSION, names=_C2_MODULES)
    with pg.cursor() as cur:
        cur.execute("DELETE FROM profiles WHERE name = %s", (_C2_PROFILE,))
    if not pg.autocommit:
        pg.commit()
    sess.invalidate_allowed_profiles()


def _c2_numbers() -> tuple[str, list[str], tuple | None]:
    summary = _call_profile_inspect(name=_C2_PROFILE, method="summary",
                                    odoo_version=_WI4_VERSION)
    modules = _call_profile_inspect(name=_C2_PROFILE, method="modules",
                                    odoo_version=_WI4_VERSION, limit=50)
    coverage = _call_profile_inspect(name=_C2_PROFILE, method="coverage",
                                     odoo_version=_WI4_VERSION)
    owned = [ln for ln in _count_block(summary) if "Owned by this profile" in ln]
    listed = sorted(re.findall(r"\bwi4_c2mod_\d{3}\b", modules))
    return owned[0] if owned else summary, listed, _coverage_rows(coverage).get(_C2_CATEGORY)


@pytest.mark.parametrize("who", ["admin", "tenant"])
def test_session_pin_does_not_zero_an_explicitly_named_profile(wi4_db, c2_world, who):
    """C2 FIX: a session pinned to wi4_tenant_17 that asks profile_inspect about
    wi4_c2_second (another profile the caller can see) gets wi4_c2_second's true
    numbers - the same an unpinned session gets - in summary, modules and
    coverage.

    Real case: a tenant with two private profiles (e.g. a customer's prod and
    staging) pins one with set_active_profile, then inspects the other by name.
    Pre-fix the pin narrowed the read to the pinned profile and the named one
    read Owned 0, an empty module list and no coverage row.
    """
    import importlib

    from src.mcp import session as sess

    server = importlib.import_module("src.mcp.server")
    tenant_id = wi4_db["tenant_id"] if who == "tenant" else None
    tok = server._tenant_id_var.set(tenant_id)
    try:
        sess.invalidate_allowed_profiles()
        unpinned = _c2_numbers()
    finally:
        server._tenant_id_var.reset(tok)
    expected = ("│   ├─ Owned by this profile: 3", _C2_MODULES, (3, 3, 0, False))
    assert unpinned == expected, f"precondition (unpinned {who}): {unpinned}"

    with _pinned(server, sess, api_key_id="424250" if who == "admin" else "424251",
                 mcp_session_id=f"wi4-c2-{who}", profile="wi4_tenant_17",
                 tenant_id=tenant_id):
        pinned = _c2_numbers()
    assert pinned == expected, (
        f"{who} pinned to wi4_tenant_17 must read {_C2_PROFILE}'s true numbers: "
        f"{pinned} != {expected}")


def test_session_pin_does_not_open_a_profile_the_tenant_cannot_see(wi4_db, c2_world):
    """C2 GUARD: ignoring the pin for a named profile never widens tenant
    visibility - wi4_tenant, pinned to its own wi4_tenant_17, still gets the
    not-visible answer for a private profile of ANOTHER tenant."""
    # GUARD: pre-existing behaviour
    import importlib

    from src.mcp import session as sess

    server = importlib.import_module("src.mcp.server")
    pg = wi4_db["pg_conn"]
    other = _tenant(pg, "wi4_c2_other_tenant")
    _profile(pg, "wi4_c2_foreign", tenant_id=other)
    sess.invalidate_allowed_profiles()
    try:
        with _pinned(server, sess, api_key_id="424252", mcp_session_id="wi4-c2-deny",
                     profile="wi4_tenant_17", tenant_id=wi4_db["tenant_id"]):
            answers = {m: _call_profile_inspect(name="wi4_c2_foreign", method=m,
                                                odoo_version=_WI4_VERSION)
                       for m in ("summary", "modules", "coverage")}
    finally:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM profiles WHERE name = 'wi4_c2_foreign'")
            cur.execute("DELETE FROM tenants WHERE id = %s", (other,))
        if not pg.autocommit:
            pg.commit()
        sess.invalidate_allowed_profiles()
    for method, out in answers.items():
        assert "Not found or not authorized" in out, f"{method}:\n{out}"


# ---------------------------------------------------------------------------
# Round 2 C3 (f7d4e22, owner decision): own vs with_ancestors, tenant-scoped
# ---------------------------------------------------------------------------
#
# Chain wi4_c3_leaf -> wi4_c3_mid -> wi4_c3_root. Nodes carry the single
# profile that owns their repo (ADR-0034), so each module is stamped with one
# profile. wi4_c3_root is private to ANOTHER tenant; wi4_c3_mid is shared;
# wi4_c3_leaf is owned by the calling tenant. wi4_c3_other (shared, outside
# the chain) holds same-category modules the chain does not carry.

_C3_CATEGORY = "C3 Accounting"
_C3_SEED = {"wi4_c3_leaf": 3, "wi4_c3_mid": 2, "wi4_c3_root": 4, "wi4_c3_other": 2}
_C3_CHAIN = "wi4_c3_leaf -> wi4_c3_mid -> wi4_c3_root"


@pytest.fixture
def c3_world(wi4_db, neo4j_driver):
    from src.mcp import session as sess

    pg = wi4_db["pg_conn"]
    caller = _tenant(pg, "wi4_c3_caller")
    owner_of_root = _tenant(pg, "wi4_c3_root_owner")
    root = _profile(pg, "wi4_c3_root", tenant_id=owner_of_root)
    mid = _profile(pg, "wi4_c3_mid", parent_id=root)
    _profile(pg, "wi4_c3_leaf", parent_id=mid, tenant_id=caller)
    _profile(pg, "wi4_c3_other")
    names = []
    with neo4j_driver.session() as s:
        for prof, n in _C3_SEED.items():
            for i in range(n):
                name = f"{prof}_mod_{i}"
                names.append(name)
                s.run("MERGE (m:Module {name: $name, odoo_version: $v}) "
                      "SET m.profile = [$p], m.edition = 'custom', m.repo = $p, "
                      "m.category = $cat",
                      name=name, v=_WI4_VERSION, p=prof, cat=_C3_CATEGORY)
    sess.invalidate_allowed_profiles()
    yield {"caller": caller}
    with neo4j_driver.session() as s:
        s.run("MATCH (m:Module {odoo_version: $v}) WHERE m.name IN $names DETACH DELETE m",
              v=_WI4_VERSION, names=names)
    with pg.cursor() as cur:
        # children first (parent_profile_id FK)
        for p in ("wi4_c3_leaf", "wi4_c3_mid", "wi4_c3_root", "wi4_c3_other"):
            cur.execute("DELETE FROM profiles WHERE name = %s", (p,))
        cur.execute("DELETE FROM tenants WHERE id = ANY(%s)", ([caller, owner_of_root],))
    if not pg.autocommit:
        pg.commit()
    sess.invalidate_allowed_profiles()


@contextmanager
def _as_tenant(tenant_id):
    import importlib

    from src.mcp import session as sess

    server = importlib.import_module("src.mcp.server")
    sess.invalidate_allowed_profiles()
    tok = server._tenant_id_var.set(tenant_id)
    try:
        yield
    finally:
        server._tenant_id_var.reset(tok)
        sess.invalidate_allowed_profiles()


def _ascii_label(line: str) -> str:
    """The text after the tree prefix must be plain English (ASCII)."""
    text = line.lstrip("│ ├└─")
    assert text.isascii(), f"non-ASCII label: {line!r}"
    return text


def test_admin_summary_counts_own_and_with_ancestors_separately(wi4_db, c3_world):
    """C3: owned = 3 (leaf only); with ancestors = 3 + 2 + 4 = 9 along
    leaf -> mid -> root; the 2 modules of wi4_c3_other are in neither."""
    block = _count_block(_call_profile_inspect(
        name="wi4_c3_leaf", method="summary", odoo_version=_WI4_VERSION))
    assert block == [
        f"├─ Module count (version {_WI4_VERSION}):",
        "│   ├─ Owned by this profile: 3",
        f"│   └─ Including ancestor profiles ({_C3_CHAIN}): 9",
    ], "\n".join(block)
    for ln in block:
        _ascii_label(ln)


def test_admin_coverage_counts_own_with_ancestors_and_elsewhere(wi4_db, c3_world):
    """C3: same numbers per category; the 2 same-category modules outside the
    chain are indexed_elsewhere and flag the row as possibly incomplete."""
    out = _call_profile_inspect(name="wi4_c3_leaf", method="coverage",
                                odoo_version=_WI4_VERSION)
    assert _coverage_rows(out)[_C3_CATEGORY] == (3, 9, 2, True), out
    legend = [ln for ln in out.splitlines() if ln.startswith("├─ Legend:")]
    assert len(legend) == 1 and f"({_C3_CHAIN})" in legend[0], out
    _ascii_label(legend[0])


def test_middle_profile_counts_only_its_own_ancestors_not_its_children(wi4_db, c3_world):
    """C3: with_ancestors walks UP the chain only - mid owns 2, mid + root = 6;
    its child wi4_c3_leaf's 3 modules are not added."""
    block = _count_block(_call_profile_inspect(
        name="wi4_c3_mid", method="summary", odoo_version=_WI4_VERSION))
    assert block[1:] == [
        "│   ├─ Owned by this profile: 2",
        "│   └─ Including ancestor profiles (wi4_c3_mid -> wi4_c3_root): 6",
    ], "\n".join(block)


def test_tenant_never_counts_an_ancestor_it_cannot_see(wi4_db, c3_world):
    """C3 tenant boundary: the caller owns wi4_c3_leaf and sees the shared
    wi4_c3_mid, but wi4_c3_root is another tenant's private profile. Its 4
    modules are excluded from with_ancestors (3 + 2 = 5), and the out-of-chain
    wi4_c3_other modules (shared, visible) are the only indexed_elsewhere."""
    with _as_tenant(c3_world["caller"]):
        summary = _call_profile_inspect(name="wi4_c3_leaf", method="summary",
                                        odoo_version=_WI4_VERSION)
        coverage = _call_profile_inspect(name="wi4_c3_leaf", method="coverage",
                                         odoo_version=_WI4_VERSION)
    block = _count_block(summary)
    assert block[1] == "│   ├─ Owned by this profile: 3", summary
    assert block[2].startswith("│   └─ Including ancestor profiles ("), summary
    assert block[2].endswith("): 5"), summary
    assert _coverage_rows(coverage)[_C3_CATEGORY] == (3, 5, 2, True), coverage
    assert "wi4_c3_root_mod_" not in summary + coverage


def test_tenant_does_not_count_out_of_chain_modules_it_cannot_see(wi4_db, c3_world):
    """C3 tenant boundary on indexed_elsewhere: the 4 modules of the foreign
    private wi4_c3_root (same category) never inflate the tenant's
    indexed_elsewhere. Asking about wi4_c3_other (no ancestors): own =
    with_ancestors = 2; the rest of the category visible to the tenant is
    leaf 3 + mid 2 = 5 (an admin would see 9)."""
    with _as_tenant(c3_world["caller"]):
        coverage = _call_profile_inspect(name="wi4_c3_other", method="coverage",
                                         odoo_version=_WI4_VERSION)
    # visible to the tenant in this category: leaf 3 + mid 2 + other 2 = 7;
    # wi4_c3_other has no ancestors -> own = with_ancestors = 2, elsewhere = 5.
    assert _coverage_rows(coverage)[_C3_CATEGORY] == (2, 2, 5, True), coverage


# ---------------------------------------------------------------------------
# (e) Missing name for summary returns clear error
# ---------------------------------------------------------------------------


def test_summary_requires_name():
    """method='summary' with name=None returns clear error message."""
    from src.mcp.inspect import _profile_inspect
    result = _profile_inspect(name=None, method="summary", odoo_version="17.0")
    assert "requires name=" in result or "Error" in result, (
        f"summary without name must report an error. Got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Round 3 (2054622): profile_inspect never names, or shows the repos of, a
# profile outside the key's scope
# ---------------------------------------------------------------------------
#
# Tenant T owns wi4_r3_child. Its parent wi4_r3_secretparent is PRIVATE to
# tenant U; its grandparent wi4_r3_base is shared. wi4_r3_child has two
# children: wi4_r3_kid_shared (shared) and wi4_r3_kid_hidden (private to U).
# Real case: a partner's private customisation profile sits between a
# customer's profile and the shared CE base - the customer's key must not
# learn the partner profile's name or its private repository URL.

_R3_SECRET = "wi4_r3_secretparent"
_R3_SECRET_URL = "https://github.com/acme-private/secret-erp"
_R3_HIDDEN_KID = "wi4_r3_kid_hidden"
_R3_SEED = {"wi4_r3_child": 3, _R3_SECRET: 4, "wi4_r3_base": 2}
_R3_CATEGORY = "R3 Sales"
_R3_METHODS = ["summary", "modules", "repos", "coverage"]


@pytest.fixture
def r3_world(wi4_db, neo4j_driver):
    from src.mcp import session as sess

    pg = wi4_db["pg_conn"]
    t = _tenant(pg, "wi4_r3_t")
    u = _tenant(pg, "wi4_r3_u")
    base = _profile(pg, "wi4_r3_base")
    secret = _profile(pg, _R3_SECRET, parent_id=base, tenant_id=u)
    child = _profile(pg, "wi4_r3_child", parent_id=secret, tenant_id=t)
    _profile(pg, "wi4_r3_kid_shared", parent_id=child)
    _profile(pg, _R3_HIDDEN_KID, parent_id=child, tenant_id=u)
    _repo(pg, base, url="https://github.com/odoo/odoo-r3-base")
    _repo(pg, secret, url=_R3_SECRET_URL)
    _repo(pg, child, url="https://github.com/customer-t/erp")
    names = []
    with neo4j_driver.session() as s:
        for prof, n in _R3_SEED.items():
            for i in range(n):
                name = f"{prof}_mod_{i}"
                names.append(name)
                s.run("MERGE (m:Module {name: $name, odoo_version: $v}) "
                      "SET m.profile = [$p], m.edition = 'custom', m.repo = $p, "
                      "m.category = $cat",
                      name=name, v=_WI4_VERSION, p=prof, cat=_R3_CATEGORY)
    sess.invalidate_allowed_profiles()
    yield {"t": t}
    with neo4j_driver.session() as s:
        s.run("MATCH (m:Module {odoo_version: $v}) WHERE m.name IN $names DETACH DELETE m",
              v=_WI4_VERSION, names=names)
    with pg.cursor() as cur:
        cur.execute("DELETE FROM repos WHERE profile_id = ANY(%s)", ([base, secret, child],))
        for p in ("wi4_r3_kid_shared", _R3_HIDDEN_KID, "wi4_r3_child", _R3_SECRET,
                  "wi4_r3_base"):
            cur.execute("DELETE FROM profiles WHERE name = %s", (p,))
        cur.execute("DELETE FROM tenants WHERE id = ANY(%s)", ([t, u],))
    if not pg.autocommit:
        pg.commit()
    sess.invalidate_allowed_profiles()


def _r3_answers(tenant_id) -> dict[str, str]:
    with _as_tenant(tenant_id):
        return {m: _call_profile_inspect(name="wi4_r3_child", method=m,
                                         odoo_version=_WI4_VERSION, limit=50)
                for m in _R3_METHODS}


def _line(out: str, head: str) -> str:
    found = [ln for ln in out.splitlines() if ln.startswith(head)]
    assert len(found) == 1, f"{head!r}:\n{out}"
    return found[0]


@pytest.mark.parametrize("method", _R3_METHODS)
def test_tenant_never_sees_a_foreign_private_profile_name_or_repo(wi4_db, r3_world, method):
    """R3 FIX: no profile_inspect method shows T the name of U's private
    ancestor or child, nor the private ancestor's repository URL."""
    out = _r3_answers(r3_world["t"])[method]
    for secret in (_R3_SECRET, _R3_HIDDEN_KID, _R3_SECRET_URL):
        assert secret not in out, f"{method} leaks {secret!r}:\n{out}"


def test_tenant_summary_discloses_every_withheld_item_as_a_count(wi4_db, r3_world):
    """R3 FIX: each place that withholds something says how many - 1 ancestor
    profile, 1 child profile, 1 repo - and still lists what T may see."""
    out = _r3_answers(r3_world["t"])["summary"]
    chain = _line(out, "├─ Ancestor chain:")
    assert "wi4_r3_child" in chain and "wi4_r3_base" in chain, chain
    assert "(+1 ancestor profile not visible to this key)" in chain, chain
    kids = _line(out, "├─ Children")
    assert "wi4_r3_kid_shared" in kids, kids
    assert re.search(r"\(\+1 child profiles? not visible to this key\)", kids), kids
    repos = _line(out, "├─ Repos (")
    assert re.search(r"\(\+1 repo(s|\(s\))? not visible to this key\)", repos), repos
    assert "https://github.com/customer-t/erp" in out, out
    assert "https://github.com/odoo/odoo-r3-base" in out, out
    including = _count_block(out)[2]
    assert "+1 ancestor profile not visible to this key" in including, including


def test_tenant_counts_are_unchanged_by_withholding_names(wi4_db, r3_world):
    """R3: hiding the private parent's name does not change T's numbers - own
    3, with ancestors 3 + 2 (the parent's 4 modules stay excluded by the
    per-node choke, as in round 2)."""
    # GUARD: pre-existing behaviour (round-2 counts)
    ans = _r3_answers(r3_world["t"])
    block = _count_block(ans["summary"])
    assert block[1] == "│   ├─ Owned by this profile: 3", block
    assert block[2].endswith(": 5"), block
    assert _coverage_rows(ans["coverage"])[_R3_CATEGORY] == (3, 5, 0, False), ans["coverage"]


def test_tenant_repos_and_coverage_disclose_the_withheld_count(wi4_db, r3_world):
    """R3 FIX: repos(name) says one ancestor repo is withheld; the coverage
    legend names the visible chain and the hidden-profile count."""
    ans = _r3_answers(r3_world["t"])
    assert re.search(r"\+1 ancestor repo(s|\(s\))? not visible to this key", ans["repos"]), \
        ans["repos"]
    assert "https://github.com/customer-t/erp" in ans["repos"], ans["repos"]
    legend = _line(ans["coverage"], "├─ Legend:")
    assert "wi4_r3_child" in legend and "wi4_r3_base" in legend, legend
    assert "+1 ancestor profile not visible to this key" in legend, legend


@pytest.mark.parametrize("method", _R3_METHODS)
def test_tenant_withheld_answers_pass_the_adr0023_tree_validator(wi4_db, r3_world, method):
    """R3: the disclosure suffixes keep every answer a valid ADR-0023 tree."""
    from tests.test_mcp_module_lifecycle_read import _assert_adr0023_tree

    _assert_adr0023_tree(_r3_answers(r3_world["t"])[method])


def test_admin_still_sees_every_profile_and_repo(wi4_db, r3_world):
    """R3 GUARD: an admin key is unrestricted - the private parent, the hidden
    child and the private repo URL are all shown, with no withheld notice."""
    # GUARD: pre-existing behaviour (admin output unchanged)
    ans = _r3_answers(None)
    summary = ans["summary"]
    assert _R3_SECRET in _line(summary, "├─ Ancestor chain:"), summary
    assert _R3_HIDDEN_KID in _line(summary, "├─ Children"), summary
    assert _R3_SECRET_URL in summary and _R3_SECRET_URL in ans["repos"], ans
    assert _R3_SECRET in _line(ans["coverage"], "├─ Legend:"), ans["coverage"]
    assert all("not visible to this key" not in out for out in ans.values()), ans
    block = _count_block(summary)
    assert block[2] == ("│   └─ Including ancestor profiles"
                        f" (wi4_r3_child -> {_R3_SECRET} -> wi4_r3_base): 9"), block
