# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issue #251 — the profile pin READ path through the ADR-0034 choke.

Before #251 the profile pin was written but never read (dead path). #251 wires
``resolve_profile_v2`` into ``_scope`` / ``_effective_allowed`` at the TOP, BEFORE
the ADR-0034 tenant narrowing, so a query tool that OMITS ``profile_name``
inherits the session-pinned profile — but only ever as a NARROWING filter,
re-validated at read time and fail-closed:

  - pinned profile applied on omit          → narrows to that profile
  - explicit profile overrides the pin      → explicit wins, pin ignored
  - pinned profile OUT OF SCOPE (scoped tenant) → deny-all (empty scope / [])
    — the pin can NEVER widen beyond own ∪ shared, nor cross tenants
  - admin (tenant None) with no pin         → unrestricted (own=None / allowed=None)
  - clear the pin                           → back to the full tenant boundary

These are marked ``postgres`` because the choke reads tenant scope from Postgres
(``resolve_tenant_scope``). The profile pin itself is in-memory; we set it via
the real ``set_active_*_db`` store + the ``_api_key_id_var`` / ``_mcp_session_id_var``
ContextVars that ``_resolve_profile`` reads.
"""
import pytest

pytestmark = pytest.mark.postgres

PK = "60251"  # numeric api_key id (passes the #248 guard)
SESS = "read-path-sess"


# ---------------------------------------------------------------------------
# PG seeding helpers (mirrors tests/test_resolve_allowed_profiles.py)
# ---------------------------------------------------------------------------


def _cleanup(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM repos WHERE profile_id IN "
            r"(SELECT id FROM profiles WHERE name LIKE 'rp\_%%')"
        )
        cur.execute(r"DELETE FROM profiles WHERE name LIKE 'rp\_%%'")
        cur.execute(r"DELETE FROM tenants WHERE name LIKE 'rp\_%%'")
    if not pg_conn.autocommit:
        pg_conn.commit()


@pytest.fixture()
def rp_pg(pg_conn):
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)
    _cleanup(pg_conn)
    yield pg_conn
    _cleanup(pg_conn)


def _tenant(pg_conn, name: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,))
        tid = cur.fetchone()[0]
    if not pg_conn.autocommit:
        pg_conn.commit()
    return tid


def _profile(pg_conn, name, version, *, tenant_id=None, parent_id=None) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, parent_profile_id, tenant_id) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (name, version, parent_id, tenant_id),
        )
        pid = cur.fetchone()[0]
    if not pg_conn.autocommit:
        pg_conn.commit()
    return pid


# ---------------------------------------------------------------------------
# Context fixture: set api_key_id + mcp_session_id + (optional) tenant; clear all
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(rp_pg):
    """Yield a setter that pins the ContextVars + clears the pin store and the
    60s tenant-scope cache, then restores everything on teardown."""
    from src.mcp import server as _srv
    from src.mcp import session as _session

    def _clear_pin_store():
        with _session._cache_lock:
            _session._cache.clear()

    _clear_pin_store()
    _session.invalidate_allowed_profiles()

    ak = _srv._api_key_id_var.set(PK)
    sid = _srv._mcp_session_id_var.set(SESS)
    tid = _srv._tenant_id_var.set(_srv._tenant_id_var.get())

    def _set_tenant(tenant_id):
        # Reset+set so the value lands cleanly under the snapshot token above.
        _srv._tenant_id_var.set(tenant_id)

    try:
        yield _set_tenant
    finally:
        _srv._api_key_id_var.reset(ak)
        _srv._mcp_session_id_var.reset(sid)
        _srv._tenant_id_var.reset(tid)
        _clear_pin_store()
        _session.invalidate_allowed_profiles()


def _pin_profile(name):
    from src.mcp.session import set_active_profile_db
    assert set_active_profile_db(PK, name, SESS) is True


# ---------------------------------------------------------------------------
# 1. pinned profile applied when the caller omits profile_name
# ---------------------------------------------------------------------------


def test_pinned_profile_applied_on_omit(ctx, rp_pg):
    """A scoped tenant pins an in-scope profile; _scope(None) narrows to it."""
    from src.mcp import server as _srv

    base = _profile(rp_pg, "rp_base_17", "17.0")  # shared base (tenant NULL)
    tid = _tenant(rp_pg, "rp_acme_t")
    _profile(rp_pg, "rp_acme_17", "17.0", tenant_id=tid, parent_id=base)
    ctx(tid)
    _pin_profile("rp_acme_17")

    scope = _srv._scope(None)  # caller omits profile_name → pin injected
    assert scope["own"] == ["rp_acme_17"], (
        "omitting profile_name must inherit the session-pinned profile (#251)"
    )
    assert "rp_base_17" in scope["shared"], "shared base must remain visible"

    allowed = _srv._effective_allowed(None)
    assert allowed == ["rp_acme_17"], "single-value filter narrows to the pinned profile"


# ---------------------------------------------------------------------------
# 2. explicit profile_name overrides the pin
# ---------------------------------------------------------------------------


def test_explicit_profile_overrides_pin(ctx, rp_pg):
    """An explicit (in-scope) profile_name wins over a different pinned profile."""
    from src.mcp import server as _srv

    base = _profile(rp_pg, "rp_base_17", "17.0")
    tid = _tenant(rp_pg, "rp_two_t")
    _profile(rp_pg, "rp_acme_17", "17.0", tenant_id=tid, parent_id=base)
    _profile(rp_pg, "rp_globex_17", "17.0", tenant_id=tid, parent_id=base)
    ctx(tid)
    _pin_profile("rp_acme_17")  # pin acme...

    scope = _srv._scope("rp_globex_17")  # ...but explicitly ask for globex
    assert scope["own"] == ["rp_globex_17"], "explicit profile_name must override the pin"

    allowed = _srv._effective_allowed("rp_globex_17")
    assert allowed == ["rp_globex_17"]


# ---------------------------------------------------------------------------
# 3. FAIL-CLOSED: an out-of-scope pinned profile yields deny-all
# ---------------------------------------------------------------------------


def test_out_of_scope_pin_fails_closed(ctx, rp_pg):
    """A scoped tenant pins a profile it does NOT own/share → deny-all. The pin
    can never widen scope nor borrow another tenant's profile (criterion 3)."""
    from src.mcp import server as _srv

    base = _profile(rp_pg, "rp_base_17", "17.0")
    t_me = _tenant(rp_pg, "rp_me_t")
    t_other = _tenant(rp_pg, "rp_other_t")
    _profile(rp_pg, "rp_me_17", "17.0", tenant_id=t_me, parent_id=base)
    # A profile owned by ANOTHER tenant — out of my scope.
    _profile(rp_pg, "rp_other_17", "17.0", tenant_id=t_other, parent_id=base)
    ctx(t_me)
    _pin_profile("rp_other_17")  # pin a profile I am NOT entitled to

    scope = _srv._scope(None)
    assert scope == {"own": [], "shared": []}, (
        "an out-of-scope pin must fail-closed to deny-all, never widen scope (#251 / ADR-0034)"
    )

    allowed = _srv._effective_allowed(None)
    assert allowed == [], "single-value filter must deny-all for an out-of-scope pin"


