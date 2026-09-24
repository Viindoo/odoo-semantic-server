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
