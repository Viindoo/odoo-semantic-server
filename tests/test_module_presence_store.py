# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_module_presence_store.py
"""ModulePresenceStore - the two-phase module lifecycle ledger (ADR-0056, OSM #378).

Business rules protected here (expected values come from the rules and the
real cases, never from what the implementation happens to return):

- C1  observing an absence never retires; only commit_retired (called after
      the graph/embedding delete succeeded) does. A pending retirement
      survives later runs, other connections and a crash between the phases.
- L5  retired -> present resurrection clears the removing-commit and successor
      evidence and counts the resurrection. Real case: payment_ogone <->
      payment_ingenico is a non-monotonic round-trip (present v8-v12, gone
      v13-v14, back v15-v17, gone again v18).
- M2  the posbox point_of_sale stub beside the real point_of_sale (odoo12-18)
      is ONE row carrying the stub in shadowed_paths; duplicate names in one
      scan are rejected before anything is written.
- H3  mark_repo_removed + repo delete keeps the rows as pending(repo_removed)
      history with repo_id NULL, addressable by row id.
- H5a other_present_owners / potential_owners_unsynced: a never-synced repo
      at the version makes every name undecidable; a synced repo never blocks.
- M5  needs_rewrite only on present rows, survives while present, consumed once.
- H4  lifecycle_attention set/clear on repos.
- L7  rename_profile keeps every row (history included) readable under the new name.
- R15 lifecycle_rows is fail-closed per allowed profiles (also as osm_reader
      under RLS) and returns the "renamed from" predecessor rows. Real case:
      tvtmaaddons renamed test_pylint -> test_viin_pylint (0240c6b77f).
- H2b per-version lock: two sessions never hold retire:<version> at once; a
      waiter gives up with LifecycleLockTimeout after its budget.
"""
from __future__ import annotations

import threading
import time

import psycopg2
import psycopg2.extras
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

V = "99.0"
V_OTHER = "98.0"
MF = "__manifest__.py"

