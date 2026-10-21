# SPDX-License-Identifier: AGPL-3.0-or-later
"""check_module_exists names merge and cross-version successors safely (#380 items 2-3) - no DB.

Rules protected (the read helpers with a fake hub; the DB-backed end-to-end
cases live in tests/test_mcp_module_successors.py):

* a ledger row with a ``git_merge`` successor renders ``Merged into: <module>
  (git merge)`` and the NO answer ends with a Next hint to it, like a rename;
* a ``git_merge`` successor is shown only when a module of that name is
  visible in the caller's scope (viin_ai moved to a private repo); hidden, the
  name is omitted - never the name, never a count. A ``git_rename`` successor
  is not gated (the retiring repo's own commit created it);
* the validated cross-version chain renders as one ``Renamed to`` / ``Merged
  into`` line (one hop: "(at 16.0, via OpenUpgrade apriori)"; several:
  "a -> b (16.0) -> c (17.0, merged)"), is a Next successor only when its last
  module is indexed at the asked version, and is not repeated when the ledger
  already names the same successor;
* no Neo4j query at all for a name the curated data has no edge from;
* the store never lets a ``git_merge`` successor abort a retirement on a
  schema without migration 0006: it is dropped with a WARNING.
"""
from __future__ import annotations

import pytest

from src.db.module_presence import ModulePresenceStore, Successor
from src.indexer import module_renames as mr
from src.mcp import lifecycle_read as lr


class FakeHub:
    def __init__(self, visible=(), presence=None):
        self.visible = set(visible)
        self.presence = presence or {}
        self.queries: list[tuple[str, dict]] = []

    def _scope_pred(self, alias):
        return f"{alias}.profile IS NOT NULL"

    def _data_bounded(self, session, query, label, **params):
        self.queries.append((label, params))
        names = params.get("names") or []
        if "collect(DISTINCT version)" in query:
            return [{"name": n, "versions": sorted(self.presence[n])}
                    for n in names if n in self.presence]
        return [{"name": n} for n in names if n in self.visible]


def _row(name="viin_ai_rag", *, successors=("viin_ai",), source="git_merge"):
    return {
        "relation": "self", "name": name, "repo_basename": "tvtmaaddons",
        "profile_name": "standard_viindoo_17", "repo_branch": "17.0",
        "odoo_version": "17.0", "state": "retired", "retire_reason": "orphan_sweep",
        "removing_commit_sha": "a231963d7a0000000000000000000000000000aa",
        "removing_commit_subject": "[MERGE] viin_ai_rag: merge into viin_ai",
        "successor_names": list(successors) if successors else None,
        "successor_source": source if successors else None,
    }


# ---------------------------------------------------------------------------
# Merged into (ledger)
# ---------------------------------------------------------------------------

def test_a_git_merge_successor_renders_merged_into():
    lines = lr._ledger_row_lines(_row())
    assert lines[-1] == "│   └─ Merged into: viin_ai (git merge)"
    assert not any("Renamed to" in ln for ln in lines)


def test_a_git_rename_successor_still_renders_renamed_to():
    lines = lr._ledger_row_lines(_row("test_pylint", successors=("test_viin_pylint",),
                                      source="git_rename"))
    assert lines[-1] == "│   └─ Renamed to: test_viin_pylint (git rename)"


def test_the_no_answer_points_next_at_the_merge_target():
    from src.mcp.tools.guidance import _format_check_module_exists

    lc = lr.ModuleLifecycle(name="viin_ai_rag", version="17.0", ledger_available=True,
                            ledger_rows=(_row(),))
    out = _format_check_module_exists(
        name="viin_ai_rag", version="17.0", indexed=False, edition=None, repo=None,
        is_ee_confusion=False, viindoo_equivalent=None, lifecycle=lc,
    )
    assert "│   └─ Merged into: viin_ai (git merge)" in out
    assert out.splitlines()[-1] == (
        "└─ Next: check_module_exists(name='viin_ai', odoo_version='17.0') for the successor")


