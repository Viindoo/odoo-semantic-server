# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recall benchmark for find_examples — requires Ollama + Qwen3-Embedding-4B + indexed data.

Gate:
  VN recall@5 >= 0.75   (38/50 queries must hit)
  EN recall@5 >= 0.80   (40/50 queries must hit)
  gap(EN-VN) <= 0.05

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

# 100-query stratified benchmark set (50 VN + 50 EN).
# Format: (query, expected_entity_substring, chunk_type)
# A query "hits" when expected_entity_substring appears in any of the top-5 entity names.

_VN_EVAL = [
    # --- Methods (25 VN) ---
    ("xác nhận đơn hàng", "action_confirm", "method"),
    ("ghi nhận thanh toán", "action_register_payment", "method"),
    ("tạo hóa đơn từ đơn hàng", "action_create_invoice", "method"),
    ("xác nhận nhận hàng", "action_done", "method"),
    ("gửi báo giá cho khách hàng", "action_quotation_send", "method"),
    ("xác nhận nhập kho", "button_validate", "method"),
    ("tính giá trị tồn kho", "_compute_qty_available", "method"),
    ("viết cập nhật bản ghi", "write", "method"),
    ("tạo mới bản ghi", "create", "method"),
    ("thay đổi thông tin khách hàng", "onchange_partner_id", "method"),
    ("lấy nhật ký kế toán mặc định", "_get_default_journal", "method"),
    ("xem danh sách hóa đơn liên quan", "action_view_invoice", "method"),
    ("in và gửi hóa đơn", "action_send_and_print", "method"),
    ("xác nhận đơn mua hàng", "button_confirm", "method"),
    ("tính tổng tiền đơn hàng", "_compute_amount", "method"),
    ("kiểm tra tính hợp lệ của bản ghi", "_check_validity", "method"),
    ("tính lại giá bán", "_recompute_prices", "method"),
    ("lấy thuế áp dụng cho sản phẩm", "_get_computed_taxes", "method"),
    ("hủy đơn hàng đã xác nhận", "action_cancel", "method"),
    ("hoàn trả hàng về kho", "action_return", "method"),
    ("chốt đơn hàng thành done", "action_done", "method"),
    ("ghi nhận chi phí dự án", "action_validate", "method"),
    ("phê duyệt yêu cầu mua hàng", "button_approve", "method"),
    ("tính lương nhân viên", "_compute_wage", "method"),
    ("ghi nhận ngày chấm công", "action_attendance", "method"),
    # --- Fields (25 VN) ---
    ("tính tổng tiền hóa đơn", "amount_total", "field"),
    ("lấy danh sách sản phẩm", "product_id", "field"),
    ("kiểm tra tồn kho", "qty_available", "field"),
    ("cập nhật địa chỉ khách hàng", "partner_id", "field"),
    ("tính giá bán sau chiết khấu", "price_unit", "field"),
    ("lấy thuế của sản phẩm", "tax_ids", "field"),
    ("tên công ty khách hàng", "name", "field"),
    ("ngày đặt hàng", "date_order", "field"),
    ("trạng thái đơn hàng", "state", "field"),
    ("đơn vị tiền tệ của đơn hàng", "currency_id", "field"),
    ("điều khoản thanh toán cho khách", "payment_term_id", "field"),
    ("nhóm danh mục sản phẩm", "categ_id", "field"),
    ("đơn vị tính của sản phẩm", "uom_id", "field"),
    ("giá vốn hàng bán", "standard_price", "field"),
    ("số lượng dự kiến trong kho", "virtual_available", "field"),
    ("tài khoản kế toán trên bút toán", "account_id", "field"),
    ("kho lưu trữ hàng hóa", "warehouse_id", "field"),
    ("phân tích chi phí dự án", "analytic_account_id", "field"),
    ("nhân viên phụ trách đơn hàng", "user_id", "field"),
    ("bảng lương nhân viên", "payslip_id", "field"),
    ("số ngày làm việc trong tháng", "worked_days_line_ids", "field"),
    ("hướng xuất nhập kho", "route_id", "field"),
    ("số lô hàng trong kho", "lot_id", "field"),
    ("bảng giá áp dụng cho khách", "pricelist_id", "field"),
    ("công ty của bản ghi", "company_id", "field"),
]

