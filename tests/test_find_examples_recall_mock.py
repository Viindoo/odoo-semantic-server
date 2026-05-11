"""Mock recall benchmark for find_examples — runs in CI without Ollama.

Tests the ranking logic (cosine similarity + centrality rerank) with
deterministic cluster-aware embeddings.  No network, no GPU.

Approach
--------
We define 3 semantic clusters with 2–3 snippets each:

  A — "compute tax based on partner country"
  B — "render PDF report"
  C — "send email confirmation"

A ``ClusterEmbedder`` is used that maps cluster membership to a tight ball
of vectors in 1024-D space:

  * Each cluster is anchored by a fixed, normalized basis vector.
  * Per-snippet jitter (seed=hash(text)) adds a small Gaussian perturbation
    (scale 0.05) so no two snippets have identical vectors.
  * A query for cluster A gets the *exact* anchor vector for cluster A,
    guaranteeing it is closer to all A-members than to any B or C member
    (the inter-cluster angle is ~90°).

The test verifies:
  1. Top-3 results for each cluster query all belong to the correct cluster.
  2. No cross-cluster leakage in top-3.

Markers: postgres + neo4j (needs live PG + Neo4j via testcontainers or CI
service containers, consistent with other embedding integration tests).
"""
import math
import random

import pytest

from tests.conftest import PG_EMBED_VERSION as TEST_VERSION

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]


# ---------------------------------------------------------------------------
# Cluster-aware deterministic embedder
# ---------------------------------------------------------------------------

DIM = 1024

# Three orthogonal anchor vectors (Gram-Schmidt over unit-axis candidates).
# We seed each with a fixed integer so they are constant across runs.
def _make_anchor(seed: int) -> list[float]:
    """Normalized Gaussian vector seeded by integer — deterministic."""
    rng = random.Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(DIM)]
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


# Anchor seeds chosen arbitrarily — far apart in seed space → near-orthogonal.
_ANCHOR_A = _make_anchor(1001)
_ANCHOR_B = _make_anchor(2002)
_ANCHOR_C = _make_anchor(3003)

_ANCHORS = {"A": _ANCHOR_A, "B": _ANCHOR_B, "C": _ANCHOR_C}


def _cluster_vec(cluster: str, jitter_seed: int, jitter_scale: float = 0.05) -> list[float]:
    """Return a normalized vector near the cluster anchor.

    Jitter is controlled by ``jitter_seed`` (hash of the snippet text) so
    every distinct snippet gets a distinct, but cluster-close, vector.
    """
    anchor = _ANCHORS[cluster]
    rng = random.Random(jitter_seed)
    perturbed = [a + rng.gauss(0, jitter_scale) for a in anchor]
    norm = math.sqrt(sum(x * x for x in perturbed))
    return [x / norm for x in perturbed]


# Curated dataset: (text, cluster)
# Cluster A — tax / country logic
# Cluster B — PDF report rendering
# Cluster C — email confirmation
_SNIPPETS: list[tuple[str, str]] = [
    # Cluster A — 3 snippets
    (
        "[tax_module] sale.order: _get_tax_country (method)\n"
        "def _get_tax_country(self):\n"
        "    return self.partner_id.country_id",
        "A",
    ),
    (
        "[account] account.tax: compute_based_on_partner (method)\n"
        "def compute_based_on_partner(self, partner):\n"
        "    if partner.country_id.code == 'VN':\n"
        "        return self.amount * 0.1",
        "A",
    ),
    (
        "[l10n_vn] sale.order: tax_id (field)\n"
        "tax_id = fields.Many2one('account.tax', "
        "domain=\"[('country_id', '=', partner_country)]\",",
        "A",
    ),
    # Cluster B — 2 snippets
    (
        "[sale] sale.order: action_print_report (method)\n"
        "def action_print_report(self):\n"
        "    return self.env.ref('sale.action_report_saleorder')"
        ".report_action(self)",
        "B",
    ),
    (
        "[account] account.move: _render_pdf_report (method)\n"
        "def _render_pdf_report(self):\n"
        "    report = self.env['ir.actions.report']\n"
        "    return report._render_qweb_pdf('account.report_invoice', self.ids)",
        "B",
    ),
    # Cluster C — 2 snippets
    (
        "[sale] sale.order: action_send_confirmation_email (method)\n"
        "def action_send_confirmation_email(self):\n"
        "    template = self.env.ref('sale.email_template_edi_sale')\n"
        "    template.send_mail(self.id, force_send=True)",
        "C",
    ),
    (
        "[mail] mail.thread: _send_confirmation_notification (method)\n"
        "def _send_confirmation_notification(self, partner_ids):\n"
        "    self.message_post(\n"
        "        body=_('Order confirmed'), partner_ids=partner_ids,\n"
        "        subtype_xmlid='mail.mt_comment',\n"
        "    )",
        "C",
    ),
]

