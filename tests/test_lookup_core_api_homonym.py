# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lookup_core_api_homonym.py
"""issue #117 bug#4 — lookup_core_api bare-name homonym ranking.

`lookup_core_api('flush', 16)` used to resolve to the SHORTEST qualified name
(``ORDER BY size(qualified_name) ASC``), which is a stable homonym
(``odoo.api.Transaction.flush``), shadowing the migration-relevant deprecated
``odoo.models.BaseModel.flush``. The ranking now prefers the deprecated/removed
candidate for an ambiguous bare name, while an EXACT qualified-name lookup still
wins (so callers who pass the full path get exactly what they asked for).
"""
import os
import sys

import pytest

from src.indexer.models import CoreSymbolInfo
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

# Unique disposable versions (issue #117 block 68-72), each owned only by this
# file so module-scoped teardown never wipes a sibling fixture; not forbidden.
# VERSION = the "from" side (deprecated BaseModel.flush present);
# REMOVED_VERSION = the "to" side where BaseModel.flush is gone (homonym diff test).
VERSION = "70.0"
REMOVED_VERSION = "72.0"

# Ground truth (verified against odoo8..odoo19 on disk): the three ".flush"
# homonym qualified names have DISTINCT lengths in real source (Transaction=26,
# BaseModel=27, BaseCursor=28), so the size() tiebreak alone is non-deterministic
# only for OTHER same-length homonyms — the ranking here selects on status, not size.


@pytest.fixture(scope="module")
def seeded_homonyms(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        for _v in (VERSION, REMOVED_VERSION):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_v)
    # Three ".flush" homonyms at the "from" version. The deprecated BaseModel.flush
    # must win the bare-name lookup on STATUS, not size (its qname is neither the
    # shortest nor the longest), so the old size-only ordering hid it behind a
    # stable homonym.
    writer.write_core_symbols([
        CoreSymbolInfo(
            qualified_name="odoo.api.Transaction.flush",  # stable
            kind="function", odoo_version=VERSION,
            signature="flush(self)", status="stable",
        ),
        CoreSymbolInfo(
            qualified_name="odoo.sql_db.BaseCursor.flush",  # stable
            kind="cursor_method", odoo_version=VERSION,
            signature="flush(self)", status="stable",
        ),
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.flush",  # DEPRECATED
            kind="orm_method", odoo_version=VERSION,
            signature="flush(self, fnames=None, records=None)",
            status="deprecated",
            replacement_qname="odoo.models.BaseModel.flush_recordset",
        ),
    ])
    # The "to" version: BaseModel.flush was REMOVED (mirrors v16->v17). Only the two
    # stable homonyms survive — so a bare-name lookup here resolves to a DIFFERENT
    # symbol than at `VERSION`, the exact condition api_version_diff must reconcile.
    writer.write_core_symbols([
        CoreSymbolInfo(
            qualified_name="odoo.api.Transaction.flush",
            kind="function", odoo_version=REMOVED_VERSION,
            signature="flush(self)", status="stable",
        ),
        CoreSymbolInfo(
            qualified_name="odoo.sql_db.BaseCursor.flush",
            kind="cursor_method", odoo_version=REMOVED_VERSION,
            signature="flush(self)", status="stable",
        ),
    ])
    yield VERSION
    with neo4j_driver.session() as session:
        for _v in (VERSION, REMOVED_VERSION):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_v)
    writer.close()


@pytest.fixture
def spec_tools(seeded_homonyms):
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp import server as mcp_server
    return mcp_server


class TestLookupHomonymRanking:
    def test_bare_name_prefers_deprecated_homonym(self, spec_tools, seeded_homonyms):
        """Ambiguous bare 'flush' surfaces the DEPRECATED BaseModel.flush, not a
        shorter stable homonym."""
        out = spec_tools._lookup_core_api("flush", VERSION)
        assert "odoo.models.BaseModel.flush" in out, (
            f"expected the deprecated BaseModel.flush to win the bare-name lookup, got:\n{out}"
        )
        assert "deprecated" in out.lower()
        # The replacement should be surfaced for migration guidance.
        assert "flush_recordset" in out

    def test_exact_qualified_name_still_wins(self, spec_tools, seeded_homonyms):
        """A full qualified name overrides the deprecated-preference heuristic."""
        out = spec_tools._lookup_core_api("odoo.api.Transaction.flush", VERSION)
        assert "odoo.api.Transaction.flush" in out
        # The deprecated BaseModel.flush must NOT hijack an exact-path request.
        assert "odoo.models.BaseModel.flush" not in out


class TestApiVersionDiffHomonym:
    """`_fetch_core_symbol` is shared by api_version_diff. A bare name must diff the
    SAME symbol across versions — not silently compare a deprecated symbol on the old
    version against an unrelated stable homonym on the new version."""

    def test_bare_name_diff_tracks_one_symbol_across_versions(
        self, spec_tools, seeded_homonyms,
    ):
        """'flush' is deprecated BaseModel.flush at VERSION and removed at
        REMOVED_VERSION. The diff must report it as REMOVED (anchored on the
        deprecated symbol), not fall through to the stable Transaction.flush and
        claim 'stable'/'signature changed'."""
        out = spec_tools._api_version_diff("flush", VERSION, REMOVED_VERSION)
        assert "removed" in out.lower(), (
            f"expected BaseModel.flush reported as removed across versions, got:\n{out}"
        )
        # Migration target must be surfaced from the anchored (old) symbol.
        assert "flush_recordset" in out
        # The unrelated stable homonym must NOT hijack the new-version side.
        assert "Transaction.flush" not in out
        assert "Stable across versions" not in out