POS = "point_of_sale"
POS_PATH = "addons/point_of_sale"
POSBOX_STUB = (
    "addons/point_of_sale/tools/posbox/overwrite_after_init/home/pi/odoo/addons/point_of_sale"
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


@pytest.fixture
def mp():
    """The store module (imported lazily so the file always collects)."""
    import src.db.module_presence as module

    return module


@pytest.fixture
def store(db, mp):
    from src.db.pg import get_pool

    return mp.ModulePresenceStore(get_pool(), lock_wait_seconds=1.0)


@pytest.fixture
def raw_conns(_ephemeral_pg_db):
    """Factory of dedicated autocommit connections (closed at teardown)."""
    opened = []

    def _make():
        c = psycopg2.connect(_ephemeral_pg_db)
        c.autocommit = True
        opened.append(c)
        return c

    yield _make
    for c in opened:
        try:
            c.close()
        except Exception:
            pass


def _q(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def _profile(conn, name, version=V):
    return _q(conn, "INSERT INTO profiles (name, odoo_version) VALUES (%s, %s) RETURNING id",
              (name, version))[0]["id"]


def _repo(conn, profile_id, basename, *, url=None, branch=V, head_sha=None):
    return _q(
        conn,
        "INSERT INTO repos (profile_id, url, branch, local_path, head_sha) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (profile_id, url or f"git@github.com:Viindoo/{basename}.git", branch,
         f"/srv/repos/{branch}/{basename}", head_sha),
    )[0]["id"]


def _row(conn, repo_id, name):
    rows = _q(conn, "SELECT * FROM module_presence WHERE repo_id = %s AND name = %s",
              (repo_id, name))
    assert len(rows) <= 1
    return rows[0] if rows else None


def _snapshot(conn):
    return (
        _q(conn, "SELECT * FROM module_presence ORDER BY id"),
        _q(conn, "SELECT * FROM repos ORDER BY id"),
    )


def _om(mp, name, path=None, **kw):
    return mp.ObservedModule(name=name, path=path or f"addons/{name}", manifest_file=MF, **kw)


def _observe(store, mp, repo_id, head, modules, *, profile="mp_p", version=V):
    obs = [m if isinstance(m, mp.ObservedModule) else _om(mp, m) for m in modules]
    return store.commit_observed(repo_id, profile_name=profile, odoo_version=version,
                                 head_sha=head, observed=obs)


@pytest.fixture
def repo(db):
    """One profile 'mp_p' at V with one repo 'tvtmaaddons' (head_sha h1)."""
    pid = _profile(db, "mp_p")
    return _repo(db, pid, "tvtmaaddons", head_sha="h1")


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

class TestObservedModuleValidation:
    def test_present_module_cannot_carry_an_exclusion_reason(self, mp):
        with pytest.raises(ValueError):
            mp.ObservedModule(name="sale", path="addons/sale", manifest_file=MF,
                              exclusion_reason="license_skip")

    def test_excluded_module_needs_a_known_exclusion_reason(self, mp):
        with pytest.raises(ValueError):
            mp.ObservedModule(name="sale", path="addons/sale", manifest_file=MF,
                              state="excluded")
        with pytest.raises(ValueError):
            mp.ObservedModule(name="sale", path="addons/sale", manifest_file=MF,
                              state="excluded", exclusion_reason="deprecated")

    def test_observed_state_is_only_present_or_excluded(self, mp):
        """A scan observes manifests; 'retired' is never an observation."""
        with pytest.raises(ValueError):
            mp.ObservedModule(name="sale", path="addons/sale", manifest_file=MF,
                              state="retired")

    def test_successor_needs_a_name_and_a_known_source(self, mp):
        with pytest.raises(ValueError):
            mp.Successor(names=(), source="git_rename")
        with pytest.raises(ValueError):
            mp.Successor(names=("test_viin_pylint",), source="similar_name")


# ---------------------------------------------------------------------------
# Rule 1 - diff is read-only and classifies the scan
# ---------------------------------------------------------------------------

class TestDiff:
    def _seed(self, store, mp, repo):
        _observe(store, mp, repo, "h1", [
            "sale_a", "sale_b", "sale_d", "sale_f", "sale_i",
            _om(mp, "sale_h", "addons/sale_h"),
            _om(mp, "sale_c", state="excluded", exclusion_reason="installable_false"),
            _om(mp, "sale_g", state="excluded", exclusion_reason="license_skip"),
        ])
        store.mark_retire_pending(repo, ["sale_d"])
        assert store.commit_retired(repo, "sale_d", head_sha="h1")
        store.mark_retire_pending(repo, ["sale_b", "sale_i"])

    def _scan(self, mp):
        return [
            _om(mp, "sale_a"),
            _om(mp, "sale_c"),                                          # excluded -> present
            _om(mp, "sale_d"),                                          # retired -> present
            _om(mp, "sale_new"),                                        # no row
            _om(mp, "sale_f", state="excluded", exclusion_reason="unparseable"),
            _om(mp, "sale_g", state="excluded", exclusion_reason="unparseable"),
            _om(mp, "sale_h", "legacy/sale_h"),                         # moved dir
            _om(mp, "sale_i"),                                          # pending, back
        ]                                                               # sale_b absent

    def test_diff_writes_nothing(self, store, mp, repo, db):
        self._seed(store, mp, repo)
        before = _snapshot(db)
        store.diff(repo, self._scan(mp))
        store.diff(repo, [])
        assert _snapshot(db) == before

    def test_diff_classifies_every_observed_and_every_missing_name(self, store, mp, repo):
        self._seed(store, mp, repo)
        d = store.diff(repo, self._scan(mp))

        assert set(d.added) == {"sale_new"}
        assert set(d.resurrected) == {"sale_d"}
        assert set(d.became_present) == {"sale_c"}
        assert dict(d.became_excluded) == {"sale_f": "unparseable"}
        assert dict(d.reason_changed) == {"sale_g": ("license_skip", "unparseable")}
        assert set(d.unchanged) == {"sale_a", "sale_h", "sale_i"}
        assert dict(d.moved) == {"sale_h": ("addons/sale_h", "legacy/sale_h")}
        assert dict(d.absent) == {"sale_b": "present"}
        assert set(d.already_pending) == {"sale_b"}
        assert set(d.pending_cleared) == {"sale_i"}
        # present before: a, b, f, h, i (c, g excluded; d retired)
        assert d.n_present_before == 5
        assert d.absent_from_present == ("sale_b",)
        assert d.unparseable_from_present == ("sale_f",)
        assert d.never_synced is False

    def test_absent_covers_excluded_rows_too_but_never_retired_rows(self, store, mp, repo):
        _observe(store, mp, repo, "h1", [
            "sale_a", _om(mp, "sale_c", state="excluded", exclusion_reason="license_skip"),
            "sale_old"])
        store.commit_retired(repo, "sale_old", head_sha="h1")
        d = store.diff(repo, [])
        assert dict(d.absent) == {"sale_a": "present", "sale_c": "excluded"}
        assert d.absent_from_present == ("sale_a",)

    def test_repo_with_no_presence_head_and_no_rows_is_never_synced(self, store, mp, repo):
        d = store.diff(repo, [_om(mp, "sale_a")])
        assert d.never_synced is True
        assert d.presence_head_sha is None
        assert d.n_present_before == 0
        assert set(d.added) == {"sale_a"}

    def test_repo_that_has_rows_or_a_presence_head_is_not_never_synced(self, store, mp, db):
        pid = _profile(db, "mp_ns")
        r_rows = _repo(db, pid, "with_rows")
        r_head = _repo(db, pid, "with_head")
        _observe(store, mp, r_rows, "h1", ["sale_a"], profile="mp_ns")
        store.mark_presence_synced(r_head, "h1")
        assert store.diff(r_rows, []).never_synced is False
        d = store.diff(r_head, [])
        assert d.never_synced is False
        assert d.presence_head_sha == "h1"

    def test_diff_of_unknown_repo_raises_repo_not_found(self, store, mp):
        from src.db.exceptions import RepoNotFoundError

        with pytest.raises(RepoNotFoundError):
            store.diff(987654, [_om(mp, "sale")])


# ---------------------------------------------------------------------------
# commit_observed - upsert of observed rows only
# ---------------------------------------------------------------------------

class TestCommitObserved:
    def test_first_observation_records_repo_identity_and_first_seen(self, store, mp, repo, db):
        res = _observe(store, mp, repo, "h1", [_om(mp, "viin_ai_rag", version_raw="17.0.1.0",
                                                   version_mismatch=False)])
        assert res.inserted == ("viin_ai_rag",)
        r = _row(db, repo, "viin_ai_rag")
        assert r["state"] == "present"
        assert r["exclusion_reason"] is None
        assert (r["repo_url"], r["repo_basename"], r["repo_branch"]) == (
            "git@github.com:Viindoo/tvtmaaddons.git", "tvtmaaddons", V)
        assert (r["profile_name"], r["odoo_version"]) == ("mp_p", V)
        assert (r["path"], r["manifest_file"], r["version_raw"]) == (
            "addons/viin_ai_rag", MF, "17.0.1.0")
        assert r["first_seen_sha"] == r["last_seen_sha"] == "h1"
        assert r["state_changed_sha"] == "h1"
        assert r["resurrection_count"] == 0
        assert r["retire_pending"] is False

    def test_reobservation_moves_last_seen_but_keeps_first_seen_and_state_change(
            self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["viin_ai_rag"])
        res = _observe(store, mp, repo, "h2", [_om(mp, "viin_ai_rag", "moved/viin_ai_rag")])
        assert res.inserted == ()
        assert res.state_changed == ()
        r = _row(db, repo, "viin_ai_rag")
        assert (r["first_seen_sha"], r["last_seen_sha"], r["state_changed_sha"]) == (
            "h1", "h2", "h1")
        assert r["path"] == "moved/viin_ai_rag"

    def test_present_excluded_transitions_and_reason_changes_move_state_change(
            self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["hw_screen"])
        res = _observe(store, mp, repo, "h2", [
            _om(mp, "hw_screen", state="excluded", exclusion_reason="installable_false")])
        assert res.state_changed == ("hw_screen",)
        r = _row(db, repo, "hw_screen")
        assert (r["state"], r["exclusion_reason"], r["state_changed_sha"]) == (
            "excluded", "installable_false", "h2")

        _observe(store, mp, repo, "h3", [
            _om(mp, "hw_screen", state="excluded", exclusion_reason="license_skip")])
        r = _row(db, repo, "hw_screen")
        assert (r["state"], r["exclusion_reason"], r["state_changed_sha"]) == (
            "excluded", "license_skip", "h3")

        _observe(store, mp, repo, "h4", [
            _om(mp, "hw_screen", state="excluded", exclusion_reason="unparseable")])
        assert _row(db, repo, "hw_screen")["exclusion_reason"] == "unparseable"

        _observe(store, mp, repo, "h5", ["hw_screen"])
        r = _row(db, repo, "hw_screen")
        assert (r["state"], r["exclusion_reason"], r["state_changed_sha"]) == (
            "present", None, "h5")
        assert r["first_seen_sha"] == "h1"

    def test_unobserved_rows_are_left_untouched(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["sale_a", "sale_b"])
        before = _row(db, repo, "sale_b")
        _observe(store, mp, repo, "h2", ["sale_a"])
        assert _row(db, repo, "sale_b") == before

    def test_empty_observation_is_a_noop(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["sale_a"])
        before = _snapshot(db)
        res = _observe(store, mp, repo, "h2", [])
        assert (res.inserted, res.resurrected, res.state_changed, res.pending_cleared) == (
            (), (), (), ())
        assert _snapshot(db) == before


# ---------------------------------------------------------------------------
# Rule 2 + 3 - two-phase retirement (C1, H1)
# ---------------------------------------------------------------------------

class TestTwoPhaseRetirement:
    def test_absence_alone_never_retires(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["test_pylint", "sale_a"])
        _observe(store, mp, repo, "h2", ["sale_a"])
        r = _row(db, repo, "test_pylint")
        assert r["state"] == "present"
        assert r["retire_pending"] is False
        assert r["last_seen_sha"] == "h1"

    def test_pending_is_not_retired_and_only_commit_retired_retires(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["test_pylint"])
        ev = mp.RetireEvidence(sha="0240c6b77f", date="2026-09-11T10:00:00+07:00",
                               subject="[REN] test_pylint -> test_viin_pylint")
        flagged = store.mark_retire_pending(repo, ["test_pylint"], evidence={"test_pylint": ev})
        assert flagged == ["test_pylint"]
        r = _row(db, repo, "test_pylint")
        assert (r["state"], r["retire_pending"], r["retire_pending_reason"]) == (
            "present", True, "absent")
        assert r["retire_pending_at"] is not None
        assert r["removing_commit_sha"] == "0240c6b77f"
        assert r["removing_commit_subject"] == "[REN] test_pylint -> test_viin_pylint"
        assert r["removing_commit_date"] is not None

        assert store.commit_retired(repo, "test_pylint", head_sha="h2") is True
        r = _row(db, repo, "test_pylint")
        assert (r["state"], r["retire_reason"]) == ("retired", "absent")
        assert (r["retire_pending"], r["retire_pending_reason"]) == (False, None)
        assert r["state_changed_sha"] == "h2"
        # evidence recorded at the pending phase is kept on the retired row
        assert r["removing_commit_sha"] == "0240c6b77f"

    def test_retired_history_is_never_overwritten(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["test_pylint"])
        store.mark_retire_pending(repo, ["test_pylint"], "orphan_sweep")
        assert store.commit_retired(repo, "test_pylint", head_sha="h2")
        before = _row(db, repo, "test_pylint")
        assert store.commit_retired(repo, "test_pylint", reason="repo_removed",
                                    head_sha="h9") is False
        assert _row(db, repo, "test_pylint") == before

    def test_commit_retired_defaults_to_the_pending_reason(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["orphan_mod"])
        store.mark_retire_pending(repo, ["orphan_mod"], "orphan_sweep")
        store.commit_retired(repo, "orphan_mod")
        assert _row(db, repo, "orphan_mod")["retire_reason"] == "orphan_sweep"

    def test_commit_retired_without_pending_uses_absent(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["sale_a"])
        assert store.commit_retired(repo, "sale_a") is True
        assert _row(db, repo, "sale_a")["retire_reason"] == "absent"

    def test_commit_retired_on_unknown_name_is_false(self, store, mp, repo):
        assert store.commit_retired(repo, "never_existed") is False

    def test_explicit_retire_evidence_wins_over_pending_evidence(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["sale_a"])
        store.mark_retire_pending(repo, ["sale_a"],
                                  evidence={"sale_a": mp.RetireEvidence(sha="old")})
        store.commit_retired(repo, "sale_a", evidence=mp.RetireEvidence(sha="new"))
        assert _row(db, repo, "sale_a")["removing_commit_sha"] == "new"

    def test_retiring_an_excluded_row_clears_its_exclusion_and_rewrite_flag(
            self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", [
            _om(mp, "hw_scale", state="excluded", exclusion_reason="installable_false")])
        store.commit_retired(repo, "hw_scale")
        r = _row(db, repo, "hw_scale")
        assert (r["state"], r["exclusion_reason"], r["needs_rewrite"]) == (
            "retired", None, False)

    def test_mark_pending_ignores_unknown_and_retired_names(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["sale_a", "sale_gone"])
        store.commit_retired(repo, "sale_gone")
        assert store.mark_retire_pending(repo, ["sale_gone", "no_such"]) == []
        assert _row(db, repo, "sale_gone")["retire_pending"] is False

    def test_reflagging_keeps_first_flag_time_but_updates_reason_and_blocker(
            self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["sale_a"])
        store.mark_retire_pending(repo, ["sale_a"], evidence={
            "sale_a": mp.RetireEvidence(sha="dead", subject="[REM] sale_a")})
        first_at = _row(db, repo, "sale_a")["retire_pending_at"]
        time.sleep(0.05)
        store.mark_retire_pending(repo, ["sale_a"], "orphan_sweep", blocked_by="G-A")
        r = _row(db, repo, "sale_a")
        assert r["retire_pending_at"] == first_at
        assert (r["retire_pending_reason"], r["retire_blocked_by"]) == ("orphan_sweep", "G-A")
        # a re-flag without evidence keeps the stored evidence
        assert (r["removing_commit_sha"], r["removing_commit_subject"]) == (
            "dead", "[REM] sale_a")

    def test_pending_survives_a_later_scan_that_still_lacks_the_module(
            self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["sale_a", "sale_b"])
        store.mark_retire_pending(repo, ["sale_b"], blocked_by="G-A")
        _observe(store, mp, repo, "h2", ["sale_a"])
        r = _row(db, repo, "sale_b")
        assert (r["state"], r["retire_pending"]) == ("present", True)

    def test_reobserving_a_pending_module_clears_pending_and_its_evidence(
            self, store, mp, repo, db):
        """G-A trip then fix: the manifest was only missing from a degraded checkout."""
        _observe(store, mp, repo, "h1", ["sale_a", "sale_b"])
        store.mark_retire_pending(
            repo, ["sale_b"], blocked_by="G-A",
            evidence={"sale_b": mp.RetireEvidence(sha="dead", subject="x")},
            successors={"sale_b": mp.Successor(names=("sale_b2",), source="git_rename")},
        )
        res = _observe(store, mp, repo, "h2", ["sale_a", "sale_b"])
        assert res.pending_cleared == ("sale_b",)
        r = _row(db, repo, "sale_b")
        assert r["state"] == "present"
        assert (r["retire_pending"], r["retire_pending_reason"], r["retire_pending_at"],
                r["retire_blocked_by"]) == (False, None, None, None)
        assert (r["removing_commit_sha"], r["removing_commit_date"],
                r["removing_commit_subject"]) == (None, None, None)
        assert (r["successor_names"], r["successor_source"]) == (None, None)

    def test_pending_retirement_survives_a_crash_and_is_reoffered(self, store, mp, repo, db):
        """Crash between phase 1 (flag) and phase 2 (delete + commit_retired):
        a fresh store on a fresh connection still sees the row pending, not retired."""
        from src.db.pg import get_pool

        _observe(store, mp, repo, "h1", ["viin_ai_pulse_memory", "sale_a"])
        store.mark_retire_pending(repo, ["viin_ai_pulse_memory"])
        del store  # the run dies here

        again = mp.ModulePresenceStore(get_pool(), lock_wait_seconds=1.0)
        assert _row(db, repo, "viin_ai_pulse_memory")["state"] == "present"
        offered = [(r["repo_id"], r["name"]) for r in again.pending_retirements(V)]
        assert offered == [(repo, "viin_ai_pulse_memory")]
        d = again.diff(repo, [_om(mp, "sale_a")])
        assert set(d.already_pending) == {"viin_ai_pulse_memory"}
        assert again.commit_retired(repo, "viin_ai_pulse_memory") is True
        assert again.pending_retirements(V) == []

    def test_pending_retirements_are_scoped_to_the_version(self, store, mp, db):
        p99 = _profile(db, "mp_v99")
        p98 = _profile(db, "mp_v98", V_OTHER)
        r99 = _repo(db, p99, "a")
        r98 = _repo(db, p98, "b", branch=V_OTHER)
        _observe(store, mp, r99, "h1", ["sale_x"], profile="mp_v99")
        _observe(store, mp, r98, "h1", ["sale_x"], profile="mp_v98", version=V_OTHER)
        store.mark_retire_pending(r99, ["sale_x"])
        store.mark_retire_pending(r98, ["sale_x"])
        assert [r["repo_id"] for r in store.pending_retirements(V)] == [r99]
        assert [r["repo_id"] for r in store.pending_retirements(V_OTHER)] == [r98]

    def test_mark_retire_blocked_annotates_only_pending_rows(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["sale_a", "sale_b"])
        store.mark_retire_pending(repo, ["sale_a"])
        ids = [_row(db, repo, n)["id"] for n in ("sale_a", "sale_b")]
        assert store.mark_retire_blocked(ids, "G-B: 40% of present modules absent") == 1
        assert _row(db, repo, "sale_a")["retire_blocked_by"] == (
            "G-B: 40% of present modules absent")
        assert _row(db, repo, "sale_b")["retire_blocked_by"] is None