# Cluster → module mapping (used to seed Neo4j Module nodes)
_CLUSTER_MODULE: dict[str, str] = {"A": "tax_module", "B": "sale", "C": "mail"}

# Synthetic entity names derived from snippet text headers
def _entity_name(snippet_text: str) -> str:
    """Extract entity name from snippet header line."""
    first_line = snippet_text.splitlines()[0]
    # Format: "[module] entity (type)"
    after_bracket = first_line.split("] ", 1)[-1]
    return after_bracket.split(" (")[0].strip()


def _module_name(snippet_text: str) -> str:
    """Extract module name from snippet header line."""
    first_line = snippet_text.splitlines()[0]
    start = first_line.index("[") + 1
    end = first_line.index("]")
    return first_line[start:end]


class ClusterEmbedder:
    """Deterministic cluster-aware embedder for mock recall tests.

    ``text_to_cluster`` maps snippet text → cluster label ('A', 'B', 'C').
    Query texts (not in the map) can pass an explicit cluster label via
    the ``query_cluster`` parameter at construction time; otherwise query
    is treated as the exact anchor (no jitter).
    """

    def __init__(
        self,
        text_to_cluster: dict[str, str],
        query_cluster: str | None = None,
    ):
        self._map = text_to_cluster
        self._query_cluster = query_cluster

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            if text in self._map:
                cluster = self._map[text]
                # Use hash of the full text as jitter seed for determinism
                vec = _cluster_vec(cluster, jitter_seed=hash(text) & 0xFFFFFFFF)
            elif self._query_cluster is not None:
                # Query: return exact anchor (jitter=0 → sharpest similarity)
                vec = list(_ANCHORS[self._query_cluster])
            else:
                # Fallback: zero-jitter anchor A
                vec = list(_ANCHOR_A)
            result.append(vec)
        return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_clusters(clean_pg_embeddings, clean_neo4j):
    """Seed Neo4j Module nodes + PostgreSQL embeddings for cluster dataset.

    Uses ClusterEmbedder without a query_cluster (only indexes snippets).
    """
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    text_to_cluster = {text: cluster for text, cluster in _SNIPPETS}
    embedder = ClusterEmbedder(text_to_cluster=text_to_cluster)

    # Register Neo4j Module nodes (required for the centrality rerank query)
    modules_seen: set[str] = set()
    with clean_neo4j.session() as s:
        for _text, _cluster in _SNIPPETS:
            mod = _module_name(_text)
            if mod not in modules_seen:
                s.run("MERGE (:Module {name:$n, odoo_version:$v})", n=mod, v=TEST_VERSION)
                modules_seen.add(mod)

    # Write embeddings per module (writer groups by module internally)
    module_chunks: dict[str, list[EmbeddingChunk]] = {}
    for text, cluster in _SNIPPETS:
        mod = _module_name(text)
        entity = _entity_name(text)
        chunk = EmbeddingChunk(
            chunk_type="method",
            module=mod,
            odoo_version=TEST_VERSION,
            entity_name=entity,
            model_name=None,
            file_path=f"{mod}/models/m.py",
            chunk_idx=0,
            content=text,
        )
        module_chunks.setdefault(mod, []).append(chunk)

    for mod, chunks in module_chunks.items():
        write_module_embeddings(clean_pg_embeddings, mod, TEST_VERSION, chunks, embedder)

    return clean_pg_embeddings, clean_neo4j, text_to_cluster


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _cluster_of_entity(entity_name: str, text_to_cluster: dict[str, str]) -> str | None:
    """Return cluster label for a result entity by matching against indexed snippets.

    The result entity_name typically arrives prefixed with `[module] ` from
    _find_examples output (e.g. `[tax_module] sale.order: _get_tax_country`).
    _entity_name() strips that prefix from snippet_text — strip the same way
    from the incoming entity_name before comparing.
    """
    # Strip leading `[module] ` if present
    stripped = entity_name.split("] ", 1)[-1] if entity_name.startswith("[") else entity_name
    stripped = stripped.strip()
    for text, cluster in text_to_cluster.items():
        if _entity_name(text) == stripped:
            return cluster
    return None


