# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_reconcile_placeholder_relink_unit.py
"""The lifecycle reconcile relinks placeholder edges BEFORE it GCs the placeholders.

Business rule: ``gc_unresolved_placeholders`` DETACH DELETEs every
``__unresolved__`` placeholder with its edges. An edge whose real target was
indexed since (a parent written by a later repo) must be moved onto that target
first, or the relation is lost until the child module is re-parsed. If the
relink fails, the GC must not run (the placeholders are invisible to readers
and can wait for the next run; the edges cannot be recovered once deleted).

No DB: a recording fake writer; the Neo4j behaviour of the relink itself is
covered by ``tests/test_writer_inherits_resolution.py`` (neo4j-marked).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.indexer.reconcile import ReconcileReport, _Reconciler

V = "99.0"


class _RecordingWriter:
    def __init__(self, relink_error: Exception | None = None):
        self.calls: list[str] = []
        self._relink_error = relink_error

    def gc_orphan_asset_bundles(self, version):
        self.calls.append("gc_orphan_asset_bundles")
        return 0

    def relink_unresolved_placeholders(self, version):
        self.calls.append("relink_unresolved_placeholders")
        if self._relink_error is not None:
            raise self._relink_error
        return {"INHERITS": 2, "DELEGATES_TO": 1, "EXTENDS_TMPL": 1}

    def gc_unresolved_placeholders(self, version):
        self.calls.append("gc_unresolved_placeholders")
        return {"Model": 3, "View": 0, "QWebTmpl": 1, "OWLComp": 0}

    def gc_null_repo_dep_stubs(self, version):
        self.calls.append("gc_null_repo_dep_stubs")
        return 0


def _reconciler(writer) -> _Reconciler:
    return _Reconciler(
        V, writer=writer, store=MagicMock(), conn=None, run_started_at=None,
        retire=True, allow_mass_retire=False, dry_run=False,
        report=ReconcileReport(odoo_version=V),
    )


def test_placeholder_edges_are_relinked_before_the_placeholder_gc():
    writer = _RecordingWriter()
    rec = _reconciler(writer)
    rec.global_gcs(run_dep_stub_gc=False)
    calls = writer.calls
    assert calls.index("relink_unresolved_placeholders") < calls.index(
        "gc_unresolved_placeholders"
    )
    assert rec.report.gc["placeholder_edges_relinked"] == 4
    assert rec.report.gc["unresolved_placeholders"] == 4
    assert not rec.report.errors


def test_a_failed_relink_skips_the_placeholder_gc_and_is_reported():
    writer = _RecordingWriter(relink_error=ValueError("neo4j said no"))
    rec = _reconciler(writer)
    rec.global_gcs(run_dep_stub_gc=True)
    assert "gc_unresolved_placeholders" not in writer.calls
    assert "neo4j said no" in rec.report.errors["relink_unresolved_placeholders"]
    # The other version-wide GCs are unaffected.
    assert "gc_orphan_asset_bundles" in writer.calls
    assert "gc_null_repo_dep_stubs" in writer.calls


class _BundleWriter(_RecordingWriter):
    def __init__(self, bundle_error: Exception | None = None):
        super().__init__()
        self._bundle_error = bundle_error

    def reconcile_asset_bundle_owners(self, version):
        self.calls.append("reconcile_asset_bundle_owners")
        if self._bundle_error is not None:
            raise self._bundle_error
        return 2


def test_shared_bundles_follow_their_live_contributors_after_the_orphan_gc():
    """ADR-0057 R9: the surviving shared bundles are reset to their live
    contributors right after the orphan-bundle GC, in every reconcile."""
    writer = _BundleWriter()
    rec = _reconciler(writer)
    rec.global_gcs(run_dep_stub_gc=False)
    calls = writer.calls
    assert calls.index("gc_orphan_asset_bundles") < calls.index(
        "reconcile_asset_bundle_owners"
    )
    assert rec.report.gc["asset_bundle_owners"] == 2
    assert not rec.report.errors


def test_a_failed_bundle_owner_reset_is_reported_and_the_other_gcs_still_run():
    writer = _BundleWriter(bundle_error=ValueError("bundle reset refused"))
    rec = _reconciler(writer)
    rec.global_gcs(run_dep_stub_gc=True)
    assert "bundle reset refused" in rec.report.errors["reconcile_asset_bundle_owners"]
    assert "gc_unresolved_placeholders" in writer.calls
    assert "gc_null_repo_dep_stubs" in writer.calls
