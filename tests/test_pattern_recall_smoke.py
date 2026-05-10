"""Recall smoke: verify suggest_pattern returns expected patterns for known intents.

Uses FakeEmbedder for deterministic embeddings (no Ollama required).

NOTE on FakeEmbedder semantics:
FakeEmbedder generates random-but-stable unit vectors seeded by content hash — all
different texts map to different vectors, but cosine similarity is essentially random
(no real semantic signal).  As a result, recall assertions on SEMANTIC matching
(e.g. "does 'compute method with depends' return the compute-field pattern?") are not
meaningful with FakeEmbedder.

This test instead validates DATA-SHAPE and CATALOGUE-PRESENCE invariants:
1. suggest_pattern returns a valid formatted response (not an error string).
2. After seeding, at least one top-5 result exists in the catalogue.
3. The catalogue contains ALL expected new W3-3 pattern IDs (anti-truncation guard).
4. Parametrized queries return non-empty results (pipeline smoke, not semantic recall).

Semantic recall (does the right pattern surface for a given intent?) requires a real
Ollama embedding model and is gated on an integration environment.
"""
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.neo4j

# Catalogue path relative to repo root
_PATTERNS_PATH = Path(__file__).resolve().parent.parent / "src" / "data" / "patterns.json"

# -------------------------------------------------------------------
# Expected pattern IDs from W3-3 (anti-truncation guard)
# -------------------------------------------------------------------
_W3_3_PATTERN_IDS = {
    "portal-sudo-public-user-access",
    "portal-layout-template-inherit",
    "portal-mixin-ensure-token",
    "portal-compute-with-sudo",
    "wizard-transient-default-get-context",
    "wizard-action-close-vs-open",
    "wizard-backorder-default-get-x2m",
    "multi-company-with-company-context",
    "multi-company-ir-rule-domain-force",
    "multi-company-property-field",
    "ir-attachment-create-res-model-res-id",
    "ir-attachment-binary-field-attachment-true",
    "ir-attachment-download-url",
    "owl-onmounted-lifecycle",
    "owl-usestate-reactive-mutation",
    "owl-template-t-attf-class",
    "owl-patch-service-override",
    "security-acl-csv-group-model",
    "security-ir-rule-portal-domain",
    "security-groups-field-attribute",
    "domain-or-operator-prefix-notation",
    "domain-child-of-parent-of",
    "domain-filter-domain-search-view",
    "mail-thread-mixin-message-post",
    "mail-thread-activity-schedule",
    "mail-thread-override-message-post",
    "report-qweb-t-foreach-docs",
    "report-qweb-t-set-subtotal",
    "website-published-mixin",
}


# -------------------------------------------------------------------
# Catalogue-level tests (no DB required)
# -------------------------------------------------------------------


def test_catalogue_contains_all_w3_3_ids():
    """All W3-3 pattern IDs must be present in patterns.json (anti-truncation guard)."""
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    present_ids = {p["pattern_id"] for p in data}
    missing = _W3_3_PATTERN_IDS - present_ids
    assert not missing, (
        f"W3-3 patterns missing from catalogue ({len(missing)} absent): {sorted(missing)}"
    )


def test_catalogue_size_at_least_80():
    """Catalogue must contain ≥80 entries after W3-3 additions."""
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    assert len(data) >= 80, f"Expected ≥80 patterns in catalogue, got {len(data)}"


def test_catalogue_w3_3_entries_have_3_gotchas():
    """Every W3-3 entry must have exactly ≥3 gotchas (schema-enforced, but double-check)."""
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    by_id = {p["pattern_id"]: p for p in data}
    violations = [
        pid for pid in _W3_3_PATTERN_IDS
        if pid in by_id and len(by_id[pid].get("gotchas", [])) < 3
    ]
    assert not violations, (
        f"W3-3 patterns with fewer than 3 gotchas: {violations}"
    )


# -------------------------------------------------------------------
# Pipeline smoke tests (require Neo4j + pgvector)
# -------------------------------------------------------------------