def _run_query(
    query_cluster: str,
    seeded_clusters,
    *,
    limit: int = 3,
) -> list[str]:
    """Run _find_examples with the anchor query for ``query_cluster``.

    Returns the list of entity names from the result output.
    """
    from src.mcp.server import _find_examples

    pg, neo4j_driver, text_to_cluster = seeded_clusters

    embedder = ClusterEmbedder(
        text_to_cluster=text_to_cluster,
        query_cluster=query_cluster,
    )

    result = _find_examples(
        f"cluster {query_cluster} query",
        odoo_version=TEST_VERSION,
        limit=limit,
        _driver=neo4j_driver,
        _pg_conn=pg,
        _embedder=embedder,
    )

    # Parse entity names from output lines starting with '#' and containing '·'
    entities = [
        line.split("·")[-1].strip()
        for line in result.splitlines()
        if line.startswith("#") and "·" in line
    ]
    return entities


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cluster_a_top3_all_in_cluster(seeded_clusters):
    """Query for cluster A (tax/country) → top-3 all belong to cluster A."""
    _pg, _neo4j, text_to_cluster = seeded_clusters
    entities = _run_query("A", seeded_clusters, limit=3)

    assert len(entities) >= 3, f"Expected at least 3 results, got {entities!r}"

    for entity in entities[:3]:
        cluster = _cluster_of_entity(entity, text_to_cluster)
        assert cluster == "A", (
            f"Entity '{entity}' belongs to cluster '{cluster}', expected 'A'.\n"
            f"Full top-3: {entities[:3]}"
        )


def test_cluster_b_top2_all_in_cluster(seeded_clusters):
    """Query for cluster B (PDF report) → top-2 all belong to cluster B.

    Cluster B has 2 snippets — we check top-2 (all that exist).
    """
    _pg, _neo4j, text_to_cluster = seeded_clusters
    entities = _run_query("B", seeded_clusters, limit=2)

    assert len(entities) >= 2, f"Expected at least 2 results, got {entities!r}"

    for entity in entities[:2]:
        cluster = _cluster_of_entity(entity, text_to_cluster)
        assert cluster == "B", (
            f"Entity '{entity}' belongs to cluster '{cluster}', expected 'B'.\n"
            f"Full top-2: {entities[:2]}"
        )


def test_cluster_c_top2_all_in_cluster(seeded_clusters):
    """Query for cluster C (email confirmation) → top-2 all belong to cluster C."""
    _pg, _neo4j, text_to_cluster = seeded_clusters
    entities = _run_query("C", seeded_clusters, limit=2)

    assert len(entities) >= 2, f"Expected at least 2 results, got {entities!r}"

    for entity in entities[:2]:
        cluster = _cluster_of_entity(entity, text_to_cluster)
        assert cluster == "C", (
            f"Entity '{entity}' belongs to cluster '{cluster}', expected 'C'.\n"
            f"Full top-2: {entities[:2]}"
        )


def test_no_cross_cluster_leakage_top3_cluster_a(seeded_clusters):
    """Top-3 for cluster A must contain zero B or C snippets."""
    _pg, _neo4j, text_to_cluster = seeded_clusters
    entities = _run_query("A", seeded_clusters, limit=3)

    wrong = [
        (e, _cluster_of_entity(e, text_to_cluster))
        for e in entities[:3]
        if _cluster_of_entity(e, text_to_cluster) != "A"
    ]
    assert not wrong, (
        f"Cross-cluster leakage in top-3 for cluster A: {wrong}\n"
        "The ranking logic may be ignoring cosine similarity — "
        "check the pgvector ORDER BY in _find_examples."
    )


def test_find_examples_header_present(seeded_clusters):
    """Result string must contain the standard header format."""
    from src.mcp.server import _find_examples

    pg, neo4j_driver, text_to_cluster = seeded_clusters
    embedder = ClusterEmbedder(
        text_to_cluster=text_to_cluster,
        query_cluster="A",
    )
    result = _find_examples(
        "compute tax based on partner country",
        odoo_version=TEST_VERSION,
        limit=3,
        _driver=neo4j_driver,
        _pg_conn=pg,
        _embedder=embedder,
    )
    assert 'find_examples: "compute tax based on partner country"' in result
    assert TEST_VERSION in result
    assert "Found" in result
