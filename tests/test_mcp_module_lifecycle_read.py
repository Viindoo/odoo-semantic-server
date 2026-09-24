# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_mcp_module_lifecycle_read.py
"""check_module_exists / describe_module tell the truth about a module's lifecycle (#378).

Business rules protected here (plan R08-R19, review M7/L2/L6/L9, F14/F18). The
expected values come from the rules and the real cases in the #378 survey
(artifacts 04/05), never from what the implementation happens to print:

- M7/R14 a module is indexed only when a profile visible to the caller OWNS it.
         A dependency stub (a node that only exists because something lists
         it in ``depends``) answers ``Indexed: No`` even to an admin key, and
         the answer says who depends on it.
- R15    tenant X never sees tenant Y's private module, its ledger rows, its
         successors, its other-version presence or its stub dependents
         (also with the ledger read running as ``osm_reader`` under RLS).
- R08/L2 YES shows when the owning repo was last seen, at THAT repo's HEAD;
- L6     a node shipped by two repos (``viin_meeting_room`` in tvtmaaddons CE
         and erponline-enterprise EE for five years) shows one Last seen line
         per repo, each with its own repo's HEAD.
- R09    ``test_pylint``@17.0 (renamed to ``test_viin_pylint`` by tvtmaaddons
         ``0240c6b77f``, 2026-09-11) answers No with the removing commit
         verbatim, ``Renamed to: test_viin_pylint`` and a Next: to it.
- R10    an ``installable: False`` manifest (the 19.0 frontier:
         ``viin_fleet_booking_approval`` flipped in ``a057495728``) says so.
- R11    asking the OLD name finds the new module through its manifest
         ``old_technical_name`` (``l10n_vn_viin_account_balance_carry_forward``
         -> ``l10n_vn_viin_account_auto_transfer``).
- R12/F18 the per-version presence list is explicit and numeric
         (``payment_ogone`` is present v8-v12, gone v13-v14, back v15-v17);
         an X.1 minor sorts between X.0 and X+1.0 and 3-digit majors sort last.
- R13    a reused name (``website_project``: "Public Projects" v8/v10, an
         unrelated "Online Task Submission" v18/v19) is listed per version and
         never merged into one history.
- R16    ledger unreachable -> exactly one ``Lifecycle: unavailable (ledger
         unreachable)`` line, the graph-derived answer still rendered, no error.
- R17    describe_module's NO branch carries the same lifecycle block and always
         ends with a Next: footer that never suggests check_module_exists.
- R18    without profile_name the NO line says "any accessible profile".
- R19    a multi-line manifest summary / name / author renders on one line, so
         the ADR-0023 tree stays valid.

Versions: the asked version is ``TEST_VERSION`` (99.0); other-version presence
uses the test-only slots in ``_OTHER_VERSIONS`` (never a real Odoo version, so
the per-test wipe can never touch real data).
"""
from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg2
import psycopg2.extras
import pytest

from tests.conftest import TEST_VERSION

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]

V = TEST_VERSION  # "99.0"
V_PREV = "98.0"
_OGONE_PRESENT = ["61.0", "62.0", "63.0", "64.0", "65.0", "101.0", "101.1", "102.0"]
_OTHER_VERSIONS = sorted({V_PREV, *_OGONE_PRESENT, "66.0"})

STD = "lr_std"  # a globally shared profile (tenant_id NULL)

# tvtmaaddons 0240c6b77f - the #378 rename (artifact 05 section 2).
RENAME_SHA = "0240c6b77fd567422440d6962d536da81866e12a"
RENAME_DATE = "2026-09-11T08:36:18+07:00"
RENAME_SUBJECT = "[REF] test_pylint: rename the module to test_viin_pylint"
# tvtmaaddons17 HEAD that recorded the retirement (review L2: the owning repo's
# HEAD, not odoo17's 9f1f07b).
TVTMA_HEAD = "281607a6d0c1b0e7a4f63c2d9b8e5f1a0c3d7e92"
ODOO_HEAD = "9f1f07b3c5a2e8d4f6b1a0c9e7d3f5b2a8c4e6d1"
PRE_RENAME_HEAD = "5d3e0a1b7c9f2e4d6a8b0c1e3f5a7d9b2c4e6f80"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _wipe_versions(driver) -> None:
    with driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version IN $vs DETACH DELETE n",
              vs=[V, *_OTHER_VERSIONS])


@pytest.fixture
def graph(clean_neo4j):
    _wipe_versions(clean_neo4j)
    yield clean_neo4j
    _wipe_versions(clean_neo4j)


@pytest.fixture
def ledger(clean_pg):
    from src.db.migrate import run_migrations
    from src.db.module_presence import ModulePresenceStore
    from src.db.pg import get_pool

    run_migrations(clean_pg)
    store = ModulePresenceStore(get_pool(), lock_wait_seconds=5.0)
    return store


def _hubs():
    """Every server-module generation the tool bodies read the hub from."""
    from src.mcp import describe
    from src.mcp.tools import guidance

    seen, out = set(), []
    for hub in (guidance._srv, describe._srv, sys.modules.get("src.mcp.server")):
        if hub is not None and id(hub) not in seen:
            seen.add(id(hub))
            out.append(hub)
    return out


@pytest.fixture
def hub(graph, monkeypatch):
    """Tool hub wired to the test Neo4j; the caller is admin unless as_tenant() says otherwise."""
    from src.mcp import session

    for h in _hubs():
        monkeypatch.setattr(h, "_get_driver", lambda: graph)
    session.invalidate_allowed_profiles()
    yield _hubs()[0]
    session.invalidate_allowed_profiles()


@contextmanager
def as_tenant(tenant_id):
    """Pin the request tenant (None = admin) on every hub generation."""
    from src.mcp import session

    session.invalidate_allowed_profiles()
    tokens = [(h, h._tenant_id_var.set(tenant_id)) for h in _hubs()]
    try:
        yield
    finally:
        for h, tok in tokens:
            h._tenant_id_var.reset(tok)
        session.invalidate_allowed_profiles()


def _ledger_down(monkeypatch):
    @contextmanager
    def _unreachable():
        raise psycopg2.OperationalError("could not connect to server: Connection refused")
        yield  # pragma: no cover

    for h in _hubs():
        monkeypatch.setattr(h, "_checkout_pg", _unreachable)


# ---------------------------------------------------------------------------
# Seed helpers (graph via the writer, ledger via ModulePresenceStore)
# ---------------------------------------------------------------------------

def _writer():
    from src.indexer.writer_neo4j import Neo4jWriter

    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    return w


def _module(name, *, version=V, repo="tvtmaaddons", profiles=(STD,), depends=(), **kw):
    from src.indexer.models import ModuleInfo, ParseResult

    info = ModuleInfo(name=name, odoo_version=version, repo=repo,
                      path=f"/srv/repos/{version}/{repo}/{name}", depends=list(depends),
                      version_raw=kw.pop("version_raw", ""), **kw)
    w = _writer()
    try:
        w.write_results([ParseResult(module=info, models=[])], profiles=list(profiles))
    finally:
        w.close()


def _stamp(name, *, head, at, repos, version=V):
    w = _writer()
    try:
        w.stamp_module_presence(version, [{"name": name, "repos": list(repos)}], head, now=at)
    finally:
        w.close()