# ---------------------------------------------------------------------------
# Rule 4 - resurrection (L5): payment_ogone <-> payment_ingenico round trip
# ---------------------------------------------------------------------------

class TestResurrection:
    def test_round_trip_rename_resurrects_with_clean_evidence(self, store, mp, repo, db):
        _observe(store, mp, repo, "v12", ["payment_ogone"])
        # v13: renamed to payment_ingenico
        _observe(store, mp, repo, "v13", ["payment_ingenico"])
        store.mark_retire_pending(
            repo, ["payment_ogone"],
            evidence={"payment_ogone": mp.RetireEvidence(sha="c0ffee", subject="[REN] ogone")},
            successors={"payment_ogone": mp.Successor(names=("payment_ingenico",),
                                                      source="git_rename")},
        )
        assert store.commit_retired(repo, "payment_ogone", head_sha="v13")
        r = _row(db, repo, "payment_ogone")
        assert (r["state"], r["successor_names"], r["successor_source"]) == (
            "retired", ["payment_ingenico"], "git_rename")

        # v15: renamed back
        res = _observe(store, mp, repo, "v15", ["payment_ogone"])
        assert res.resurrected == ("payment_ogone",)
        r = _row(db, repo, "payment_ogone")
        assert r["state"] == "present"
        assert r["resurrection_count"] == 1
        assert r["retire_reason"] is None
        assert (r["successor_names"], r["successor_source"]) == (None, None)
        assert (r["removing_commit_sha"], r["removing_commit_date"],
                r["removing_commit_subject"]) == (None, None, None)
        assert r["first_seen_sha"] == "v12"
        assert (r["last_seen_sha"], r["state_changed_sha"]) == ("v15", "v15")

    def test_second_disappearance_and_return_counts_twice(self, store, mp, repo, db):
        """payment_ogone is non-monotonic: gone at v13, back at v15, gone at v18."""
        _observe(store, mp, repo, "v12", ["payment_ogone"])
        store.commit_retired(repo, "payment_ogone", head_sha="v13")
        _observe(store, mp, repo, "v15", ["payment_ogone"])
        store.commit_retired(repo, "payment_ogone", head_sha="v18")
        assert _row(db, repo, "payment_ogone")["resurrection_count"] == 1
        _observe(store, mp, repo, "v19", ["payment_ogone"])
        r = _row(db, repo, "payment_ogone")
        assert (r["state"], r["resurrection_count"]) == ("present", 2)

    def test_resurrection_into_excluded_counts_too(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["hw_scanner"])
        store.commit_retired(repo, "hw_scanner")
        _observe(store, mp, repo, "h2", [
            _om(mp, "hw_scanner", state="excluded", exclusion_reason="installable_false")])
        r = _row(db, repo, "hw_scanner")
        assert (r["state"], r["exclusion_reason"], r["resurrection_count"],
                r["retire_reason"]) == ("excluded", "installable_false", 1, None)


