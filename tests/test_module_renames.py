# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-version module renames from OpenUpgrade apriori (#380 item 2) - no DB, no network.

Rules protected:

* the parser reads ``renamed_modules`` / ``merged_modules`` of every apriori
  era (dict, list of pairs, missing map) without executing the file, strips
  names, drops non-module names and self-maps, and refuses a non-literal
  instead of writing a gap (excerpts of the real files in
  ``tests/fixtures/openupgrade_apriori``);
* the committed ``spec_data/module_renames.json`` carries provenance for every
  entry (repo, branch, 40-hex commit, apriori path) - ADR-0055;
* an edge is shown only when the index confirms it: old indexed at N-1 and at
  no version from N to the asked one, new indexed at N. The name-reuse trap
  (``sale_coupon -> coupon`` at 14.0 while sale_coupon still ships at 14.0 and
  15.0) never validates; the real disappearance (``sale_coupon ->
  sale_loyalty`` at 16.0) does. Chains compose hop by hop with increasing
  versions; a successor the caller cannot see never validates;
* the generator keeps a branch's recorded commit when its content did not
  change (no weekly PR for an unrelated upstream commit), reports drift
  otherwise, and fails (exit 2) when the source cannot be read.
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
from datetime import date
from pathlib import Path

import pytest

from src.indexer import module_renames as mr
from src.indexer import module_renames_sync as sync

FIXTURES = Path(__file__).parent / "fixtures" / "openupgrade_apriori"


def _fixture(version: str) -> str:
    return (FIXTURES / f"apriori_{version}.py").read_text()


# ---------------------------------------------------------------------------
# Parser, per era
# ---------------------------------------------------------------------------

def test_8_0_era_has_renames_and_no_merged_map():
    got = mr.parse_apriori(_fixture("8.0"))
    assert got.renamed == {
        "account_banking_mt940": "account_bank_statement_import_mt940_base",
        "base_calendar": "calendar",
        "base_condition_template": "base_comment_template",
        "mrp_jit": "procurement_jit",
        "project_mrp": "sale_service",
    }
    assert got.merged == {}


def test_10_0_era_merged_modules_list_of_pairs_is_read_and_deduplicated():
    got = mr.parse_apriori(_fixture("10.0"))
    assert got.merged == {
        "account_full_reconcile": "account",
        "mail_tip": "mail",
        "project_timesheet": "hr_timesheet",
        "test_pylint": "test_lint",
    }
    assert got.renamed["connector"] == "queue_job"


def test_16_0_era_strips_names_and_drops_non_module_names():
    got = mr.parse_apriori(_fixture("16.0"))
    assert got.renamed == {
        "coupon": "loyalty",
        "sale_coupon": "sale_loyalty",
        "website_sale_coupon_delivery": "website_sale_loyalty_delivery",
    }
    assert got.merged == {"fetchmail": "mail", "gift_card": "loyalty",
                          "pos_gift_card": "pos_loyalty"}
    assert any("coupon.coupon" in d for d in got.dropped)


def test_parser_never_executes_the_file(tmp_path):
    marker = tmp_path / "executed"
    source = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "renamed_modules = {'a_old': 'a_new'}\n"
    )
    assert mr.parse_apriori(source).renamed == {"a_old": "a_new"}
    assert not marker.exists()


def test_a_self_map_is_dropped_and_a_hyphenated_oca_name_kept():
    got = mr.parse_apriori(
        "renamed_modules = {'same': 'same', 'account_e-invoice_generate': 'x_gen'}\n"
    )
    assert got.renamed == {"account_e-invoice_generate": "x_gen"}
    assert got.dropped


@pytest.mark.parametrize("source", [
    "renamed_modules = dict(a='b')\n",          # a call, not a literal
    "renamed_modules = ['a', 'b']\n",           # a list that is not of pairs
    "renamed_modules = {\n",                    # does not parse
])
def test_a_shape_the_parser_cannot_vouch_for_fails_loudly(source):
    with pytest.raises(ValueError):
        mr.parse_apriori(source)


# ---------------------------------------------------------------------------
# The committed data (ADR-0055 provenance)
# ---------------------------------------------------------------------------

