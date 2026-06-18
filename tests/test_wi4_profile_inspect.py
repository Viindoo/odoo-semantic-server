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
import sys

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
    assert "summary" in result and "repos" in result and "modules" in result


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
    real_names = frozenset(mcp._tool_manager._tools.keys())
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


def test_modules_name_none_narrowed_by_active_session_pin(wi4_db):
    """name=None + an active session pin narrows _scope(None) to the pinned profile.

    _scope(None) injects the per-session pin via _resolve_profile(None) BEFORE the
    ADR-0034 tenant narrowing (#251), so a session pinned to a profile that is NOT
    on the seeded modules' profile[] must hide every module — even for an admin
    (unrestricted) caller. This is the behavior the helper comment now documents
    ("FURTHER narrowed by any active session pin, narrowing-only / fail-closed").

    Baseline: test_inquery_choke_mutation_guard already proves the SAME admin call
    with NO pin returns all 60 modules. Here the only added variable is the pin, so
    the disappearance is attributable solely to pin injection.

    MUTATION CHECK: deleting the `profile_name = _resolve_profile(None)` injection
    at the top of server._scope makes this test RED — the pin is ignored, own stays
    None (admin/unrestricted), and the modules reappear.
    """
    import importlib

    from src.mcp import session as sess

    server = importlib.import_module("src.mcp.server")

    pinned_api_key_id = "424242"          # numeric → a real (storable) pin key
    mcp_session_id = "wi4-pin-session"
    foreign_profile = "wi4_internal_97"   # NOT in modules' profile[] (viindoo+odoo)

    # Set the per-session pin in the in-memory store (the source of truth, #251).
    stored = sess.set_active_profile_db(
        pinned_api_key_id, foreign_profile, mcp_session_id
    )
    assert stored, "Pin must be stored for a numeric api_key_id (precondition)."

    # Bind the current MCP context to that same (api_key_id, mcp_session_id) so the
    # real resolution path (_get_api_key_id / _get_mcp_session_id -> resolve_profile_v2)
    # picks up the pin we just wrote — no mock of _resolve_profile (we test behavior,
    # not the mock).
    tok_key = server._api_key_id_var.set(pinned_api_key_id)
    tok_sid = server._mcp_session_id_var.set(mcp_session_id)
    try:
        result = _call_profile_inspect(
            name=None,
            method="modules",
            odoo_version=_WI4_VERSION,
        )
    finally:
        server._api_key_id_var.reset(tok_key)
        server._mcp_session_id_var.reset(tok_sid)
        sess._cache_invalidate(pinned_api_key_id, mcp_session_id)

    assert "wi4_mod_" not in result, (
        "Session pin to a foreign profile must narrow _scope(None) and hide all "
        f"modules whose profile[] does not include the pin.\nResult:\n{result}"
    )


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