# ---------------------------------------------------------------------------
# Rule 5 - posbox (M2)
# ---------------------------------------------------------------------------

class TestSameNameInOneRepo:
    def test_posbox_stub_lands_as_one_row_with_shadowed_path(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", [
            _om(mp, POS, POS_PATH, shadowed_paths=(POSBOX_STUB,))])
        rows = _q(db, "SELECT path, shadowed_paths FROM module_presence WHERE name = %s", (POS,))
        assert rows == [{"path": POS_PATH, "shadowed_paths": [POSBOX_STUB]}]

    def test_shadowed_paths_follow_the_latest_scan(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", [
            _om(mp, POS, POS_PATH, shadowed_paths=(POSBOX_STUB,))])
        _observe(store, mp, repo, "h2", [_om(mp, POS, POS_PATH)])
        assert _row(db, repo, POS)["shadowed_paths"] == []

    def test_two_observations_with_the_same_name_are_rejected_before_writing(
            self, store, mp, repo, db):
        dup = [_om(mp, POS, POS_PATH), _om(mp, POS, POSBOX_STUB), _om(mp, "sale")]
        with pytest.raises(ValueError):
            _observe(store, mp, repo, "h1", dup)
        with pytest.raises(ValueError):
            store.diff(repo, dup)
        assert _q(db, "SELECT count(*) AS n FROM module_presence") == [{"n": 0}]


