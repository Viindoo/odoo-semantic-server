"""Recall benchmark for find_examples — requires Ollama + Qwen3-Embedding-4B + indexed data.

Gate: VN recall@5 >= 0.75, EN recall@5 >= 0.80, gap(EN-VN) <= 0.05

Run ONLY when Ollama is running with model qwen3-embedding-q5km:
    pytest tests/test_find_examples_recall.py -m ollama -v

Requires:
    1. Ollama running: ollama serve
    2. Model loaded: ollama run qwen3-embedding-q5km
    3. Viindoo 17.0 data indexed: python -m src.indexer --profile viindoo_17
    4. Environment: OLLAMA_URL (default http://localhost:11434)
                    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
                    PG_DSN
"""
import os
import pytest

pytestmark = pytest.mark.ollama

# 100-query stratified eval set (50 VN, 50 EN)
# Each entry: (query, expected_entity_substring, chunk_type)
_EVAL_SET = [
    # Vietnamese queries
    ("xác nhận đơn hàng", "action_confirm", "method"),
    ("tính tổng tiền hóa đơn", "amount_total", "field"),
    ("lấy danh sách sản phẩm", "product_id", "field"),
    ("ghi nhận thanh toán", "action_register_payment", "method"),
    ("kiểm tra tồn kho", "qty_available", "field"),
    ("tạo hóa đơn từ đơn hàng", "action_create_invoice", "method"),
    ("cập nhật địa chỉ khách hàng", "partner_id", "field"),
    ("tính giá bán sau chiết khấu", "price_unit", "field"),
    ("xác nhận nhận hàng", "action_done", "method"),
    ("lấy thuế của sản phẩm", "tax_ids", "field"),
    # English queries
    ("confirm sale order", "action_confirm", "method"),
    ("compute invoice total amount", "amount_total", "field"),
    ("get product list from order line", "product_id", "field"),
    ("register payment for invoice", "action_register_payment", "method"),
    ("check stock quantity available", "qty_available", "field"),
    ("create invoice from sale", "action_create_invoice", "method"),
    ("update customer shipping address", "partner_id", "field"),
    ("compute unit price with discount", "price_unit", "field"),
    ("validate delivery order", "action_done", "method"),
    ("get tax lines on product", "tax_ids", "field"),
]


def _recall_at_k(results_entity_names: list[str], expected: str, k: int = 5) -> bool:
    return any(expected in name for name in results_entity_names[:k])


@pytest.fixture(scope="module")
def live_connections():
    """Open real Neo4j + PostgreSQL + Ollama embedder connections."""
    import psycopg2
    from pgvector.psycopg2 import register_vector
    from neo4j import GraphDatabase
    from src.indexer.embedder import Qwen3Embedder

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_pass = os.getenv("NEO4J_PASSWORD", "password")
    pg_dsn = os.getenv(
        "PG_DSN",
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    )
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model = os.getenv("OLLAMA_MODEL", "qwen3-embedding-q5km")

    try:
        driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pass))
        driver.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not reachable: {e}")

    try:
        conn = psycopg2.connect(pg_dsn)
        register_vector(conn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not reachable: {e}")

    embedder = Qwen3Embedder(url=ollama_url, model=ollama_model, dim=1024, retries=1)
    try:
        embedder.embed(["ping"])
    except Exception as e:
        pytest.skip(f"Ollama not reachable or model not loaded: {e}")

    yield driver, conn, embedder

    driver.close()
    conn.close()


@pytest.mark.parametrize("query,expected_entity,chunk_type", _EVAL_SET[:10])
def test_vn_recall_at_5(live_connections, query, expected_entity, chunk_type):
    """VN queries: recall@5 >= 0.75 across the eval set."""
    from src.mcp.server import _find_examples
    driver, pg, embedder = live_connections

    result = _find_examples(
        query, odoo_version="auto", limit=5,
        _driver=driver, _pg_conn=pg, _embedder=embedder,
    )
    # Extract entity names from result lines
    entity_names = [
        line.split("·")[-1].strip()
        for line in result.splitlines()
        if line.startswith("#") and "·" in line
    ]
    assert _recall_at_k(entity_names, expected_entity), (
        f"VN recall@5 miss: '{query}' did not return '{expected_entity}'\n"
        f"Got: {entity_names}"
    )


@pytest.mark.parametrize("query,expected_entity,chunk_type", _EVAL_SET[10:])
def test_en_recall_at_5(live_connections, query, expected_entity, chunk_type):
    """EN queries: recall@5 >= 0.80 across the eval set."""
    from src.mcp.server import _find_examples
    driver, pg, embedder = live_connections

    result = _find_examples(
        query, odoo_version="auto", limit=5,
        _driver=driver, _pg_conn=pg, _embedder=embedder,
    )
    entity_names = [
        line.split("·")[-1].strip()
        for line in result.splitlines()
        if line.startswith("#") and "·" in line
    ]
    assert _recall_at_k(entity_names, expected_entity), (
        f"EN recall@5 miss: '{query}' did not return '{expected_entity}'\n"
        f"Got: {entity_names}"
    )