def test_a_merge_target_visible_to_the_caller_is_kept():
    hub = FakeHub(visible={"viin_ai"})
    rows = lr._visible_successors(hub, None, [_row()], "17.0", {})
    assert rows[0]["successor_names"] == ["viin_ai"]
    assert rows[0]["successor_source"] == "git_merge"


def test_a_merge_target_outside_the_callers_scope_is_omitted_not_counted():
    hub = FakeHub(visible=set())
    rows = lr._visible_successors(hub, None, [_row()], "17.0", {})
    assert rows[0]["successor_names"] == []
    assert rows[0]["successor_source"] is None
    lc = lr.ModuleLifecycle(name="viin_ai_rag", version="17.0", ledger_available=True,
                            ledger_rows=tuple(rows))
    text = "\n".join(lr.lifecycle_lines(lc))
    assert "viin_ai (" not in text and "Merged into" not in text
    assert lc.successors == ()


def test_a_git_rename_successor_is_not_scope_gated():
    hub = FakeHub(visible=set())
    row = _row("test_pylint", successors=("test_viin_pylint",), source="git_rename")
    rows = lr._visible_successors(hub, None, [row], "17.0", {})
    assert rows[0]["successor_names"] == ["test_viin_pylint"]
    assert hub.queries == [], "nothing to gate, no query"


# ---------------------------------------------------------------------------
# Cross-version chain (OpenUpgrade apriori, validated)
# ---------------------------------------------------------------------------

def _lc(chain, **kw):
    return lr.ModuleLifecycle(name=chain.name if chain else "x", version=kw.pop("version", "16.0"),
                              ledger_available=True, cross_version=chain, **kw)


def _edge(old, new, version, kind=mr.KIND_RENAME):
    return mr.Edge(old, new, version, kind)


def test_one_validated_hop_renders_renamed_to_with_its_version_and_source():
    chain = mr.Chain("sale_coupon", "16.0", (_edge("sale_coupon", "sale_loyalty", "16.0"),), True)
    lines = lr.lifecycle_lines(_lc(chain))
    assert "├─ Renamed to:      sale_loyalty (at 16.0, via OpenUpgrade apriori)" in lines
    assert _lc(chain).successors == ("sale_loyalty",)


def test_a_mixed_rename_then_merge_chain_is_labelled_replaced_by():
    """The label covers every hop: a rename then a merge is neither a pure
    rename nor a pure merge."""
    chain = mr.Chain("website_sale_coupon_delivery", "17.0", (
        _edge("website_sale_coupon_delivery", "website_sale_loyalty_delivery", "16.0"),
        _edge("website_sale_loyalty_delivery", "website_sale_loyalty", "17.0", mr.KIND_MERGE),
    ), True)
    lines = lr.lifecycle_lines(_lc(chain, version="17.0"))
    assert (
        "├─ Replaced by:     website_sale_coupon_delivery -> website_sale_loyalty_delivery "
        "(16.0) -> website_sale_loyalty (17.0, merged) (via OpenUpgrade apriori)"
    ) in lines


def test_a_merge_then_rename_chain_is_not_labelled_renamed_to():
    chain = mr.Chain("a", "17.0", (
        _edge("a", "b", "16.0", mr.KIND_MERGE), _edge("b", "c", "17.0"),
    ), True)
    lines = lr.lifecycle_lines(_lc(chain, version="17.0"))
    assert ("├─ Replaced by:     a -> b (16.0, merged) -> c (17.0)"
            " (via OpenUpgrade apriori)") in lines


@pytest.mark.parametrize(("kind", "label"), [
    (mr.KIND_MERGE, "Merged into:     "), (mr.KIND_RENAME, "Renamed to:      "),
])
def test_a_chain_of_one_kind_keeps_its_label(kind, label):
    chain = mr.Chain("a", "17.0", (_edge("a", "b", "16.0", kind), _edge("b", "c", "17.0", kind)),
                     True)
    lines = lr.lifecycle_lines(_lc(chain, version="17.0"))
    assert any(ln.startswith(f"├─ {label}a -> b (16.0") for ln in lines), lines


