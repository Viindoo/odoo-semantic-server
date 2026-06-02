# Runbook — Backfill Neo4j graph metadata sau deploy code (node-property-only change)

> **Khi nào dùng runbook này:** bạn deploy một thay đổi code làm parser/writer **thêm hoặc đổi
> property trên Neo4j node** (Module/Model/Field/Method...) NHƯNG **không đổi chunk text /
> embeddings** của pgvector. Production đã index đầy đủ và cần "rải" property mới lên dữ liệu
> hiện có mà KHÔNG tốn lại toàn bộ thời gian index từ đầu.
>
> **Ví dụ cụ thể (trigger runbook này):** PR #241 / issue #238 (WI-1) — thêm `readonly`,
> `inverse`, `effective_readonly` vào `:Field` node (`src/indexer/writer_neo4j.py`,
> `parser_python.py`, `models.py`). KHÔNG chạm `embedder.py`/`writer_pgvector.py`.

## ⚠️ Nguyên tắc số 1 — ĐIỀU TRA HIỆN TRẠNG PRODUCTION TRƯỚC KHI QUYẾT ĐỊNH

Lệnh reindex KHÔNG phải đóng-hộp. Một lệnh sai (`--full` không kèm `--no-embed` khi embeddings
không đổi) sẽ re-embed thừa toàn bộ pgvector — có thể tốn **~1 ngày 1 đêm** vô ích. Ngược lại,
dùng `--no-embed` khi embeddings THỰC SỰ đã đổi sẽ để lại vector sai/cũ một cách âm thầm.

**Bắt buộc chạy checklist điều tra dưới đây và xác nhận từng mục TRƯỚC khi chọn lệnh.**

### Checklist điều tra (data-driven — đừng đoán, hãy đo)

1. **Diff thực sự chạm gì?** — `git diff <sha_đang_chạy_prod>..<sha_deploy> --stat`.
   - Có chạm `src/indexer/embedder.py`, `src/indexer/writer_pgvector.py`, hoặc bất kỳ hàm
     chunking (`make_*_chunks`, `_sliding`) không?
   - **KHÔNG chạm** → chunk text byte-identical → embeddings không đổi → `--no-embed` an toàn.
   - **CÓ chạm** → embeddings có thể đổi → **KHÔNG dùng `--no-embed`**; cần re-embed (runbook khác).

2. **Embedding model/dim có đổi không?** (ADR-0045) — query Postgres:
   ```sql
   SELECT DISTINCT embedding_model, embedding_dim FROM <chunk_table>;  -- vd module_chunks
   ```
   - Nếu provider/model/dim đổi so với env hiện tại (`EMBEDDER_*`) → **bắt buộc full re-embed**
     (guard `EmbedderDimMismatch` sẽ fail-fast nếu lệch) — đây là concern khác, KHÔNG dùng runbook này.

3. **Có job index đang chạy không?** — tránh chạy chồng:
   ```bash
   curl -s -H "Cookie: <admin_session>" https://<host>/api/jobs   # hoặc xem indexer_is_running
   ```
   Ghi lại `repos.head_sha` hiện tại của từng repo (để so sánh sau).

4. **Ước lượng thời gian THỰC TẾ (đo, đừng tin con số phỏng đoán):**
   - `GET /ready` → `embeddings_total` + `embeddings_by_chunk_type` (số chunk phải re-embed nếu
     buộc embed).
   - Xem log/metrics lần index gần nhất (nếu bật `embedder_batch_duration_seconds` — ADR-0010/M10C)
     để biết tỉ lệ thời gian **embed** vs **parse+Neo4j-write**. Với change node-property-only,
     `--no-embed` cắt bỏ phần embed (thường là bottleneck áp đảo) → chỉ còn parse + graph-write.

5. **Backup trước khi chạy.** Dù `--full` là **MERGE upsert in-place** (KHÔNG wipe — xem dưới),
   vẫn snapshot Neo4j theo [`disaster-recovery.md`](../disaster-recovery.md) trước mọi thao tác ghi hàng loạt.

6. **Chạy off-peak.** `--full` re-MERGE in-place không gây downtime (graph cũ vẫn phục vụ query,
   property mới xuất hiện dần), nhưng tăng tải DB đáng kể.

## Hiểu đúng ngữ nghĩa lệnh (vì sao KHÔNG phải "xóa rồi index lại")

- **`--full` = UPSERT in-place, KHÔNG wipe.** Writer dùng `MERGE (f:Field {name,model,module,odoo_version})`
  rồi `SET ...` vô điều kiện (`src/indexer/writer_neo4j.py`). Node cũ được **tìm lại + cập nhật
  property**, không bị xóa. ADR-0007 D4: *"`--full` = re-write what we have, then continue"* — chỉ
  **bỏ qua cơ chế skip incremental**, không có `DETACH DELETE`/clear-database.
- **Incremental KHÔNG đủ để backfill.** Deploy code ≠ đổi repo data → `git rev-parse HEAD` == stored
  `repos.head_sha` → indexer **skip toàn bộ** (`src/indexer/pipeline.py`), không module nào re-parse →
  property mới **không bao giờ được set**. ⇒ Bắt buộc `--full` để bypass skip.
- **Không có shortcut Cypher** khi property mới là dữ liệu đọc từ source (vd `readonly`/`inverse`).
  Chỉ backfill được bằng cách re-parse → bắt buộc chạy indexer, không thể `SET` thuần từ graph hiện có.
- **Xóa stale node** (rename/move) là cờ RIÊNG `--gc` (opt-in), KHÔNG bật mặc định, KHÔNG cần cho backfill.

## Lệnh khuyến nghị (sau khi checklist xác nhận: embeddings KHÔNG đổi)

```bash
# Neo4j-only backfill, KHÔNG re-embed pgvector:
<VENV>/bin/python -m src.indexer index-repo --all --full --no-embed
#   --full     → re-MERGE mọi module in-place → rải property mới lên mọi node (KHÔNG wipe)
#   --no-embed → bỏ qua re-embed pgvector (embeddings byte-identical → re-embed là lãng phí thuần)
# Tùy chọn song song: --max-workers N --profile-workers M
```

> 🚫 **KHÔNG chạy `--full` mà thiếu `--no-embed`** khi embeddings không đổi — sẽ re-embed lại
> toàn bộ pgvector (~phần lớn thời gian index từ đầu) vô ích.
>
> ✅ Chỉ bỏ `--no-embed` (tức re-embed) khi checklist mục 1–2 cho thấy chunk text / model / dim
> THỰC SỰ đã đổi.

## Verify sau backfill

```bash
# 1. Neo4j: property mới đã có trên Field node của một stored-related field đã biết:
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
  "MATCH (f:Field {odoo_version:'17.0'}) WHERE f.related IS NOT NULL
   RETURN f.effective_readonly AS ro, count(*) AS n ORDER BY ro;"
# Kỳ vọng: có nhóm ro=true (stored-related no-inverse) thay vì toàn null.

# 2. MCP smoke (thay <API_KEY>/<HOST>): model_inspect list-fields hiện cờ readonly/related:
#    model_inspect(model='workflow.instance', method='fields', odoo_version='17.0')
#    → dòng res_model phải có 'related=...' + 'readonly'.
```

## Rollback

Property mới chỉ **thêm** vào node; render degrade an toàn khi vắng. Nếu cần revert code, không
phải xóa property — chúng vô hại với code cũ (code cũ không đọc chúng). Không có thao tác rollback
DB bắt buộc.
