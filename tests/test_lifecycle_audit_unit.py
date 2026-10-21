# SPDX-License-Identifier: AGPL-3.0-or-later
"""lifecycle-audit graph facade (ADR-0056 B11): the audit can read the graph
but can never write it, even when it reuses index / reconcile code that would.
No database needed."""
from __future__ import annotations

import pytest


class _RecordingWriter:
    """Stands in for Neo4jWriter: records every call, answers reads."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def orphan_module_names(self, odoo_version):
        self.calls.append("orphan_module_names")
        return ["ghost"]

    def module_identity(self, odoo_version, names):
        self.calls.append("module_identity")
        return {}

    def modules_without_profile(self, odoo_version):
        self.calls.append("modules_without_profile")
        return []

    def stamp_module_presence(self, odoo_version, rows, head, now=None):
        self.calls.append("stamp_module_presence")
        return 0

    def retire_modules(self, *args, **kwargs):
        self.calls.append("retire_modules")

    def drop_module_owner(self, *args, **kwargs):
        self.calls.append("drop_module_owner")

    def write_parse_result(self, *args, **kwargs):
        self.calls.append("write_parse_result")


@pytest.mark.parametrize("write", ["retire_modules", "drop_module_owner", "write_parse_result"])
def test_graph_writes_are_unreachable_through_the_audit_writer(write):
    from src.indexer.lifecycle_audit import ReadOnlyWriter

    inner = _RecordingWriter()
    ro = ReadOnlyWriter(inner)
    with pytest.raises(AttributeError):
        getattr(ro, write)
    assert inner.calls == []


def test_graph_reads_pass_through_to_the_real_writer():
    from src.indexer.lifecycle_audit import ReadOnlyWriter

    inner = _RecordingWriter()
    ro = ReadOnlyWriter(inner)
    assert ro.orphan_module_names("99.0") == ["ghost"]
    assert ro.modules_without_profile("99.0") == []
    assert inner.calls == ["orphan_module_names", "modules_without_profile"]


def test_presence_stamp_is_simulated_as_a_full_match_without_writing():
    """A successful run stamps every present module; the audit answers the same
    count and never reaches the real stamp."""
    from src.indexer.lifecycle_audit import ReadOnlyWriter

    inner = _RecordingWriter()
    ro = ReadOnlyWriter(inner)
    rows = iter([{"name": "a"}, {"name": "b"}, {"name": "c"}])
    assert ro.stamp_module_presence("99.0", rows, "abc123") == 3
    assert inner.calls == []


def _audit_report(**counts) -> dict:
    from src.indexer.lifecycle_audit import (
        FINDING_KEYS,
        INFORMATIONAL_FINDING_KEYS,
        has_actionable_findings,
    )

    findings = {k: counts.get(k, 0) for k in FINDING_KEYS}
    return {
        "scope": {"profile": "p", "all": False, "version": None}, "duration_s": 0.1,
        "repos": [], "versions": [], "findings": findings,
        "informational_findings": sorted(INFORMATIONAL_FINDING_KEYS),
        "has_findings": has_actionable_findings(findings),
    }


@pytest.mark.parametrize("counts, exit_code", [
    ({"parser_gaps": 3, "unproven_drops": 2}, 0),
    ({"parser_gaps": 3, "would_retire": 1}, 4),
    ({}, 0),
])
def test_kept_data_is_listed_but_only_drift_fails_the_weekly_audit(
    monkeypatch, capsys, counts, exit_code,
):
    """Parser gaps and unproven drops are kept by owner decision (ADR-0057) and
    only a code change resolves them: the weekly ``--fail-on-findings`` timer
    must not mail admins every week about them. They stay in the report."""
    import argparse

    import src.indexer.lifecycle_audit as audit
    from src.indexer.__main__ import _run_lifecycle_audit

    monkeypatch.setattr(audit, "run_lifecycle_audit", lambda **kw: _audit_report(**counts))
    args = argparse.Namespace(profile="p", all=False, version=None, json=False,
                              fail_on_findings=True)

    assert _run_lifecycle_audit(args) == exit_code
    out, err = capsys.readouterr()
    if counts.get("parser_gaps"):
        assert "kept (informational, not a finding): parser_gaps=3" in out
    if exit_code:
        assert "would_retire=1" in err and "parser_gaps" not in err