def test_pin_cannot_invent_unregistered_profile(ctx, rp_pg):
    """Pinning a profile that does not exist at all (and is not in scope) also
    fail-closes for a scoped tenant."""
    from src.mcp import server as _srv

    base = _profile(rp_pg, "rp_base_17", "17.0")
    t_me = _tenant(rp_pg, "rp_solo_t")
    _profile(rp_pg, "rp_solo_17", "17.0", tenant_id=t_me, parent_id=base)
    ctx(t_me)
    _pin_profile("rp_ghost_does_not_exist")

    assert _srv._scope(None) == {"own": [], "shared": []}
    assert _srv._effective_allowed(None) == []


# ---------------------------------------------------------------------------
# 4. admin (tenant None) with no pin stays unrestricted
# ---------------------------------------------------------------------------


def test_admin_no_pin_stays_unrestricted(ctx, rp_pg):
    """An admin / global key (tenant_id None) with NO pin is unrestricted."""
    from src.mcp import server as _srv

    ctx(None)  # admin / global key
    # No pin set.

    scope = _srv._scope(None)
    assert scope["own"] is None, "admin with no pin must stay unrestricted (own=None)"
    assert _srv._effective_allowed(None) is None, "admin single-value filter is None (no filter)"


# ---------------------------------------------------------------------------
# 5. clearing the pin reverts to the full tenant boundary
# ---------------------------------------------------------------------------


def test_clear_pin_reverts_to_full_scope(ctx, rp_pg):
    """After clearing the profile pin, _scope(None) returns the full (own, shared)
    boundary again — widening back from the single narrowed profile."""
    from src.mcp import server as _srv
    from src.mcp.session import set_active_profile_db

    base = _profile(rp_pg, "rp_base_17", "17.0")
    tid = _tenant(rp_pg, "rp_clear_t")
    # Two owned profiles so a narrowing pin (1) is observably different from the
    # cleared full boundary (2).
    _profile(rp_pg, "rp_clear_a_17", "17.0", tenant_id=tid, parent_id=base)
    _profile(rp_pg, "rp_clear_b_17", "17.0", tenant_id=tid, parent_id=base)
    ctx(tid)

    _pin_profile("rp_clear_a_17")
    assert _srv._scope(None)["own"] == ["rp_clear_a_17"], "pin narrows to one profile"

    # Clear the pin → scope widens back to the tenant's full own boundary.
    assert set_active_profile_db(PK, None, SESS) is True
    scope = _srv._scope(None)
    assert set(scope["own"]) == {"rp_clear_a_17", "rp_clear_b_17"}, (
        "with the pin cleared, scope returns the tenant's full own boundary, not the narrowed view"
    )
    assert "rp_base_17" in scope["shared"]