def test_every_entry_of_the_committed_data_has_provenance():
    data = mr.load_data()
    branches = data["branches"]
    assert branches, "no branch block"
    for branch, block in branches.items():
        assert mr.branch_major(branch) is not None and mr.branch_major(branch) >= mr.MIN_MAJOR
        assert block["branch"] == branch
        assert block["repo"] == mr.DEFAULT_SOURCE_REPO
        assert re.fullmatch(r"[0-9a-f]{40}", block["commit"]), (branch, block["commit"])
        assert block["path"] in (*mr.APRIORI_PATHS, None), (branch, block["path"])
        if block["path"] is None:
            assert not block["renamed_modules"] and not block["merged_modules"]
        for key in ("renamed_modules", "merged_modules"):
            for old, new in block[key].items():
                assert mr._MODULE_NAME_RE.match(old) and mr._MODULE_NAME_RE.match(new)
                assert old != new
    assert len(mr.edges_from_data(data)) == sum(
        len(b["renamed_modules"]) + len(b["merged_modules"]) for b in branches.values()
    )


def test_the_committed_data_loads_into_the_read_index():
    index = mr.rename_index()
    assert len(index) == len(mr.edges_from_data(mr.load_data()))


# ---------------------------------------------------------------------------
# Read-time oracle (synthetic presence)
# ---------------------------------------------------------------------------

def _index(*edges):
    return mr.RenameIndex(mr.Edge(*e) for e in edges)


COUPON_EDGES = _index(
    ("sale_coupon", "coupon", "14.0", mr.KIND_RENAME),       # name-reuse trap
    ("sale_coupon", "sale_loyalty", "16.0", mr.KIND_RENAME),
    ("coupon", "loyalty", "16.0", mr.KIND_RENAME),
    ("website_sale_coupon_delivery", "website_sale_loyalty_delivery", "16.0",
     mr.KIND_RENAME),
    ("website_sale_loyalty_delivery", "website_sale_loyalty", "17.0", mr.KIND_MERGE),
)
PRESENCE = {
    "sale_coupon": {"13.0", "14.0", "15.0"},
    "coupon": {"14.0", "15.0"},
    "sale_loyalty": {"16.0", "17.0", "18.0"},
    "loyalty": {"16.0", "17.0", "18.0"},
    "website_sale_coupon_delivery": {"13.0", "14.0", "15.0"},
    "website_sale_loyalty_delivery": {"16.0"},
    "website_sale_loyalty": {"16.0", "17.0", "18.0"},
}


def _presence(table):
    calls = []

    def lookup(names):
        calls.append(list(names))
        return {n: table[n] for n in names if n in table}

    return lookup, calls


def _chain(name, version, index=COUPON_EDGES, table=PRESENCE):
    lookup, calls = _presence(table)
    return mr.resolve_chain(name, version, index, lookup), calls


def test_the_name_reuse_trap_never_validates_and_the_real_rename_does():
    chain, _ = _chain("sale_coupon", "16.0")
    assert chain is not None and chain.resolved
    assert [(h.old, h.new, h.version) for h in chain.hops] == [
        ("sale_coupon", "sale_loyalty", "16.0")]


def test_asked_before_the_rename_there_is_no_successor():
    """sale_coupon is absent at 12.0 because it did not exist yet."""
    chain, calls = _chain("sale_coupon", "12.0")
    assert chain is None
    assert calls == [], "no edge at or before 12.0: no index query"


def test_a_two_hop_chain_composes_with_increasing_versions():
    chain, calls = _chain("website_sale_coupon_delivery", "17.0")
    assert chain is not None and chain.resolved
    assert [(h.new, h.version, h.kind) for h in chain.hops] == [
        ("website_sale_loyalty_delivery", "16.0", mr.KIND_RENAME),
        ("website_sale_loyalty", "17.0", mr.KIND_MERGE),
    ]
    assert len(calls) == 2, "one presence query per hop"


def test_the_chain_stops_where_the_asked_version_is_indexed():
    chain, _ = _chain("website_sale_coupon_delivery", "16.0")
    assert chain is not None and chain.resolved and chain.final == "website_sale_loyalty_delivery"


def test_a_successor_outside_the_callers_scope_never_validates():
    """The presence callback is scoped: a private successor is simply not there."""
    hidden = {k: v for k, v in PRESENCE.items() if k != "sale_loyalty"}
    chain, _ = _chain("sale_coupon", "16.0", table=hidden)
    assert chain is None