# ---------------------------------------------------------------------------
# Rule 6 - repo delete (H3)
# ---------------------------------------------------------------------------

class TestRepoRemoved:
    def test_repo_delete_keeps_rows_pending_repo_removed_then_retires_by_row_id(
            self, store, mp, repo, db):
        from src.db.pg import get_pool
        from src.db.repo_registry import RepoStore

        _observe(store, mp, repo, "h1", [
            "viin_ai_search",
            _om(mp, "viin_ai_skill", state="excluded", exclusion_reason="license_skip"),
            "old_mod"])
        store.mark_needs_rewrite(repo, "viin_ai_search")
        store.commit_retired(repo, "old_mod")
        old_before = _row(db, repo, "old_mod")

        flagged = store.mark_repo_removed(repo)
        assert sorted((f["name"], f["odoo_version"]) for f in flagged) == [
            ("viin_ai_search", V), ("viin_ai_skill", V)]
        for n in ("viin_ai_search", "viin_ai_skill"):
            r = _row(db, repo, n)
            assert (r["retire_pending"], r["retire_pending_reason"], r["needs_rewrite"]) == (
                True, "repo_removed", False)
            assert r["state"] != "retired"
        assert _row(db, repo, "old_mod")["retire_pending"] is False
        assert _row(db, repo, "old_mod")["state"] == old_before["state"] == "retired"

        RepoStore(get_pool()).delete_repo(repo)

        orphans = {r["name"]: r for r in _q(
            db, "SELECT * FROM module_presence WHERE repo_id IS NULL")}
        assert set(orphans) == {"viin_ai_search", "viin_ai_skill", "old_mod"}
        assert {(r["repo_url"], r["repo_basename"], r["repo_branch"], r["profile_name"])
                for r in orphans.values()} == {
            ("git@github.com:Viindoo/tvtmaaddons.git", "tvtmaaddons", V, "mp_p")}

        pending = {r["name"]: r for r in store.pending_retirements(V)}
        assert set(pending) == {"viin_ai_search", "viin_ai_skill"}
        assert all(r["repo_id"] is None for r in pending.values())

        rid = pending["viin_ai_search"]["id"]
        assert store.commit_retired(None, "viin_ai_search", row_id=rid) is True
        row = _q(db, "SELECT * FROM module_presence WHERE id = %s", (rid,))[0]
        assert (row["state"], row["retire_reason"], row["retire_pending"]) == (
            "retired", "repo_removed", False)
        assert {r["name"] for r in store.pending_retirements(V)} == {"viin_ai_skill"}

    def test_removed_repo_rows_are_not_owners(self, store, mp, db):
        pid = _profile(db, "mp_own_rm")
        gone = _repo(db, pid, "gone")
        _observe(store, mp, gone, "h1", ["viin_ai_rag"], profile="mp_own_rm")
        store.mark_repo_removed(gone)
        with db.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = %s", (gone,))
        assert store.other_present_owners("viin_ai_rag", V) == []


# ---------------------------------------------------------------------------
# Rule 8 - ownership (H5)
# ---------------------------------------------------------------------------