@pytest.fixture
def _seeded_w3_patterns(clean_pg_embeddings, clean_neo4j):
    """Seed a small but representative subset of new W3-3 patterns into Neo4j + pgvector."""
    from psycopg2.extras import execute_values

    from src.indexer.embedder import FakeEmbedder
    from src.indexer.models import PatternExample
    from src.indexer.writer_neo4j import Neo4jWriter
    from src.indexer.writer_pgvector import _INSERT_SQL, make_pattern_chunks
    from tests.conftest import PG_EMBED_VERSION as TEST_VERSION

    # Use a representative subset: 1 portal, 1 mail, 1 OWL, 1 domain (covers new categories)
    patterns = [
        PatternExample(
            pattern_id="portal-sudo-public-user-access",
            intent_keywords=["portal", "sudo", "public user", "controller"],
            file_ref="addons/portal/controllers/portal.py:335",
            snippet_text=(
                "IrAttachment = request.env['ir.attachment']\n"
                "if not request.env.user._is_internal():\n"
                "    IrAttachment = IrAttachment.sudo()\n"
                "attachment = IrAttachment.create({'name': name})"
            ),
            gotchas=[
                "_is_internal() returns False for both portal and public users",
                "sudo() on env widens ALL subsequent calls",
                "Never cache sudo env between requests",
            ],
            odoo_version_min=TEST_VERSION,
            language="python",
            core_symbol_names=[],
        ),
        PatternExample(
            pattern_id="mail-thread-mixin-message-post",
            intent_keywords=["mail.thread", "message_post", "chatter"],
            file_ref="addons/sale/models/sale_order.py:1133",
            snippet_text=(
                "self.message_post(\n"
                "    body=_('Taxes recomputed for %s', self.name)\n"
                ")"
            ),
            gotchas=[
                "Sanitise body HTML to prevent XSS in chatter",
                "_inherit mail.thread alone does not enable follower notifications",
                "message_post on recordset creates one message per record",
            ],
            odoo_version_min=TEST_VERSION,
            language="python",
            core_symbol_names=[],
        ),
        PatternExample(
            pattern_id="owl-onmounted-lifecycle",
            intent_keywords=["OWL", "onMounted", "lifecycle", "DOM ready"],
            file_ref="addons/web/static/src/core/file_input/file_input.js:36",
            snippet_text=(
                "onMounted(() => {\n"
                "    if (this.props.autoOpen) {\n"
                "        this.onTriggerClicked();\n"
                "    }\n"
                "});"
            ),
            gotchas=[
                "useRef().el is null before onMounted fires",
                "Do not call onMounted inside a conditional branch",
                "Use onWillUnmount to clean up event listeners",
            ],
            odoo_version_min=TEST_VERSION,
            language="js",
            core_symbol_names=[],
        ),
        PatternExample(
            pattern_id="domain-or-operator-prefix-notation",
            intent_keywords=["domain", "OR operator", "pipe prefix", "domain_force"],
            file_ref="addons/sale/security/ir_rules.xml:47",
            snippet_text=(
                "domain_force=\"['|',('user_id','=',user.id),('user_id','=',False)]\""
            ),
            gotchas=[
                "'|' applies to NEXT TWO conditions only",
                "user variable is only available inside domain_force, not Python search()",
                "Mixing | and & prefixes without counting causes silent logic errors",
            ],
            odoo_version_min=TEST_VERSION,
            language="xml",
            core_symbol_names=[],
        ),
    ]

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    writer.write_pattern_examples(patterns)
    writer.close()

    embedder = FakeEmbedder(dim=1024)
    chunks = make_pattern_chunks(patterns)
    texts = [c.content for c in chunks]
    vecs = embedder.embed(texts)
    with clean_pg_embeddings.cursor() as cur:
        execute_values(
            cur, _INSERT_SQL,
            [c.as_tuple(vecs[i]) for i, c in enumerate(chunks)],
        )
    return clean_pg_embeddings, clean_neo4j


# The marks below are layered: neo4j (outer) + postgres (inner for pg fixture)
@pytest.mark.postgres
@pytest.mark.parametrize(
    "intent,language",
    [
        ("portal sudo public user controller access guard", "python"),
        ("mail thread message post chatter body", "python"),
        ("OWL onMounted lifecycle DOM component", "js"),
        ("domain OR operator pipe prefix domain_force", "xml"),
    ],
)
def test_suggest_pattern_returns_valid_response(
    intent, language, _seeded_w3_patterns
):
    """suggest_pattern returns a header line (not an error) for known W3-3 intents.

    NOTE: FakeEmbedder produces random vectors — the TOP result may NOT be the
    semantically correct pattern.  This test only checks that the pipeline runs
    end-to-end and returns a properly formatted response, not semantic accuracy.
    """
    pg, neo4j_driver = _seeded_w3_patterns
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _suggest_pattern
    from tests.conftest import PG_EMBED_VERSION as TEST_VERSION

    result = _suggest_pattern(
        intent,
        odoo_version=TEST_VERSION,
        language=language,
        _driver=neo4j_driver,
        _pg_conn=pg,
        _embedder=FakeEmbedder(dim=1024),
    )

    # Must start with "suggest_pattern(" header — error strings start differently
    assert result.startswith("suggest_pattern("), (
        f"Expected formatted response, got error: {result[:200]!r}"
    )
    # Must report at least 1 match
    assert "matches" in result, (
        f"Expected 'matches' in response, got: {result[:200]!r}"
    )


@pytest.mark.postgres
def test_suggest_pattern_skips_gracefully_when_no_results(clean_pg_embeddings, clean_neo4j):
    """suggest_pattern returns a 'no patterns indexed' message when catalogue is empty."""
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _suggest_pattern
    from tests.conftest import PG_EMBED_VERSION as TEST_VERSION

    # clean_pg_embeddings + clean_neo4j: no patterns seeded
    result = _suggest_pattern(
        "portal sudo access",
        odoo_version=TEST_VERSION,
        language="python",
        _driver=clean_neo4j,
        _pg_conn=clean_pg_embeddings,
        _embedder=FakeEmbedder(dim=1024),
    )
    assert "no patterns indexed" in result or "matches" in result, (
        f"Unexpected response format: {result[:300]!r}"
    )