def test_an_unresolved_chain_says_so_and_is_no_next_successor():
    chain = mr.Chain("coupon", "18.0", (_edge("coupon", "loyalty", "16.0"),), False)
    lc = _lc(chain, version="18.0")
    assert ("├─ Renamed to:      loyalty (at 16.0, via OpenUpgrade apriori); loyalty is not "
            "indexed at 18.0") in lr.lifecycle_lines(lc)
    assert lc.successors == ()


def test_a_chain_the_ledger_already_names_is_not_repeated():
    chain = mr.Chain("coupon", "16.0", (_edge("coupon", "loyalty", "16.0"),), True)
    row = _row("coupon", successors=("loyalty",), source="git_rename")
    lc = _lc(chain, ledger_rows=(row,))
    lines = lr.lifecycle_lines(lc)
    assert not any("via OpenUpgrade apriori" in ln for ln in lines)
    assert lc.successors == ("loyalty",)


def test_no_edge_from_the_name_means_no_query(monkeypatch):
    monkeypatch.setattr(mr, "rename_index", lambda *a, **k: mr.RenameIndex(()))
    hub = FakeHub()
    assert lr.read_cross_version_chain(hub, None, "sale", "17.0", {}, known_versions=[]) is None
    assert hub.queries == []


def test_the_chain_query_is_scoped_and_validates_against_it(monkeypatch):
    index = mr.RenameIndex([_edge("sale_coupon", "sale_loyalty", "16.0")])
    monkeypatch.setattr(mr, "rename_index", lambda *a, **k: index)
    scope = {"allowed": ["std_16"]}
    hub = FakeHub(presence={"sale_loyalty": {"16.0"}})
    chain = lr.read_cross_version_chain(hub, None, "sale_coupon", "16.0", scope,
                                        known_versions=["15.0", "14.0"])
    assert chain is not None and chain.final == "sale_loyalty" and chain.resolved
    (label, params), = hub.queries
    assert params["allowed"] == ["std_16"], "the caller's scope reaches the query"
    assert params["names"] == ["sale_loyalty"], "the asked name's versions were already known"
    hidden = FakeHub(presence={})
    assert lr.read_cross_version_chain(hidden, None, "sale_coupon", "16.0", scope,
                                       known_versions=["15.0"]) is None


# ---------------------------------------------------------------------------
# Store: no retirement aborts on a schema without migration 0006
# ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, definition):
        self.definition = definition

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return None if self.definition is None else (self.definition,)


class _Conn:
    def __init__(self, definition):
        self.definition = definition
        self.probes = 0

    def cursor(self, *a, **k):
        self.probes += 1
        return _Cur(self.definition)


MERGE = Successor(("viin_ai",), "git_merge")
OLD = "CHECK ((successor_source = ANY (ARRAY['git_rename'::text, 'old_technical_name'::text])))"
NEW = ("CHECK ((successor_source = ANY (ARRAY['git_rename'::text, 'git_merge'::text, "
       "'old_technical_name'::text])))")


def test_a_merge_successor_is_dropped_with_a_warning_before_migration_0006(caplog):
    store = ModulePresenceStore(None)  # type: ignore[arg-type]
    assert store._storable_successor(_Conn(OLD), "viin_ai_rag", MERGE) is None
    assert "migration 0006" in caplog.text


def test_a_merge_successor_is_kept_after_migration_0006_and_probed_once():
    store = ModulePresenceStore(None)  # type: ignore[arg-type]
    conn = _Conn(NEW)
    assert store._storable_successor(conn, "viin_ai_rag", MERGE) == MERGE
    assert store._storable_successor(conn, "viin_ai_agent", MERGE) == MERGE
    assert conn.probes == 1


@pytest.mark.parametrize("source", ["git_rename", "old_technical_name"])
def test_other_successors_are_never_probed(source):
    conn = _Conn(OLD)
    s = Successor(("x",), source)
    assert ModulePresenceStore(None)._storable_successor(conn, "y", s) == s  # type: ignore[arg-type]
    assert conn.probes == 0