class TestOwnership:
    @pytest.fixture
    def world(self, store, mp, db):
        """p_own @V: r1..r4 synced; r5 registered never synced; r6 unsynced.
        p_oth @98.0: r7 never synced (other version)."""
        pid = _profile(db, "p_own")
        r = {k: _repo(db, pid, k, head_sha="h1") for k in ("r1", "r2", "r3", "r4", "r6")}
        r["r5"] = _repo(db, pid, "r5")
        name = "viin_ai_rag"
        _observe(store, mp, r["r1"], "h1", [name], profile="p_own")
        _observe(store, mp, r["r2"], "h1", [name], profile="p_own")
        store.mark_retire_pending(r["r2"], [name])
        _observe(store, mp, r["r3"], "h1", [
            _om(mp, name, state="excluded", exclusion_reason="license_skip")], profile="p_own")
        _observe(store, mp, r["r4"], "h1", [name], profile="p_own")
        store.commit_retired(r["r4"], name)
        _observe(store, mp, r["r6"], "h0", [
            "viin_ai_agent", _om(mp, "viin_ai_skill", state="excluded",
                                 exclusion_reason="installable_false"), "old_mod"],
            profile="p_own")
        store.commit_retired(r["r6"], "old_mod")
        for k in ("r1", "r2", "r3", "r4"):
            store.mark_presence_synced(r[k], "h1")
        store.mark_presence_synced(r["r6"], "h0")   # repos.head_sha h1 != h0
        poth = _profile(db, "p_oth", V_OTHER)
        r["r7"] = _repo(db, poth, "r7", branch=V_OTHER)
        return r

    def test_present_owners_include_pending_rows_but_not_excluded_or_retired(self, store, world):
        owners = store.other_present_owners("viin_ai_rag", V)
        assert [o["repo_id"] for o in owners] == sorted([world["r1"], world["r2"]])
        by_repo = {o["repo_id"]: o for o in owners}
        assert by_repo[world["r2"]]["retire_pending"] is True
        assert by_repo[world["r1"]]["repo_basename"] == "r1"

    def test_present_owners_exclude_the_asking_repo_and_other_versions(self, store, world):
        assert [o["repo_id"] for o in store.other_present_owners(
            "viin_ai_rag", V, exclude_repo_id=world["r1"])] == [world["r2"]]
        assert store.other_present_owners("viin_ai_rag", V_OTHER) == []

    def test_never_synced_repo_blocks_every_name_at_its_version(self, store, world):
        for name in ("viin_ai_rag", "never_heard_of"):
            unsynced = store.potential_owners_unsynced(name, V)
            assert {(u["repo_id"], u["why"]) for u in unsynced} >= {(world["r5"], "never_synced")}
        assert world["r7"] not in {
            u["repo_id"] for u in store.potential_owners_unsynced("viin_ai_rag", V)}

    def test_synced_repos_never_appear_as_potential_owners(self, store, world):
        ids = {u["repo_id"] for u in store.potential_owners_unsynced("viin_ai_rag", V)}
        assert ids.isdisjoint({world["r1"], world["r2"], world["r3"], world["r4"]})

    def test_unsynced_repo_appears_only_for_names_it_had_present_or_excluded(
            self, store, world):
        def pairs(name):
            return {(u["repo_id"], u["why"])
                    for u in store.potential_owners_unsynced(name, V)}

        assert pairs("viin_ai_agent") == {(world["r6"], "had_name"),
                                         (world["r5"], "never_synced")}
        assert pairs("viin_ai_skill") == {(world["r6"], "had_name"),
                                         (world["r5"], "never_synced")}
        assert pairs("viin_ai_rag") == {(world["r5"], "never_synced")}
        assert pairs("old_mod") == {(world["r5"], "never_synced")}   # retired: not had

    def test_exclude_repo_id_drops_the_asking_repo(self, store, world):
        assert store.potential_owners_unsynced(
            "viin_ai_rag", V, exclude_repo_id=world["r5"]) == []

    def test_repo_with_presence_head_but_no_head_sha_is_unsynced(self, store, mp, db):
        pid = _profile(db, "p_nohead")
        rid = _repo(db, pid, "nohead")
        _observe(store, mp, rid, "h1", ["sale_z"], profile="p_nohead")
        store.mark_presence_synced(rid, "h1")        # repos.head_sha stays NULL
        assert {u["repo_id"] for u in store.potential_owners_unsynced("sale_z", V)} == {rid}

    def test_repo_sync_state_and_present_names(self, store, world):
        state = {s["repo_id"]: s["synced"] for s in store.repo_sync_state(V)}
        assert state[world["r1"]] is True
        assert state[world["r5"]] is False
        assert state[world["r6"]] is False
        assert world["r7"] not in state
        # viin_ai_rag present in r1/r2; viin_ai_agent in r6; skill excluded; old_mod retired
        assert store.present_names(V) == {"viin_ai_rag", "viin_ai_agent"}


# ---------------------------------------------------------------------------
# Rule 9 - needs_rewrite (M5)
# ---------------------------------------------------------------------------

class TestNeedsRewrite:
    def test_only_present_rows_can_be_flagged(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", [
            "sale_a", _om(mp, "sale_x", state="excluded", exclusion_reason="license_skip"),
            "sale_r"])
        store.commit_retired(repo, "sale_r")
        assert store.mark_needs_rewrite(repo, "sale_x") is False
        assert store.mark_needs_rewrite(repo, "sale_r") is False
        assert store.mark_needs_rewrite(repo, "no_such") is False
        assert store.mark_needs_rewrite(repo, "sale_a") is True
        assert store.needs_rewrite_names(repo) == ["sale_a"]

    def test_flag_survives_reobservation_while_present_and_is_consumed_once(
            self, store, mp, repo):
        _observe(store, mp, repo, "h1", ["sale_a", "sale_b"])
        store.mark_needs_rewrite(repo, "sale_a")
        _observe(store, mp, repo, "h2", ["sale_a", "sale_b"])
        assert store.needs_rewrite_names(repo) == ["sale_a"]      # peek does not clear
        assert store.needs_rewrite_names(repo) == ["sale_a"]
        assert store.consume_needs_rewrite(repo) == ["sale_a"]
        assert store.consume_needs_rewrite(repo) == []
        assert store.needs_rewrite_names(repo) == []

    def test_becoming_excluded_clears_the_flag(self, store, mp, repo, db):
        _observe(store, mp, repo, "h1", ["sale_a"])
        store.mark_needs_rewrite(repo, "sale_a")
        _observe(store, mp, repo, "h2", [
            _om(mp, "sale_a", state="excluded", exclusion_reason="installable_false")])
        assert _row(db, repo, "sale_a")["needs_rewrite"] is False

    def test_clear_needs_rewrite_clears_only_the_named_rows(self, store, mp, repo):
        _observe(store, mp, repo, "h1", ["sale_a", "sale_b"])
        store.mark_needs_rewrite(repo, "sale_a")
        store.mark_needs_rewrite(repo, "sale_b")
        assert store.clear_needs_rewrite(repo, ["sale_a"]) == 1
        assert store.needs_rewrite_names(repo) == ["sale_b"]


# ---------------------------------------------------------------------------
# Rule 11 - repos lifecycle columns (H1, H4)
# ---------------------------------------------------------------------------

class TestRepoColumns:
    def test_presence_synced_and_attention_round_trip(self, store, repo, db):
        store.mark_presence_synced(repo, "h7")
        store.set_lifecycle_attention(repo, "G-A tripped: 3 manifests unreadable")
        r = _q(db, "SELECT presence_head_sha, lifecycle_attention, lifecycle_attention_at "
                   "FROM repos WHERE id = %s", (repo,))[0]
        assert r["presence_head_sha"] == "h7"
        assert r["lifecycle_attention"] == "G-A tripped: 3 manifests unreadable"
        assert r["lifecycle_attention_at"] is not None
        store.clear_lifecycle_attention(repo)
        r = _q(db, "SELECT presence_head_sha, lifecycle_attention, lifecycle_attention_at "
                   "FROM repos WHERE id = %s", (repo,))[0]
        assert (r["lifecycle_attention"], r["lifecycle_attention_at"]) == (None, None)
        assert r["presence_head_sha"] == "h7"

    def test_unknown_repo_raises_repo_not_found(self, store):
        from src.db.exceptions import RepoNotFoundError

        with pytest.raises(RepoNotFoundError):
            store.mark_presence_synced(987654, "h1")
        with pytest.raises(RepoNotFoundError):
            store.set_lifecycle_attention(987654, "x")
        with pytest.raises(RepoNotFoundError):
            store.clear_lifecycle_attention(987654)

    def test_empty_attention_text_is_rejected(self, store, repo, db):
        with pytest.raises(ValueError):
            store.set_lifecycle_attention(repo, "")
        assert _q(db, "SELECT lifecycle_attention FROM repos WHERE id = %s", (repo,)) == [
            {"lifecycle_attention": None}]