def test_an_old_name_still_indexed_at_the_rename_version_is_not_renamed():
    table = {**PRESENCE, "coupon": {"14.0", "15.0", "16.0"}}
    chain, _ = _chain("coupon", "17.0", table=table)
    assert chain is None


def test_an_old_name_back_later_invalidates_the_earlier_disappearance():
    """coupon gone at 16.0 but indexed again at 17.0 (reuse): asked at 18.0,
    the 16.0 edge does not describe the 18.0 absence."""
    table = {**PRESENCE, "coupon": {"14.0", "15.0", "17.0"}}
    chain, _ = _chain("coupon", "18.0", table=table)
    assert chain is None


def test_old_name_must_be_indexed_right_before_the_rename():
    table = {**PRESENCE, "coupon": {"14.0"}}  # gone at 15.0 already
    chain, _ = _chain("coupon", "16.0", table=table)
    assert chain is None


def test_a_chain_whose_last_module_is_not_indexed_at_the_asked_version_is_unresolved():
    table = {**PRESENCE, "website_sale_loyalty": {"17.0"}}
    chain, _ = _chain("website_sale_coupon_delivery", "18.0", table=table)
    assert chain is not None and not chain.resolved
    assert chain.final == "website_sale_loyalty"


def test_a_rename_is_preferred_over_a_merge_of_the_same_version():
    index = _index(("a_old", "b_merge", "16.0", mr.KIND_MERGE),
                   ("a_old", "c_new", "16.0", mr.KIND_RENAME))
    table = {"a_old": {"15.0"}, "b_merge": {"16.0"}, "c_new": {"16.0"}}
    chain, _ = _chain("a_old", "16.0", index=index, table=table)
    assert chain is not None and chain.final == "c_new"


def test_a_cycle_ends():
    index = _index(("x_a", "x_b", "15.0", mr.KIND_RENAME), ("x_b", "x_a", "16.0", mr.KIND_RENAME))
    table = {"x_a": {"14.0", "16.0"}, "x_b": {"15.0"}}
    chain, _ = _chain("x_a", "17.0", index=index, table=table)
    assert chain is None or len(chain.hops) <= mr.MAX_HOPS


def test_missing_data_file_degrades_to_no_edges(tmp_path, caplog):
    index = mr.rename_index(tmp_path / "absent.json")
    assert len(index) == 0
    assert "unavailable" in caplog.text


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

SHA_A = "a" * 40
SHA_B = "b" * 40


def _heads(**branches):
    return lambda repo: dict(branches)


def _fetcher(files):
    def fetch(repo, sha, path):
        return files.get((sha, path))
    return fetch