def _q(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def _tenant(conn, name):
    return _q(conn, "INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,))[0]["id"]


def _profile(conn, name, *, tenant_id=None, version=V):
    return _q(conn, "INSERT INTO profiles (name, odoo_version, tenant_id) "
                    "VALUES (%s, %s, %s) RETURNING id", (name, version, tenant_id))[0]["id"]


def _repo(conn, profile_id, basename, *, branch=V):
    return _q(conn, "INSERT INTO repos (profile_id, url, branch, local_path) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
              (profile_id, f"git@github.com:Viindoo/{basename}.git", branch,
               f"/srv/repos/{branch}/{basename}"))[0]["id"]


def _observe(store, repo_id, profile, head, modules, *, at, version=V):
    from src.db.module_presence import ObservedModule

    obs = [m if isinstance(m, ObservedModule)
           else ObservedModule(name=m, path=m, manifest_file="__manifest__.py")
           for m in modules]
    store.commit_observed(repo_id, profile_name=profile, odoo_version=version,
                          head_sha=head, observed=obs, observed_at=at)


def _retire(store, repo_id, name, *, head, sha=None, when=None, subject=None,
            successors=None, source="git_rename", reason="absent"):
    from src.db.module_presence import RetireEvidence, Successor

    evidence = RetireEvidence(sha=sha, date=when, subject=subject) if sha else None
    successor = Successor(names=tuple(successors), source=source) if successors else None
    assert store.commit_retired(repo_id, name, reason=reason, evidence=evidence,
                                successor=successor, head_sha=head)


def _utc_day(value) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d")


def _ledger_row(conn, repo_id, name):
    return _q(conn, "SELECT * FROM module_presence WHERE repo_id = %s AND name = %s",
              (repo_id, name))[0]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_TOP = ("├─ ", "└─ ")
_SUB_OPEN = ("│   ├─ ", "│   └─ ")
_SUB_LAST = ("    ├─ ", "    └─ ")


def _assert_adr0023_tree(out: str) -> None:
    """Header + one-line tree items, one closing ``└─`` at the root, closed sublists."""
    assert "None" not in out, out
    lines = out.rstrip("\n").split("\n")
    assert len(lines) >= 2, out
    assert not lines[0].startswith(("├", "└", "│", " ")), out
    parent = None
    for i, ln in enumerate(lines[1:], start=1):
        if ln.startswith(_TOP):
            assert parent != "closed", f"line after the root └─: {ln!r}\n{out}"
            prev = lines[i - 1]
            assert not prev.startswith(("│   ├─ ", "    ├─ ")), (
                f"sublist not closed with └─ before {ln!r}\n{out}")
            parent = "closed" if ln.startswith("└─ ") else "open"
        elif ln.startswith(_SUB_OPEN):
            assert parent == "open" and not lines[i - 1].startswith(
                ("│   └─ ", "    └─ ")), f"stray sub-item {ln!r}\n{out}"
        elif ln.startswith(_SUB_LAST):
            assert parent == "closed", f"'    ' sub-item not under the root └─: {ln!r}\n{out}"
        else:
            raise AssertionError(f"line {i} is not a tree item: {ln!r}\n{out}")
    assert parent == "closed", f"tree not closed by a root └─ line:\n{out}"


def _lines(out: str) -> list[str]:
    return out.rstrip("\n").split("\n")


def _line(out: str, head: str) -> str:
    """The single top-level line starting with *head* (asserted, never StopIteration)."""
    found = [ln for ln in _lines(out) if ln.startswith(head)]
    assert len(found) == 1, f"expected one {head!r} line:\n{out}"
    return found[0]


def _block(out: str, head: str) -> list[str]:
    """The top-level line starting with *head* plus its ``│   `` sub-items."""
    lines = _lines(out)
    starts = [i for i, ln in enumerate(lines) if ln.startswith(head)]
    assert len(starts) == 1, f"expected one {head!r} line:\n{out}"
    i = starts[0]
    block = [lines[i]]
    for ln in lines[i + 1:]:
        if not ln.startswith(("│   ", "    ")):
            break
        block.append(ln)
    return block


def _check(name, profile_name=None, version=V):
    from src.mcp.tools import guidance

    return guidance._srv._check_module_exists(name, version, profile_name=profile_name)


def _describe(name, profile_name=None, version=V):
    from src.mcp import describe

    return describe._srv._describe_module(name, version, profile_name)


# ---------------------------------------------------------------------------
# Shared worlds
# ---------------------------------------------------------------------------

@pytest.fixture
def std(ledger, pg_conn):
    """One shared profile with tvtmaaddons + odoo repos at 99.0."""
    pid = _profile(pg_conn, STD)
    return {
        "pid": pid,
        "tvtma": _repo(pg_conn, pid, "tvtmaaddons"),
        "odoo": _repo(pg_conn, pid, "odoo"),
    }


@pytest.fixture
def test_pylint_world(hub, ledger, std, pg_conn):
    """#378: tvtmaaddons 0240c6b77f renamed test_pylint -> test_viin_pylint at 17.0.

    Graph: test_viin_pylint owned at 99.0 (17.0); test_pylint still present at
    98.0 (16.0). Ledger: test_pylint last seen at the pre-rename HEAD, retired
    with the rename commit as evidence, recorded at tvtmaaddons HEAD 281607a.
    """
    _module("test_viin_pylint")
    _module("test_pylint", version=V_PREV)
    _observe(ledger, std["tvtma"], STD, PRE_RENAME_HEAD, ["test_pylint"],
             at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC))
    _observe(ledger, std["tvtma"], STD, RENAME_SHA, ["test_viin_pylint"],
             at=datetime(2026, 9, 11, 2, 0, tzinfo=UTC))
    _retire(ledger, std["tvtma"], "test_pylint", head=TVTMA_HEAD, sha=RENAME_SHA,
            when=RENAME_DATE, subject=RENAME_SUBJECT, successors=["test_viin_pylint"])
    row = _ledger_row(pg_conn, std["tvtma"], "test_pylint")
    return {"recorded_on": _utc_day(row["state_changed_at"])}


def _expected_test_pylint_block(recorded_on):
    return [
        f"├─ Lifecycle (tvtmaaddons, profile {STD}, branch {V}):",
        f"│   ├─ State: retired - removed from branch {V} "
        f"(recorded at HEAD 281607a on {recorded_on})",
        "│   ├─ Last seen: HEAD 5d3e0a1 on 2026-09-10",
        f'│   ├─ Removing commit: 0240c6b77f 2026-09-11 "{RENAME_SUBJECT}"',
        "│   └─ Renamed to: test_viin_pylint (git rename)",
    ]


# ---------------------------------------------------------------------------
# R09 - the #378 case end to end
# ---------------------------------------------------------------------------

class TestRenamedModuleAnswersNoAndPointsAtTheSuccessor:
    def test_test_pylint_at_17_shows_removing_commit_rename_and_next(self, test_pylint_world):
        out = _check("test_pylint")
        _assert_adr0023_tree(out)
        lines = _lines(out)
        assert lines[1] == "├─ Indexed:         No", out
        assert _block(out, "├─ Lifecycle (") == _expected_test_pylint_block(
            test_pylint_world["recorded_on"]), out
        assert f"├─ Present at other versions: {V_PREV} [tvtmaaddons]" in lines, out
        assert lines[-1] == (
            f"└─ Next: check_module_exists(name='test_viin_pylint', odoo_version='{V}') "
            "for the successor"), out

    def test_no_branch_sections_follow_the_contract_order(self, test_pylint_world):
        out = _check("test_pylint")
        lines = _lines(out)
        order = [
            lines.index(_line(out, p))
            for p in ("├─ Indexed:", "├─ Lifecycle (", "├─ Present at other versions:",
                      "├─ Is EE confusion:", "└─ Next:")
        ]
        assert order == sorted(order), out

    def test_the_successor_itself_is_indexed(self, test_pylint_world):
        # GUARD: pre-existing behaviour
        out = _check("test_viin_pylint")
        assert "├─ Indexed:         Yes" in _lines(out), out

    def test_merged_module_shows_its_removing_commit_without_a_successor(
            self, hub, ledger, std):
        """viin_ai_rag: '[MERGE] viin_ai_rag: merge into viin_ai' (2026-08-29), no rename."""
        _module("viin_ai")
        _observe(ledger, std["tvtma"], STD, "1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d",
                 ["viin_ai", "viin_ai_rag"], at=datetime(2026, 8, 28, tzinfo=UTC))
        _retire(ledger, std["tvtma"], "viin_ai_rag", head=TVTMA_HEAD,
                sha="7e6d5c4b3a29180716253443526170819a0b1c2d", when="2026-08-29T10:12:00+07:00",
                subject="[MERGE] viin_ai_rag: merge into viin_ai")
        out = _check("viin_ai_rag")
        _assert_adr0023_tree(out)
        block = _block(out, "├─ Lifecycle (")
        assert (
            '│   └─ Removing commit: 7e6d5c4b3a 2026-08-29 '
            '"[MERGE] viin_ai_rag: merge into viin_ai"') in block, out
        assert "Renamed to" not in out, out
        assert _lines(out)[-1] == (
            f"└─ Not indexed at {V} in any accessible profile. Verify the module name, "
            "or call list_available_profiles to see indexed scope."), out

    def test_rename_chain_shows_renamed_from_and_next_to_the_newest_name(
            self, hub, ledger, std):
        """sale_coupon -> coupon -> loyalty (artifact 04 section 4.4) asked at 'coupon'."""
        _module("loyalty", repo="odoo")
        at = datetime(2026, 1, 5, tzinfo=UTC)
        _observe(ledger, std["odoo"], STD, ODOO_HEAD, ["sale_coupon", "coupon"], at=at)
        _observe(ledger, std["odoo"], STD, ODOO_HEAD, ["loyalty"], at=at)
        _retire(ledger, std["odoo"], "sale_coupon", head=ODOO_HEAD, successors=["coupon"])
        _retire(ledger, std["odoo"], "coupon", head=ODOO_HEAD, successors=["loyalty"])
        out = _check("coupon")
        _assert_adr0023_tree(out)
        lines = _lines(out)
        assert "│   └─ Renamed to: loyalty (git rename)" in _block(out, "├─ Lifecycle ("), out
        assert "├─ Renamed from:    sale_coupon (odoo, retired)" in lines, out
        assert lines[-1] == (
            f"└─ Next: check_module_exists(name='loyalty', odoo_version='{V}') "
            "for the successor"), out


# ---------------------------------------------------------------------------
# Ledger state texts (R10 + the other rows the NO block can meet)
# ---------------------------------------------------------------------------

class TestLedgerStateLines:
    def test_installable_false_manifest_says_it_is_not_indexed_and_why(self, hub, ledger, std):
        """19.0 frontier: viin_fleet_booking_approval flipped installable:False (a057495728)."""
        from src.db.module_presence import ObservedModule

        _observe(ledger, std["tvtma"], STD, "a0574957281f3e5d7c9b1a3e5f7d9c1b3a5e7f9d", [
            ObservedModule(name="viin_fleet_booking_approval",
                           path="viin_fleet_booking_approval",
                           manifest_file="__manifest__.py", state="excluded",
                           exclusion_reason="installable_false",
                           version_raw="18.0.1.0.0", version_mismatch=True),
        ], at=datetime(2026, 7, 22, 4, 0, tzinfo=UTC))
        out = _check("viin_fleet_booking_approval")
        _assert_adr0023_tree(out)
        assert _block(out, "├─ Lifecycle (") == [
            f"├─ Lifecycle (tvtmaaddons, profile {STD}, branch {V}):",
            f"│   ├─ State: on branch {V} at HEAD a057495 on 2026-07-22 "
            "but not indexed: installable: False",
            "│   └─ Manifest version: 18.0.1.0.0 (does not match the branch version)",
        ], out

    @pytest.mark.parametrize(("reason", "text"), [
        ("absent", f"retired - removed from branch {V}"),
        ("repo_removed", "retired - the repository was removed from the profile"),
        ("orphan_sweep", "retired - no repository ships it any more (orphan sweep)"),
    ])
    def test_retire_reason_is_spelled_out(self, hub, ledger, std, pg_conn, reason, text):
        _observe(ledger, std["tvtma"], STD, PRE_RENAME_HEAD, ["to_accounting_bi"],
                 at=datetime(2026, 7, 2, tzinfo=UTC))
        _retire(ledger, std["tvtma"], "to_accounting_bi", head=TVTMA_HEAD, reason=reason)
        day = _utc_day(_ledger_row(pg_conn, std["tvtma"], "to_accounting_bi")["state_changed_at"])
        block = _block(_check("to_accounting_bi"), "├─ Lifecycle (")
        assert block[1] == f"│   ├─ State: {text} (recorded at HEAD 281607a on {day})", block

    def test_present_in_ledger_but_no_visible_node_is_reported(self, hub, ledger, std):
        _observe(ledger, std["tvtma"], STD, TVTMA_HEAD, ["viin_queue"],
                 at=datetime(2026, 9, 12, tzinfo=UTC))
        block = _block(_check("viin_queue"), "├─ Lifecycle (")
        assert block[1:] == [
            f"│   └─ State: present on branch {V} at HEAD 281607a on 2026-09-12, "
            "but no module node is visible in this scope",
        ], block

    def test_ledger_rows_of_other_versions_are_not_this_versions_history(
            self, hub, ledger, std, pg_conn):
        """website_project retired at v11 (merged into project) says nothing about v17."""
        # GUARD: pre-existing behaviour
        pid = _profile(pg_conn, "lr_v64", version="64.0")
        r64 = _repo(pg_conn, pid, "odoo", branch="64.0")
        _observe(ledger, r64, "lr_v64", ODOO_HEAD, ["website_project"],
                 at=datetime(2017, 9, 1, tzinfo=UTC), version="64.0")
        _retire(ledger, r64, "website_project", head=ODOO_HEAD, successors=["project"],
                source="old_technical_name")
        out = _check("website_project")
        assert "Lifecycle" not in out and "Renamed to" not in out, out


# ---------------------------------------------------------------------------
# R11 - reverse old_technical_name lookup
# ---------------------------------------------------------------------------

OLD_CF = "l10n_vn_viin_account_balance_carry_forward"
NEW_CF = "l10n_vn_viin_account_auto_transfer"


class TestOldNameFindsTheNewModule:
    def test_old_technical_name_points_at_the_new_module(self, hub, ledger, std):
        _module(NEW_CF, old_technical_name=OLD_CF)
        out = _check(OLD_CF)
        _assert_adr0023_tree(out)
        lines = _lines(out)
        assert lines[1] == "├─ Indexed:         No", out
        assert (f"├─ Renamed to:      {NEW_CF} "
                f"(declares old_technical_name '{OLD_CF}' at {V})") in lines, out
        assert lines[-1] == (
            f"└─ Next: check_module_exists(name='{NEW_CF}', odoo_version='{V}') "
            "for the successor"), out

    def test_a_declaration_at_another_version_is_not_a_successor_here(self, hub, ledger, std):
        # GUARD: pre-existing behaviour
        _module(NEW_CF, version=V_PREV, old_technical_name=OLD_CF)
        out = _check(OLD_CF)
        assert "Renamed to" not in out and "for the successor" not in out, out


# ---------------------------------------------------------------------------
# R12 / R13 / F18 - per-version presence
# ---------------------------------------------------------------------------

class TestPresenceAtOtherVersions:
    @pytest.fixture
    def ogone(self, hub, ledger, std):
        """payment_ogone v8-v12, gone v13/v14 (payment_ingenico), back v15-v17.

        Asked in the gap (99.0 plays v13). 101.1 is a saas-style X.1 minor; the
        3-digit majors prove the order is numeric, not lexicographic.
        """
        for v in _OGONE_PRESENT:
            _module("payment_ogone", version=v, repo="odoo")
        _module("payment_ingenico", repo="odoo")
        _module("payment_ingenico", version="66.0", repo="odoo")

    def test_versions_are_listed_one_by_one_in_numeric_order(self, ogone):
        out = _check("payment_ogone")
        _assert_adr0023_tree(out)
        expected = ", ".join(f"{v} [odoo]" for v in _OGONE_PRESENT)
        assert f"├─ Present at other versions: {expected}" in _lines(out), out

    def test_describe_points_at_the_numerically_newest_version(self, ogone):
        out = _describe("payment_ogone")
        _assert_adr0023_tree(out)
        assert _lines(out)[-1] == (
            "└─ Next: describe_module(name='payment_ogone', odoo_version='102.0') "
            "for the newest version where it is indexed"), out

    def test_reused_name_is_listed_per_version_without_merging_histories(self, hub, ledger, std):
        """website_project: 'Public Projects' v8/v10, unrelated 'Online Task Submission' v18/v19."""
        for v in ("61.0", "63.0"):
            _module("website_project", version=v, repo="odoo",
                    shortdesc="Public Projects", summary="Publish Your Public Projects")
        for v in ("101.0", "102.0"):
            _module("website_project", version=v, repo="odoo",
                    shortdesc="Online Task Submission",
                    summary="Add a task suggestion form to your website")
        out = _check("website_project")
        _assert_adr0023_tree(out)
        assert ("├─ Present at other versions: 61.0 [odoo], 63.0 [odoo], "
                "101.0 [odoo], 102.0 [odoo]") in _lines(out), out
        for identity in ("Public Projects", "Online Task Submission", "Renamed"):
            assert identity not in out, out
        new = _check("website_project", version="102.0")
        assert "Add a task suggestion form to your website" in new, new
        assert "Publish Your Public Projects" not in new, new

    def test_repos_per_version_are_named_in_brackets(self, hub, ledger, std):
        _module("viin_meeting_room", version="61.0", repo="tvtmaaddons")
        _module("viin_meeting_room", version="102.0", repo="erponline-enterprise")
        out = _check("viin_meeting_room")
        assert ("├─ Present at other versions: 61.0 [tvtmaaddons], "
                "102.0 [erponline-enterprise]") in _lines(out), out


# ---------------------------------------------------------------------------
# R14 / M7 - dependency stubs are not modules
# ---------------------------------------------------------------------------

class TestDependencyStubIsNotIndexed:
    def test_stub_answers_no_to_admin_and_names_its_dependents(self, hub, ledger, std):
        _module("viin_account_asset", depends=["account_asset"])
        with as_tenant(None):
            out = _check("account_asset")
        _assert_adr0023_tree(out)
        lines = _lines(out)
        assert lines[1] == "├─ Indexed:         No", out
        assert (f"├─ Dependency stub: listed in 'depends' of 1 indexed module(s) at {V} "
                "(viin_account_asset), not itself indexed") in lines, out

    def test_many_dependents_are_previewed_five_at_most(self, hub, ledger, std):
        dependents = [f"viin_hr_x{i}" for i in range(7)]
        for d in dependents:
            _module(d, depends=["hr_payroll"])
        out = _check("hr_payroll")
        line = _line(out, "├─ Dependency stub:")
        m = re.fullmatch(
            rf"├─ Dependency stub: listed in 'depends' of 7 indexed module\(s\) at {V} "
            r"\((.+), \.\.\. and 2 more\), not itself indexed", line)
        assert m, line
        shown = m.group(1).split(", ")
        assert len(shown) == 5 and set(shown) <= set(dependents), line

    def test_profile_less_node_is_not_a_yes_even_with_a_path(self, hub, ledger, std):
        """M7: a node with no owning profile (pre-ADR-0034 leftovers) is not an indexed module."""
        _module("legacy_ownerless", profiles=())
        with as_tenant(None):
            assert "├─ Indexed:         No" in _lines(_check("legacy_ownerless"))

    def test_describe_module_treats_a_stub_as_not_indexed(self, hub, ledger, std):
        _module("viin_account_asset", depends=["account_asset"])
        out = _describe("account_asset")
        _assert_adr0023_tree(out)
        assert _lines(out)[0] == f"No module named 'account_asset' indexed for Odoo {V}.", out
        assert any(ln.startswith("├─ Dependency stub:") for ln in _lines(out)), out


# ---------------------------------------------------------------------------
# R08 / L2 / L6 - freshness on the YES branch
# ---------------------------------------------------------------------------

class TestLastSeenOnYes:
    def test_single_owner_shows_its_own_repos_head_and_date(self, hub, ledger, std):
        _module("test_viin_pylint")
        _module("base", repo="odoo")
        _stamp("test_viin_pylint", head=TVTMA_HEAD, at=datetime(2026, 9, 12, 3, tzinfo=UTC),
               repos=["tvtmaaddons"])
        _stamp("base", head=ODOO_HEAD, at=datetime(2026, 9, 12, 4, tzinfo=UTC), repos=["odoo"])
        out = _check("test_viin_pylint")
        _assert_adr0023_tree(out)
        assert "├─ Last seen:       HEAD 281607a on 2026-09-12 [tvtmaaddons]" in _lines(out), out
        assert "9f1f07b" not in out, out

    def test_single_owner_freshness_needs_no_ledger(self, hub, ledger, std, monkeypatch):
        _module("test_viin_pylint")
        _stamp("test_viin_pylint", head=TVTMA_HEAD, at=datetime(2026, 9, 12, 3, tzinfo=UTC),
               repos=["tvtmaaddons"])
        _ledger_down(monkeypatch)
        out = _check("test_viin_pylint")
        assert "├─ Last seen:       HEAD 281607a on 2026-09-12 [tvtmaaddons]" in _lines(out), out
        assert "unavailable" not in out, out

    def test_unstamped_node_says_the_head_is_not_stamped_yet(self, hub, ledger, std):
        _module("test_viin_pylint")
        line = _line(_check("test_viin_pylint"), "├─ Last seen:")
        assert re.fullmatch(r"├─ Last seen:       \d{4}-\d\d-\d\d "
                            r"\(branch HEAD not stamped yet\) \[tvtmaaddons\]", line), line

    @pytest.fixture
    def meeting_room(self, hub, ledger, std, pg_conn):
        """viin_meeting_room shipped by tvtmaaddons (CE) AND erponline-enterprise (EE)."""
        ee = _repo(pg_conn, std["pid"], "erponline-enterprise")
        _module("viin_meeting_room", repo="erponline-enterprise")
        _stamp("viin_meeting_room", head="a3524b8de1f0e2d3c4b5a69788796a5b4c3d2e1f",
               at=datetime(2026, 8, 19, 9, tzinfo=UTC),
               repos=["tvtmaaddons", "erponline-enterprise"])
        _observe(ledger, std["tvtma"], STD, "3a9f0c2e71b8d6c4a2e0f8d6b4c2a0e8f6d4b2c1",
                 ["viin_meeting_room"], at=datetime(2026, 8, 18, 9, tzinfo=UTC))
        _observe(ledger, ee, STD, "a3524b8de1f0e2d3c4b5a69788796a5b4c3d2e1f",
                 ["viin_meeting_room"], at=datetime(2026, 8, 19, 9, tzinfo=UTC))
        return {"ee": ee}

    def test_two_owners_show_one_last_seen_per_repo_with_that_repos_head(self, meeting_room):
        out = _check("viin_meeting_room")
        _assert_adr0023_tree(out)
        lines = _lines(out)
        assert "├─ Repos:           erponline-enterprise, tvtmaaddons" in lines, out
        assert _block(out, "├─ Last seen (per repo):") == [
            "├─ Last seen (per repo):",
            "│   ├─ [erponline-enterprise] HEAD a3524b8 on 2026-08-19",
            "│   └─ [tvtmaaddons] HEAD 3a9f0c2 on 2026-08-18",
        ], out

    def test_a_repo_that_is_retiring_its_copy_is_flagged(self, meeting_room, ledger, std):
        """tvtmaaddons cec517769b '[MOV] viin_meeting_room: move to Viindoo EE'."""
        ledger.mark_retire_pending(std["tvtma"], ["viin_meeting_room"])
        block = _block(_check("viin_meeting_room"), "├─ Last seen (per repo):")
        assert block[-1] == "│   └─ [tvtmaaddons] HEAD 3a9f0c2 on 2026-08-18 (retirement pending)"

    def test_a_repo_that_ships_it_as_not_installable_says_so(self, meeting_room, ledger, std):
        from src.db.module_presence import ObservedModule

        _observe(ledger, std["tvtma"], STD, "3a9f0c2e71b8d6c4a2e0f8d6b4c2a0e8f6d4b2c1", [
            ObservedModule(name="viin_meeting_room", path="viin_meeting_room",
                           manifest_file="__manifest__.py", state="excluded",
                           exclusion_reason="installable_false"),
        ], at=datetime(2026, 8, 18, 9, tzinfo=UTC))
        block = _block(_check("viin_meeting_room"), "├─ Last seen (per repo):")
        assert block[-1] == ("│   └─ [tvtmaaddons] HEAD 3a9f0c2 on 2026-08-18 "
                             "(not indexed from this repo: installable: False)"), block

    def test_an_owner_without_a_ledger_row_is_named_as_untracked(self, meeting_room):
        _stamp("viin_meeting_room", head="a3524b8de1f0e2d3c4b5a69788796a5b4c3d2e1f",
               at=datetime(2026, 8, 19, 9, tzinfo=UTC),
               repos=["tvtmaaddons", "erponline-enterprise", "viindoo-odoo-addons"])
        block = _block(_check("viin_meeting_room"), "├─ Last seen (per repo):")
        assert "│   └─ [viindoo-odoo-addons] not tracked in the ledger yet" in block, block

    def test_two_owners_with_the_ledger_down_fall_back_to_the_graph_stamp(
            self, meeting_room, monkeypatch):
        _ledger_down(monkeypatch)
        out = _check("viin_meeting_room")
        _assert_adr0023_tree(out)
        lines = _lines(out)
        assert "├─ Indexed:         Yes" in lines, out
        assert ("├─ Last seen:       HEAD a3524b8 on 2026-08-19 (latest across repos; "
                "per-repo detail unavailable - ledger unreachable)") in lines, out

    def test_version_note_only_when_the_manifest_version_disagrees(self, hub, ledger, std, graph):
        _module("viin_account_x", version_raw="18.0.1.0.0")
        assert "Version note" not in _check("viin_account_x")
        with graph.session() as s:  # the flag lane-core writes from the ledger (F25)
            s.run("MATCH (m:Module {name:'viin_account_x', odoo_version:$v}) "
                  "SET m.version_mismatch = true", v=V)
        assert (f"├─ Version note:    manifest declares 18.0.1.0.0, indexed at branch "
                f"version {V}") in _lines(_check("viin_account_x"))


# ---------------------------------------------------------------------------
# R19 - multi-line manifest text stays one tree line
# ---------------------------------------------------------------------------

class TestMultiLineManifestTextIsFlattened:
    @pytest.fixture
    def multiline(self, hub, ledger, std):
        _module("test_viin_pylint",
                shortdesc="Test Pylint\n  (Viindoo)",
                summary="Run pylint on the addons\n    and fail the build on errors",
                author="T.V.T Marine Automation (aka TVTMA),\nViindoo")

    def test_check_module_exists_keeps_a_valid_tree(self, multiline):
        out = _check("test_viin_pylint")
        _assert_adr0023_tree(out)
        assert any(ln.endswith("Run pylint on the addons and fail the build on errors")
                   for ln in _lines(out)), out
        assert any(ln.endswith("T.V.T Marine Automation (aka TVTMA), Viindoo")
                   for ln in _lines(out)), out

    def test_describe_module_keeps_a_valid_tree(self, multiline):
        out = _describe("test_viin_pylint")
        _assert_adr0023_tree(out)
        assert "Run pylint on the addons and fail the build on errors" in out, out
        assert "Test Pylint (Viindoo)" in out, out


# ---------------------------------------------------------------------------
# R16 - ledger unreachable
# ---------------------------------------------------------------------------

class TestLedgerUnreachable:
    def test_no_branch_degrades_to_one_line_and_keeps_the_graph_answer(
            self, test_pylint_world, monkeypatch):
        _ledger_down(monkeypatch)
        out = _check("test_pylint")
        _assert_adr0023_tree(out)
        lines = _lines(out)
        assert [ln for ln in lines if "Lifecycle" in ln] == [
            "├─ Lifecycle: unavailable (ledger unreachable)"], out
        assert "Removing commit" not in out and RENAME_SHA[:7] not in out, out
        assert f"├─ Present at other versions: {V_PREV} [tvtmaaddons]" in lines, out
        assert lines[-1].startswith(f"└─ Not indexed at {V} in any accessible profile."), out

    def test_graph_successor_still_answers_when_the_ledger_is_down(
            self, hub, ledger, std, monkeypatch):
        _module(NEW_CF, old_technical_name=OLD_CF)
        _ledger_down(monkeypatch)
        out = _check(OLD_CF)
        assert "├─ Lifecycle: unavailable (ledger unreachable)" in _lines(out), out
        assert _lines(out)[-1] == (
            f"└─ Next: check_module_exists(name='{NEW_CF}', odoo_version='{V}') "
            "for the successor"), out

    def test_describe_module_no_branch_degrades_the_same_way(self, test_pylint_world, monkeypatch):
        _ledger_down(monkeypatch)
        out = _describe("test_pylint")
        _assert_adr0023_tree(out)
        assert "├─ Lifecycle: unavailable (ledger unreachable)" in _lines(out), out
        assert _lines(out)[-1] == (
            f"└─ Next: describe_module(name='test_pylint', odoo_version='{V_PREV}') "
            "for the newest version where it is indexed"), out


# ---------------------------------------------------------------------------
# lane-mcpfix defect 4 - a degraded resource body is served but never cached
# ---------------------------------------------------------------------------

def _read_module_resource(cache, name):
    """odoo://99.0/module/<name> through the real serve path (key + cache + render)."""
    from src.mcp.resources import _render_module, _serve_resource_blocking

    return _serve_resource_blocking(
        cache, V, "module", name, lambda resolved: _render_module(resolved, name))


class TestDegradedModuleResourceIsNotCached:
    """The module resource caches for 300s. A body rendered while the ledger is
    down ("Lifecycle: unavailable") must not outlive the outage: the first read
    after recovery shows the real lifecycle, well inside the TTL."""

    def test_ledger_outage_heals_on_the_next_read(self, test_pylint_world, monkeypatch):
        """FIX: pre-fix the degraded body stayed cached for the whole TTL."""
        from src.mcp.resources import ResourceCache

        cache = ResourceCache(ttl=300)
        with monkeypatch.context() as mp:
            _ledger_down(mp)
            degraded = _read_module_resource(cache, "test_pylint")
        assert "├─ Lifecycle: unavailable (ledger unreachable)" in _lines(degraded), degraded
    
        healed = _read_module_resource(cache, "test_pylint")
        assert "Lifecycle: unavailable" not in healed, healed
        assert f'│   ├─ Removing commit: 0240c6b77f 2026-09-11 "{RENAME_SUBJECT}"' in (
            _lines(healed)), healed

    def test_healthy_body_is_still_served_from_the_cache(self, test_pylint_world, monkeypatch):
        """GUARD: a healthy body is cached - a later ledger outage does not reach it."""
        # GUARD: pre-existing behaviour
        from src.mcp.resources import ResourceCache

        cache = ResourceCache(ttl=300)
        healthy = _read_module_resource(cache, "test_pylint")
        assert "Removing commit" in healthy, healthy
        with monkeypatch.context() as mp:
            _ledger_down(mp)
            again = _read_module_resource(cache, "test_pylint")
        assert again == healthy

    def test_the_tool_call_is_unaffected_by_the_cache_rule(self, test_pylint_world, monkeypatch):
        """GUARD: describe_module (a tool, never cached) renders the same degraded
        body as the resource did - the fix changes caching, not rendering."""
        # GUARD: pre-existing behaviour
        from src.mcp.resources import ResourceCache

        with monkeypatch.context() as mp:
            _ledger_down(mp)
            tool = _describe("test_pylint")
            resource = _read_module_resource(ResourceCache(ttl=300), "test_pylint")
        assert tool == resource, f"TOOL:\n{tool}\nRESOURCE:\n{resource}"


# ---------------------------------------------------------------------------
# R17 - describe_module NO branch
# ---------------------------------------------------------------------------

class TestDescribeModuleNotIndexed:
    def test_renamed_module_carries_the_lifecycle_block_and_describes_the_successor(
            self, test_pylint_world):
        out = _describe("test_pylint")
        _assert_adr0023_tree(out)
        lines = _lines(out)
        assert lines[0] == f"No module named 'test_pylint' indexed for Odoo {V}.", out
        assert _block(out, "├─ Lifecycle (") == _expected_test_pylint_block(
            test_pylint_world["recorded_on"]), out
        assert lines[-1] == (
            f"└─ Next: describe_module(name='test_viin_pylint', odoo_version='{V}') "
            "for the successor"), out
        assert "check_module_exists" not in out, out

    def test_unknown_name_ends_with_the_profiles_listing(self, hub, ledger, std):
        out = _describe("no_such_module_xyz")
        _assert_adr0023_tree(out)
        assert _lines(out)[-1] == "└─ Next: list_available_profiles() to see the indexed scope", out
        assert "check_module_exists" not in out, out


# ---------------------------------------------------------------------------
# R18 + EE-confusion terminal line
# ---------------------------------------------------------------------------

class TestNotIndexedWording:
    def test_without_profile_it_says_any_accessible_profile(self, hub, ledger, std):
        out = _check("no_such_module_xyz")
        assert _lines(out)[-1] == (
            f"└─ Not indexed at {V} in any accessible profile. Verify the module name, "
            "or call list_available_profiles to see indexed scope."), out

    def test_with_profile_it_names_that_profile(self, hub, ledger, std):
        out = _check("no_such_module_xyz", profile_name=STD)
        assert _lines(out)[-1] == (
            f"└─ Not indexed at {V} in profile {STD}. Verify the module name, "
            "or call list_available_profiles to see indexed scope."), out

    def test_ee_confusion_answer_does_not_send_the_agent_to_describe_a_missing_module(
            self, hub, ledger, std):
        out = _check("knowledge")
        _assert_adr0023_tree(out)
        assert "Is EE confusion: Yes" in out, out
        assert "describe_module(" not in out, out
        assert _lines(out)[-1].startswith(f"└─ Not indexed at {V} in any accessible profile."), out


# ---------------------------------------------------------------------------
# R15 - tenant isolation (graph + ledger)
# ---------------------------------------------------------------------------

def _seed_tenants(ledger, conn):
    """acme and globex each own a private profile; lr_std is shared by both.

    Graph: globex's private module (also at another version), a globex module
    that depends on a stub and declares an old_technical_name, and an acme
    module depending on the same stub. Ledger: both tenants retired a module
    of the same name, each with a revealing commit subject.
    """
    std_pid = _profile(conn, STD)
    acme = _tenant(conn, "lr_acme")
    globex = _tenant(conn, "lr_globex")
    acme_pid = _profile(conn, "lr_acme_p", tenant_id=acme)
    globex_pid = _profile(conn, "lr_globex_p", tenant_id=globex)
    r_std = _repo(conn, std_pid, "odoo")
    r_acme = _repo(conn, acme_pid, "acme_addons")
    r_globex = _repo(conn, globex_pid, "globex_addons")
    _module("globex_secret", repo="globex_addons", profiles=["lr_globex_p"])
    _module("globex_secret", version=V_PREV, repo="globex_addons", profiles=["lr_globex_p"])
    _module("globex_billing", repo="globex_addons", profiles=["lr_globex_p"],
            depends=["account_asset"], old_technical_name="acme_old_name")
    _module("acme_payroll", repo="acme_addons", profiles=["lr_acme_p"],
            depends=["account_asset"])
    at = datetime(2026, 9, 1, tzinfo=UTC)
    _observe(ledger, r_globex, "lr_globex_p", TVTMA_HEAD, ["viin_crm_extra"], at=at)
    _retire(ledger, r_globex, "viin_crm_extra", head=TVTMA_HEAD, sha=RENAME_SHA,
            when=RENAME_DATE, subject="[REM] viin_crm_extra: contract ended with Initech",
            successors=["globex_crm2"])
    _observe(ledger, r_acme, "lr_acme_p", TVTMA_HEAD, ["viin_crm_extra"], at=at)
    _retire(ledger, r_acme, "viin_crm_extra", head=TVTMA_HEAD, sha=RENAME_SHA,
            when=RENAME_DATE, subject="[REM] acme copy retired")
    _observe(ledger, r_std, STD, ODOO_HEAD, ["base"], at=at)
    return {"acme": acme, "globex": globex}


class TestTenantIsolation:
    @pytest.fixture
    def tenants(self, hub, ledger, pg_conn):
        return _seed_tenants(ledger, pg_conn)

    def test_other_tenants_private_module_is_not_indexed_for_me(self, tenants):
        # GUARD: pre-existing behaviour
        with as_tenant(tenants["acme"]):
            out = _check("globex_secret")
        assert "├─ Indexed:         No" in _lines(out), out
        assert "globex_addons" not in out and "Present at other versions" not in out, out
        with as_tenant(tenants["globex"]):  # positive control
            assert "├─ Indexed:         Yes" in _lines(_check("globex_secret"))

    def test_asking_for_the_other_tenants_profile_does_not_widen_scope(self, tenants):
        # GUARD: pre-existing behaviour
        with as_tenant(tenants["acme"]):
            out = _check("globex_secret", profile_name="lr_globex_p")
        assert "├─ Indexed:         No" in _lines(out), out
        assert "globex_addons" not in out, out

    def test_ledger_rows_are_filtered_to_my_profiles(self, tenants):
        with as_tenant(tenants["acme"]):
            out = _check("viin_crm_extra")
        assert "[REM] acme copy retired" in out, out
        assert "Initech" not in out and "globex_crm2" not in out, out
        assert "lr_globex_p" not in out, out
        with as_tenant(tenants["globex"]):
            theirs = _check("viin_crm_extra")
        assert "Initech" in theirs and "acme copy" not in theirs, theirs

    def test_admin_sees_every_tenants_ledger_rows(self, tenants):
        with as_tenant(None):
            out = _check("viin_crm_extra")
        assert "Initech" in out and "[REM] acme copy retired" in out, out

    def test_stub_dependents_and_reverse_successors_stay_in_scope(self, tenants):
        with as_tenant(tenants["acme"]):
            stub = _check("account_asset")
            old = _check("acme_old_name")
        assert (f"├─ Dependency stub: listed in 'depends' of 1 indexed module(s) at {V} "
                "(acme_payroll), not itself indexed") in _lines(stub), stub
        assert "globex" not in stub, stub
        assert "globex" not in old and "Renamed to" not in old, old

    def test_under_rls_as_osm_reader_a_tenant_still_sees_only_its_rows(
            self, hub, ledger, clean_pg, _ephemeral_pg_db, monkeypatch):
        """R15 RLS half: the ledger read works as the least-privilege MCP role.

        The role must exist before the migrations run so their GRANTs fire
        (production order: ops/rls_create_osm_reader.sql, then migrate).
        """
        from src.db.migrate import run_migrations
        from tests.conftest import drop_osm_reader, ensure_osm_reader_or_skip, wipe_pg_tables

        ensure_osm_reader_or_skip(clean_pg)
        reader = None
        try:
            try:
                with clean_pg.cursor() as cur:
                    cur.execute("GRANT osm_reader TO CURRENT_USER")
            except psycopg2.errors.InsufficientPrivilege as exc:
                pytest.skip(f"cannot become osm_reader: {exc}")
            wipe_pg_tables(clean_pg)
            run_migrations(clean_pg)
            ids = _seed_tenants(ledger, clean_pg)
            reader = psycopg2.connect(_ephemeral_pg_db)
            reader.autocommit = True
            with reader.cursor() as cur:
                cur.execute("SET ROLE osm_reader")

            @contextmanager
            def _reader_conn():
                yield reader

            for h in _hubs():
                monkeypatch.setattr(h, "_checkout_pg", _reader_conn)
            with as_tenant(ids["acme"]):
                out = _check("viin_crm_extra")
            assert "[REM] acme copy retired" in out, out
            assert "Initech" not in out and "unavailable" not in out, out
        finally:
            if reader is not None:
                reader.close()
            drop_osm_reader(clean_pg)


# ---------------------------------------------------------------------------
# Final review T1 / T2 and E2E-D2 - what a scoped key may read in a lifecycle row
# ---------------------------------------------------------------------------
#
# T1: ``retire_blocked_by`` is operator text. An ``undecidable`` reason names the
# unsynced repos of ANY tenant that may still ship the module (basename + repo
# id), an ``error`` reason carries raw exception text. A scoped key sees only the
# class of the reason; an admin key sees the reason in full.
# T2: a successor recorded from a manifest ``old_technical_name`` may be another
# tenant's private module; "Renamed to" and describe_module's "Next:" never name
# a module the caller cannot see (a ``git_rename`` successor comes from the
# retiring repo's own history and is always shown).
# E2E-D2: a retired row renders "(recorded at HEAD <sha7> on <date>)",
# "(recorded at HEAD <sha7>)" or "(recorded on <date>)" - never a "not stamped"
# claim, never a doubled parenthesis.

GLOBEX_REPO = "globex_private_addons"


def _pending(store, repo_id, name, *, profile, blocked_by, at):
    """acme's repo observed *name*, then flagged it for retirement, blocked."""
    _observe(store, repo_id, profile, TVTMA_HEAD, [name], at=at)
    assert store.mark_retire_pending(repo_id, [name], "absent", blocked_by=blocked_by) == [name]


@pytest.fixture
def blocked_world(hub, ledger, pg_conn):
    """acme (tenant) retires names that are held back by reasons naming globex."""
    acme = _tenant(pg_conn, "lr_acme")
    globex = _tenant(pg_conn, "lr_globex")
    acme_pid = _profile(pg_conn, "lr_acme_p", tenant_id=acme)
    globex_pid = _profile(pg_conn, "lr_globex_p", tenant_id=globex)
    r_acme = _repo(pg_conn, acme_pid, "acme_addons")
    r_globex = _repo(pg_conn, globex_pid, GLOBEX_REPO)
    at = datetime(2026, 9, 20, tzinfo=UTC)
    reasons = {
        # Written by reconcile.pending for a name an unsynced repo may ship.
        "acme_undecidable": (
            f"undecidable: acme_undecidable@{V} kept: repo(s) {GLOBEX_REPO} "
            f"(repo id={r_globex}) [never synced] not synced"
        ),
        # Written by reconcile._error: the exception class and its raw message.
        "acme_error": (
            f"error: acme_error@{V} not retired (ServiceUnavailable: Couldn't connect to "
            "10.20.30.40:7687 as neo4j_prod_admin)"
        ),
        "acme_gated": "gate:scan_incomplete,mass_retire",
        "acme_odd_gate": f"gate:mass_retire,{GLOBEX_REPO}",
        "acme_no_retire": "no_retire",
        "acme_recent": "skipped_recent",
    }
    for name, reason in reasons.items():
        _pending(ledger, r_acme, name, profile="lr_acme_p", blocked_by=reason, at=at)
    return {"acme": acme, "globex": globex, "r_globex": r_globex, "reasons": reasons}


def _pending_line(out: str) -> str:
    lines = [ln for ln in _block(out, "├─ Lifecycle (") if "Retirement pending:" in ln]
    assert len(lines) == 1, out
    return lines[0]


class TestScopedKeySeesOnlyTheBlockerKind:
    """T1 (FIX): the reason a pending retirement waits for, per caller."""

    @pytest.mark.parametrize(("name", "expected"), [
        ("acme_undecidable",
         "undecidable (another repository that may ship it is not synced yet)"),
        ("acme_error", "error (retirement failed; retried by the next run)"),
        ("acme_gated", "gate: scan_incomplete, mass_retire"),
        ("acme_odd_gate", "gate"),
        ("acme_no_retire", "no_retire (--no-retire run)"),
        ("acme_recent", "skipped_recent (re-written by a concurrent run)"),
    ])
    def test_scoped_key_sees_the_class_of_the_reason_only(self, blocked_world, name, expected):
        with as_tenant(blocked_world["acme"]):
            out = _check(name)
        _assert_adr0023_tree(out)
        assert _pending_line(out).endswith(f"Retirement pending: absent (blocked by {expected})"), (
            out)

    @pytest.mark.parametrize("name", ["acme_undecidable", "acme_error", "acme_odd_gate"])
    def test_no_foreign_repo_label_or_exception_text_reaches_a_scoped_key(
            self, blocked_world, name):
        r_globex = blocked_world["r_globex"]
        for tool in (_check, _describe):
            with as_tenant(blocked_world["acme"]):
                out = tool(name)
            assert "Retirement pending" in out, out  # positive control: the row is shown
            for secret in (GLOBEX_REPO, f"repo id={r_globex}", "ServiceUnavailable",
                           "10.20.30.40", "neo4j_prod_admin"):
                assert secret not in out, f"{tool.__name__} leaked {secret!r}:\n{out}"

    def test_admin_key_sees_the_full_operator_reason(self, blocked_world):
        # GUARD: pre-existing behaviour (the reason was always rendered in full)
        reasons = blocked_world["reasons"]
        with as_tenant(None):
            undecidable = _check("acme_undecidable")
            error = _check("acme_error")
        assert _pending_line(undecidable).endswith(
            f"(blocked by {reasons['acme_undecidable']})"), undecidable
        assert f"{GLOBEX_REPO} (repo id={blocked_world['r_globex']})" in undecidable
        assert "ServiceUnavailable: Couldn't connect to 10.20.30.40:7687" in error, error


@pytest.fixture
def successor_world(hub, ledger, pg_conn):
    """acme retired two names; globex's PRIVATE module declares one of the old names.

    ``acme_old`` was recorded (by an orphan sweep before T2) with globex's
    ``globex_new`` as its old_technical_name successor; ``acme_old2``'s recorded
    successor ``acme_new2`` is acme's own module; ``acme_renamed`` was renamed
    in acme's repo history to ``globex_named_copy`` (git rename)."""
    acme = _tenant(pg_conn, "lr_acme")
    globex = _tenant(pg_conn, "lr_globex")
    acme_pid = _profile(pg_conn, "lr_acme_p", tenant_id=acme)
    globex_pid = _profile(pg_conn, "lr_globex_p", tenant_id=globex)
    r_acme = _repo(pg_conn, acme_pid, "acme_addons")
    _repo(pg_conn, globex_pid, GLOBEX_REPO)
    _module("globex_new", repo=GLOBEX_REPO, profiles=["lr_globex_p"],
            old_technical_name="acme_old")
    _module("acme_new2", repo="acme_addons", profiles=["lr_acme_p"],
            old_technical_name="acme_old2")
    at = datetime(2026, 9, 20, tzinfo=UTC)
    _observe(ledger, r_acme, "lr_acme_p", PRE_RENAME_HEAD,
             ["acme_old", "acme_old2", "acme_renamed"], at=at)
    _retire(ledger, r_acme, "acme_old", head=TVTMA_HEAD, reason="orphan_sweep",
            successors=["globex_new"], source="old_technical_name")
    _retire(ledger, r_acme, "acme_old2", head=TVTMA_HEAD, reason="orphan_sweep",
            successors=["acme_new2"], source="old_technical_name")
    _retire(ledger, r_acme, "acme_renamed", head=TVTMA_HEAD, sha=RENAME_SHA,
            when=RENAME_DATE, subject="[REF] acme_renamed: rename",
            successors=["globex_named_copy"], source="git_rename")
    return {"acme": acme, "globex": globex}


class TestRenamedToStaysInScope:
    """T2 (FIX) at render time: a recorded successor outside the caller's scope."""

    def test_other_tenants_module_is_never_named_as_the_successor(self, successor_world):
        with as_tenant(successor_world["acme"]):
            check = _check("acme_old")
            describe = _describe("acme_old")
        for out in (check, describe):
            _assert_adr0023_tree(out)
            assert "Lifecycle (acme_addons" in out, out  # positive control: acme's row shows
            assert "globex_new" not in out, out
            assert "Renamed to" not in out, out
        assert "describe_module(name='globex_new'" not in describe, describe

    def test_own_successor_is_still_named_and_described_next(self, successor_world):
        # GUARD: pre-existing behaviour (positive control of the scope cut)
        with as_tenant(successor_world["acme"]):
            check = _check("acme_old2")
            describe = _describe("acme_old2")
        assert "│   └─ Renamed to: acme_new2 (manifest old_technical_name)" in _lines(check), check
        assert _lines(describe)[-1] == (
            f"└─ Next: describe_module(name='acme_new2', odoo_version='{V}') "
            "for the successor"), describe

    def test_admin_sees_the_recorded_successor(self, successor_world):
        # GUARD: pre-existing behaviour (admin is unscoped)
        with as_tenant(None):
            out = _check("acme_old")
        assert "Renamed to: globex_new (manifest old_technical_name)" in out, out

    def test_a_git_rename_successor_comes_from_the_repos_own_history_and_is_shown(
            self, successor_world):
        # GUARD: pre-existing behaviour (contract: git_rename successors are not filtered)
        with as_tenant(successor_world["acme"]):
            out = _check("acme_renamed")
        assert "│   └─ Renamed to: globex_named_copy (git rename)" in _lines(out), out


def _state_line(out: str) -> str:
    [line] = [ln for ln in _block(out, "├─ Lifecycle (") if "State: " in ln]
    return line


def _balanced(text: str) -> bool:
    depth = 0
    for ch in text:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth < 0:
            return False
    return depth == 0


class TestRetiredStateNamesWhereItWasRecorded:
    """E2E-D2 (FIX): the recorded-at clause of a retired row, for every stamp shape."""

    def test_row_of_a_deleted_repo_without_a_head_reads_recorded_on_the_date(
            self, hub, ledger, std, pg_conn):
        """A row retired for a repo that no longer exists has no HEAD to record."""
        _observe(ledger, std["tvtma"], STD, PRE_RENAME_HEAD, ["viin_gone"],
                 at=datetime(2026, 9, 1, tzinfo=UTC))
        _retire(ledger, std["tvtma"], "viin_gone", head=None)
        day = _utc_day(_ledger_row(pg_conn, std["tvtma"], "viin_gone")["state_changed_at"])
        out = _check("viin_gone")
        line = _state_line(out)
        assert line == f"│   ├─ State: retired - removed from branch {V} (recorded on {day})", out
        assert "not stamped" not in out and _balanced(line), line

    def test_row_with_a_head_reads_recorded_at_that_head(self, hub, ledger, std, pg_conn):
        # GUARD: pre-existing behaviour (the HEAD + date shape was already right)
        _observe(ledger, std["tvtma"], STD, PRE_RENAME_HEAD, ["viin_gone"],
                 at=datetime(2026, 9, 1, tzinfo=UTC))
        _retire(ledger, std["tvtma"], "viin_gone", head=TVTMA_HEAD)
        day = _utc_day(_ledger_row(pg_conn, std["tvtma"], "viin_gone")["state_changed_at"])
        line = _state_line(_check("viin_gone"))
        assert line == (f"│   ├─ State: retired - removed from branch {V} "
                        f"(recorded at HEAD 281607a on {day})"), line
        assert _balanced(line)