# ---------------------------------------------------------------------------
# L7 - rename_profile
# ---------------------------------------------------------------------------

class TestRenameProfile:
    def _seed(self, store, mp, db):
        pid = _profile(db, "p_old")
        r1 = _repo(db, pid, "a")
        r2 = _repo(db, pid, "b")
        _observe(store, mp, r1, "h1", ["sale_a", "sale_gone"], profile="p_old")
        store.commit_retired(r1, "sale_gone")
        _observe(store, mp, r2, "h1", ["sale_a"], profile="p_old")
        store.mark_repo_removed(r2)
        with db.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = %s", (r2,))
        return pid

    def test_rename_rewrites_every_row_including_history(self, store, mp, db):
        self._seed(store, mp, db)
        assert store.rename_profile("p_old", "p_new") == 3
        assert _q(db, "SELECT DISTINCT profile_name FROM module_presence") == [
            {"profile_name": "p_new"}]
        assert len(store.lifecycle_rows("sale_a", V, ["p_new"])) == 2
        assert len(store.lifecycle_rows("sale_gone", V, ["p_new"])) == 1
        assert store.lifecycle_rows("sale_a", V, ["p_old"]) == []

    def test_rename_joins_the_callers_transaction(self, store, mp, db, raw_conns):
        self._seed(store, mp, db)
        tx = raw_conns()
        tx.autocommit = False
        store.rename_profile("p_old", "p_new", conn=tx)
        tx.rollback()
        assert _q(db, "SELECT DISTINCT profile_name FROM module_presence") == [
            {"profile_name": "p_old"}]


# ---------------------------------------------------------------------------
# Rule 7 - lifecycle_rows (R15, "renamed from")
# ---------------------------------------------------------------------------

def _seed_tenants(store, mp, db):
    pa = _profile(db, "tenant_a")
    pb = _profile(db, "tenant_b")
    ra = _repo(db, pa, "tvtmaaddons", head_sha="h1")
    rb = _repo(db, pb, "globex_addons", head_sha="h1")
    _observe(store, mp, ra, "before", ["test_pylint"], profile="tenant_a")
    _observe(store, mp, ra, "0240c6b77f", ["test_viin_pylint"], profile="tenant_a")
    store.mark_retire_pending(
        ra, ["test_pylint"],
        evidence={"test_pylint": mp.RetireEvidence(sha="0240c6b77f",
                                                   date="2026-09-11T00:00:00Z")},
        successors={"test_pylint": mp.Successor(names=("test_viin_pylint",),
                                                source="git_rename")},
    )
    store.commit_retired(ra, "test_pylint", head_sha="0240c6b77f")
    _observe(store, mp, rb, "h1", ["test_viin_pylint"], profile="tenant_b")
    return {"ra": ra, "rb": rb}


class TestLifecycleRows:
    @pytest.fixture
    def tenants(self, store, mp, db):
        return _seed_tenants(store, mp, db)

    def test_tenant_sees_own_rows_plus_predecessor_and_no_other_tenant(self, store, tenants):
        rows = store.lifecycle_rows("test_viin_pylint", V, ["tenant_a"])
        assert [(r["name"], r["relation"], r["profile_name"]) for r in rows] == [
            ("test_viin_pylint", "self", "tenant_a"),
            ("test_pylint", "predecessor", "tenant_a"),
        ]
        pred = rows[1]
        assert (pred["state"], pred["successor_names"], pred["removing_commit_sha"]) == (
            "retired", ["test_viin_pylint"], "0240c6b77f")

    def test_retired_name_reads_as_self_with_its_successor(self, store, tenants):
        rows = store.lifecycle_rows("test_pylint", V, ["tenant_a"])
        assert [(r["relation"], r["state"], r["successor_names"]) for r in rows] == [
            ("self", "retired", ["test_viin_pylint"])]
        assert store.lifecycle_rows("test_pylint", V, ["tenant_b"]) == []

    def test_empty_allowed_profiles_is_fail_closed(self, store, tenants):
        assert store.lifecycle_rows("test_viin_pylint", V, []) == []

    def test_unscoped_and_multi_tenant_reads_see_every_profile(self, store, tenants):
        for allowed in (None, ["tenant_a", "tenant_b"]):
            rows = store.lifecycle_rows("test_viin_pylint", V, allowed)
            assert sorted((r["profile_name"], r["relation"]) for r in rows) == [
                ("tenant_a", "predecessor"), ("tenant_a", "self"), ("tenant_b", "self")]

    def test_all_versions_are_ordered_numerically_newest_first(self, store, mp, db):
        versions = ["9.0", "17.0", "master", V]
        for v in versions:
            pid = _profile(db, f"p_ord_{v}", v)
            rid = _repo(db, pid, "odoo", branch=v, url="https://github.com/odoo/odoo.git")
            _observe(store, mp, rid, "h1", ["payment_ogone"], profile=f"p_ord_{v}", version=v)
        rows = store.lifecycle_rows("payment_ogone", None, None)
        assert [r["odoo_version"] for r in rows] == [V, "17.0", "9.0", "master"]
        assert [r["odoo_version"] for r in store.lifecycle_rows(
            "payment_ogone", "17.0", None)] == ["17.0"]

    def test_osm_reader_under_rls_sees_only_the_allowed_tenant(
            self, clean_pg, raw_conns, mp):
        """Same read as the MCP tier's least-privilege role: RLS + explicit filter."""
        from src.db.pg import get_pool
        from tests.conftest import drop_osm_reader, ensure_osm_reader_or_skip

        ensure_osm_reader_or_skip(clean_pg)
        try:
            try:
                with clean_pg.cursor() as cur:
                    cur.execute("GRANT osm_reader TO CURRENT_USER")
            except psycopg2.errors.InsufficientPrivilege as exc:
                pytest.skip(f"cannot become osm_reader: {exc}")
            run_migrations(clean_pg)
            store = mp.ModulePresenceStore(get_pool(), lock_wait_seconds=1.0)
            _seed_tenants(store, mp, clean_pg)

            reader = raw_conns()
            with reader.cursor() as cur:
                cur.execute("SET ROLE osm_reader")
            rows = store.lifecycle_rows("test_viin_pylint", V, ["tenant_a"], conn=reader)
            assert sorted((r["profile_name"], r["relation"]) for r in rows) == [
                ("tenant_a", "predecessor"), ("tenant_a", "self")]
            assert store.lifecycle_rows("test_viin_pylint", V, [], conn=reader) == []
            # None = admin/unscoped: same '*' sentinel as the embeddings read path
            # (server._allowed_to_guc(None) == '*'), so even osm_reader sees all.
            rows = store.lifecycle_rows("test_viin_pylint", V, None, conn=reader)
            assert sorted(r["profile_name"] for r in rows) == [
                "tenant_a", "tenant_a", "tenant_b"]
        finally:
            drop_osm_reader(clean_pg)