def _script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "sync_module_renames.py"
    spec = importlib.util.spec_from_file_location("sync_module_renames", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(tmp_path, *, mode, heads, fetch, spec=None):
    spec = spec or (tmp_path / "module_renames.json")
    out = io.StringIO()
    code = _script().run(
        mode=mode, spec_path=spec, heads=heads, fetch=fetch, today=date(2026, 10, 20), out=out,
    )
    return code, spec, out.getvalue()


V16 = "openupgrade_scripts/apriori.py"


def test_write_then_check_is_in_sync(tmp_path):
    files = {(SHA_A, V16): _fixture("16.0")}
    code, spec, _ = _run(tmp_path, mode="write", heads=_heads(**{"16.0": SHA_A}),
                         fetch=_fetcher(files))
    assert code == 0
    block = json.loads(spec.read_text())["branches"]["16.0"]
    assert (block["commit"], block["path"], block["repo"]) == (SHA_A, V16, "OCA/OpenUpgrade")
    assert block["renamed_modules"]["sale_coupon"] == "sale_loyalty"
    code, _, _ = _run(tmp_path, mode="check", heads=_heads(**{"16.0": SHA_A}),
                      fetch=_fetcher(files), spec=spec)
    assert code == 0


def test_an_upstream_commit_that_does_not_change_the_maps_is_no_drift(tmp_path):
    files = {(SHA_A, V16): _fixture("16.0"), (SHA_B, V16): _fixture("16.0") + "\n# touched\n"}
    _, spec, _ = _run(tmp_path, mode="write", heads=_heads(**{"16.0": SHA_A}),
                      fetch=_fetcher(files))
    code, _, _ = _run(tmp_path, mode="check", heads=_heads(**{"16.0": SHA_B}),
                      fetch=_fetcher(files), spec=spec)
    assert code == 0
    _run(tmp_path, mode="write", heads=_heads(**{"16.0": SHA_B}), fetch=_fetcher(files), spec=spec)
    assert json.loads(spec.read_text())["branches"]["16.0"]["commit"] == SHA_A


def test_a_changed_map_is_drift_and_write_records_the_new_commit(tmp_path):
    files = {(SHA_A, V16): _fixture("16.0"),
             (SHA_B, V16): _fixture("16.0").replace('"coupon": "loyalty"', '"coupon": "loyalty2"')}
    _, spec, _ = _run(tmp_path, mode="write", heads=_heads(**{"16.0": SHA_A}),
                      fetch=_fetcher(files))
    code, _, out = _run(tmp_path, mode="check", heads=_heads(**{"16.0": SHA_B}),
                        fetch=_fetcher(files), spec=spec)
    assert code == 1 and "DRIFT" in out
    _run(tmp_path, mode="write", heads=_heads(**{"16.0": SHA_B}), fetch=_fetcher(files), spec=spec)
    block = json.loads(spec.read_text())["branches"]["16.0"]
    assert block["commit"] == SHA_B and block["renamed_modules"]["coupon"] == "loyalty2"


def test_the_first_apriori_path_the_commit_has_is_used_per_era(tmp_path):
    files = {(SHA_A, "odoo/addons/openupgrade_records/lib/apriori.py"): _fixture("10.0"),
             (SHA_B, "openerp/addons/openupgrade_records/lib/apriori.py"): _fixture("8.0")}
    code, spec, _ = _run(tmp_path, mode="write",
                         heads=_heads(**{"10.0": SHA_A, "8.0": SHA_B, "20.0": "c" * 40}),
                         fetch=_fetcher(files))
    assert code == 0
    branches = json.loads(spec.read_text())["branches"]
    assert list(branches) == ["8.0", "10.0", "20.0"], "numeric branch order"
    assert branches["10.0"]["path"] == "odoo/addons/openupgrade_records/lib/apriori.py"
    assert branches["20.0"]["path"] is None and branches["20.0"]["renamed_modules"] == {}


def test_an_unreadable_source_is_exit_2(tmp_path):
    def broken(repo):
        raise RuntimeError("ls-remote failed")

    code, spec, _ = _run(tmp_path, mode="check", heads=broken, fetch=_fetcher({}))
    assert code == 2 and not spec.exists()


def test_an_apriori_that_does_not_parse_is_exit_2(tmp_path):
    files = {(SHA_A, V16): "renamed_modules = {\n"}
    code, _, _ = _run(tmp_path, mode="check", heads=_heads(**{"16.0": SHA_A}),
                      fetch=_fetcher(files))
    assert code == 2


def test_no_branch_with_an_apriori_is_exit_2(tmp_path):
    code, _, _ = _run(tmp_path, mode="write", heads=_heads(**{"16.0": SHA_A}),
                      fetch=_fetcher({}))
    assert code == 2


def test_remote_branch_heads_keeps_only_indexed_n_0_branches():
    class R:
        returncode = 0
        stderr = ""
        stdout = "\n".join([
            f"{SHA_A}\trefs/heads/7.0", f"{SHA_A}\trefs/heads/8.0",
            f"{SHA_B}\trefs/heads/18.0-update-analysis-bot", f"{SHA_B}\trefs/heads/19.0",
            f"{SHA_B}\trefs/heads/documentation",
        ])

    assert sync.remote_branch_heads("OCA/OpenUpgrade", runner=lambda *a, **k: R()) == {
        "8.0": SHA_A, "19.0": SHA_B,
    }


def test_version_and_branch_shapes_refuse_a_trailing_newline():
    """`$` matches before a final newline; upstream data must match exactly."""
    assert mr.version_key("16.0") == (16, 0)
    assert mr.version_key("16.0\n") is None
    assert mr.branch_major("16.0") == 16
    assert mr.branch_major("16.0\n") is None