_EN_EVAL = [
    # --- Methods (25 EN) ---
    ("confirm sale order", "action_confirm", "method"),
    ("register payment for invoice", "action_register_payment", "method"),
    ("create invoice from sale", "action_create_invoice", "method"),
    ("validate delivery order", "action_done", "method"),
    ("send quotation to customer", "action_quotation_send", "method"),
    ("validate stock picking", "button_validate", "method"),
    ("compute available stock quantity", "_compute_qty_available", "method"),
    ("write update record", "write", "method"),
    ("create new record", "create", "method"),
    ("onchange customer partner", "onchange_partner_id", "method"),
    ("get default accounting journal", "_get_default_journal", "method"),
    ("view related invoices", "action_view_invoice", "method"),
    ("print and send invoice", "action_send_and_print", "method"),
    ("confirm purchase order", "button_confirm", "method"),
    ("compute order total amount", "_compute_amount", "method"),
    ("check record validity", "_check_validity", "method"),
    ("recompute sale prices", "_recompute_prices", "method"),
    ("get applicable taxes on product", "_get_computed_taxes", "method"),
    ("cancel confirmed sale order", "action_cancel", "method"),
    ("return goods to warehouse", "action_return", "method"),
    ("lock order to done state", "action_done", "method"),
    ("validate project expense", "action_validate", "method"),
    ("approve purchase requisition", "button_approve", "method"),
    ("compute employee wage", "_compute_wage", "method"),
    ("record employee attendance", "action_attendance", "method"),
    # --- Fields (25 EN) ---
    ("compute invoice total amount", "amount_total", "field"),
    ("get product list from order line", "product_id", "field"),
    ("check stock quantity available", "qty_available", "field"),
    ("update customer shipping address", "partner_id", "field"),
    ("compute unit price with discount", "price_unit", "field"),
    ("get tax lines on product", "tax_ids", "field"),
    ("get customer company name", "name", "field"),
    ("order date field", "date_order", "field"),
    ("order status state", "state", "field"),
    ("invoice currency", "currency_id", "field"),
    ("payment terms on invoice", "payment_term_id", "field"),
    ("product category", "categ_id", "field"),
    ("product unit of measure", "uom_id", "field"),
    ("product cost price", "standard_price", "field"),
    ("forecasted stock quantity", "virtual_available", "field"),
    ("accounting account on journal entry", "account_id", "field"),
    ("warehouse for stock location", "warehouse_id", "field"),
    ("project analytic account", "analytic_account_id", "field"),
    ("responsible salesperson", "user_id", "field"),
    ("employee payslip", "payslip_id", "field"),
    ("worked days on payslip", "worked_days_line_ids", "field"),
    ("stock routing rule", "route_id", "field"),
    ("serial lot number", "lot_id", "field"),
    ("applied pricelist", "pricelist_id", "field"),
    ("record company field", "company_id", "field"),
]


def _recall_at_k(results_entity_names: list[str], expected: str, k: int = 5) -> bool:
    return any(expected in name for name in results_entity_names[:k])


def _extract_entities(result_text: str) -> list[str]:
    """Parse entity names from _find_examples output lines."""
    return [
        line.split("·")[-1].strip()
        for line in result_text.splitlines()
        if line.startswith("#") and "·" in line
    ]


@pytest.fixture(scope="module")
def live_connections():
    """Open real Neo4j + PostgreSQL + Ollama embedder connections."""
    import psycopg2
    from neo4j import GraphDatabase
    from pgvector.psycopg2 import register_vector

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


def _compute_hits(query_set, driver, pg, embedder, *, odoo_version="auto", limit=5):
    """Run all queries and return (hits_list, misses_list)."""
    from src.mcp.server import _find_examples

    hits = []
    misses = []
    for query, expected_entity, _ in query_set:
        result = _find_examples(
            query, odoo_version=odoo_version, limit=limit,
            _driver=driver, _pg_conn=pg, _embedder=embedder,
        )
        entity_names = _extract_entities(result)
        hit = _recall_at_k(entity_names, expected_entity)
        hits.append(hit)
        if not hit:
            misses.append(query)
    return hits, misses


@pytest.mark.ollama
def test_recall_benchmark_aggregate(live_connections):
    """Gate: VN recall@5 >= 0.75, EN recall@5 >= 0.80, gap(EN-VN) <= 0.05.

    Single aggregate test across all 100 queries (50 VN + 50 EN).
    Reports all missed queries per language on failure.
    """
    driver, pg, embedder = live_connections

    vn_hits, vn_misses = _compute_hits(_VN_EVAL, driver, pg, embedder)
    en_hits, en_misses = _compute_hits(_EN_EVAL, driver, pg, embedder)

    vn_ratio = sum(vn_hits) / len(vn_hits)
    en_ratio = sum(en_hits) / len(en_hits)
    gap = en_ratio - vn_ratio

    assert vn_ratio >= 0.75, (
        f"VN recall@5 = {vn_ratio:.2f} ({sum(vn_hits)}/{len(vn_hits)}) < 0.75\n"
        f"Missed queries ({len(vn_misses)}): {vn_misses}"
    )
    assert en_ratio >= 0.80, (
        f"EN recall@5 = {en_ratio:.2f} ({sum(en_hits)}/{len(en_hits)}) < 0.80\n"
        f"Missed queries ({len(en_misses)}): {en_misses}"
    )
    assert gap <= 0.05, (
        f"gap(EN-VN) = {gap:.2f} > 0.05  (VN={vn_ratio:.2f}, EN={en_ratio:.2f})"
    )