# ---------------------------------------------------------------------------
# rows_for_repo / lifecycle_counts (B10 JSON, B11 audit)
# ---------------------------------------------------------------------------

def test_lifecycle_counts_per_repo_with_zeros_for_unknown_ids(store, mp, repo):
    _observe(store, mp, repo, "h1", [
        "sale_a", "sale_b",
        _om(mp, "sale_x", state="excluded", exclusion_reason="license_skip"), "sale_r"])
    store.commit_retired(repo, "sale_r")
    store.mark_retire_pending(repo, ["sale_b"])
    store.mark_needs_rewrite(repo, "sale_a")
    counts = store.lifecycle_counts([repo, 987654])
    assert counts[repo] == {"present": 2, "excluded": 1, "retired": 1,
                            "retire_pending": 1, "needs_rewrite": 1}
    assert counts[987654] == {"present": 0, "excluded": 0, "retired": 0,
                              "retire_pending": 0, "needs_rewrite": 0}
    assert sorted(r["name"] for r in store.rows_for_repo(repo)) == [
        "sale_a", "sale_b", "sale_r", "sale_x"]


# ---------------------------------------------------------------------------
# Rule 10 - per-version lock (H2b)
# ---------------------------------------------------------------------------

class TestVersionLock:
    @pytest.fixture
    def two_versions(self, db):
        p99 = _profile(db, "lock_99")
        p98 = _profile(db, "lock_98", V_OTHER)
        return _repo(db, p99, "a"), _repo(db, p98, "b", branch=V_OTHER)

    def test_ledger_write_on_another_session_times_out_while_version_is_locked(
            self, store, mp, db, raw_conns, two_versions):
        from src.db.exceptions import LifecycleLockTimeout

        r99, _ = two_versions
        holder, other = raw_conns(), raw_conns()
        with store.version_lock(V, conn=holder):
            t0 = time.monotonic()
            with pytest.raises(LifecycleLockTimeout):
                _observe(store, mp, r99, "h1", ["sale_a"], profile="lock_99")
            waited = time.monotonic() - t0
            with pytest.raises(LifecycleLockTimeout):
                store.commit_observed(r99, profile_name="lock_99", odoo_version=V,
                                      head_sha="h1", observed=[_om(mp, "sale_a")], conn=other)
        assert waited >= 0.9, f"gave up after {waited:.2f}s, budget is 1.0s"
        assert _row(db, r99, "sale_a") is None

    def test_every_ledger_writer_respects_the_lock(self, store, mp, db, raw_conns, two_versions):
        from src.db.exceptions import LifecycleLockTimeout

        r99, _ = two_versions
        _observe(store, mp, r99, "h1", ["sale_a"], profile="lock_99")
        holder = raw_conns()
        writers = [
            lambda: store.mark_retire_pending(r99, ["sale_a"]),
            lambda: store.commit_retired(r99, "sale_a"),
            lambda: store.mark_repo_removed(r99),
            lambda: store.mark_needs_rewrite(r99, "sale_a"),
            lambda: store.rename_profile("lock_99", "lock_99b"),
        ]
        before = _snapshot(db)
        with store.version_lock(V, conn=holder):
            for write in writers:
                with pytest.raises(LifecycleLockTimeout):
                    write()
        assert _snapshot(db) == before

    def test_other_versions_and_reads_are_not_blocked(
            self, store, mp, db, raw_conns, two_versions):
        r99, r98 = two_versions
        _observe(store, mp, r99, "h1", ["sale_a"], profile="lock_99")
        holder = raw_conns()
        with store.version_lock(V, conn=holder):
            _observe(store, mp, r98, "h1", ["sale_b"], profile="lock_98", version=V_OTHER)
            assert store.diff(r99, []).absent == {"sale_a": "present"}
            assert [o["repo_id"] for o in store.other_present_owners("sale_a", V)] == [r99]
        assert _row(db, r98, "sale_b")["state"] == "present"

    def test_same_session_is_reentrant_inside_version_lock(
            self, store, mp, db, raw_conns, two_versions):
        r99, _ = two_versions
        holder = raw_conns()
        with store.version_lock(V, conn=holder) as locked:
            store.commit_observed(r99, profile_name="lock_99", odoo_version=V, head_sha="h1",
                                  observed=[_om(mp, "sale_a")], conn=locked)
            store.mark_retire_pending(r99, ["sale_a"], conn=locked)
        assert _row(db, r99, "sale_a")["retire_pending"] is True

    def test_two_sessions_cannot_hold_the_version_lock_at_once(self, store, raw_conns):
        from src.db.exceptions import LifecycleLockTimeout

        a, b = raw_conns(), raw_conns()
        with store.version_lock(V, conn=a):
            with pytest.raises(LifecycleLockTimeout):
                with store.version_lock(V, conn=b):
                    pass
        # released on exit: b can take it now
        with store.version_lock(V, conn=b):
            pass

    def test_waiter_proceeds_once_the_holder_releases_within_the_budget(
            self, mp, db, raw_conns, two_versions):
        from src.db.pg import get_pool

        r99, _ = two_versions
        patient = mp.ModulePresenceStore(get_pool(), lock_wait_seconds=10.0)
        holder = raw_conns()
        acquired = threading.Event()

        def hold():
            with patient.version_lock(V, conn=holder):
                acquired.set()
                time.sleep(0.8)

        t = threading.Thread(target=hold)
        t.start()
        assert acquired.wait(5)
        t0 = time.monotonic()
        _observe(patient, mp, r99, "h1", ["sale_a"], profile="lock_99")
        waited = time.monotonic() - t0
        t.join()
        assert _row(db, r99, "sale_a")["state"] == "present"
        assert 0.3 <= waited < 10.0, waited
