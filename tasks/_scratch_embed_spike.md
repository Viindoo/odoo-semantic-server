---
status: scratch
scope: tasks/_scratch_embed_spike
audience: operator (not a permanent doc)
last-updated: 2026-04-22
TODO: DELETE sau khi research/embedding-self-host-spike.md ship và
      ADR-0002 Revision committed. Giữ lại kết luận, không giữ runbook.
---

# Scratch — embedding self-host spike runbook

Ghi chú tạm để chạy spike trên `osm-dev`. Xoá khi
`research/embedding-self-host-spike.md` committed.

## Mục tiêu

Self-retrieval benchmark (docstring → body) trên fixture Odoo CE subset,
3 models:

- `BAAI/bge-code-v1` (primary theo ADR-0002)
- `BAAI/bge-m3`
- `jinaai/jina-embeddings-v2-base-code`

Metric: recall@{1,5,10}, MRR, P50/P95 per-query latency, corpus encode
throughput, VRAM peak, disk size.

## Corpus stats (đã tạo local)

- `tests/fixtures/odoo_ce_subset` → 258 records sau filter
  `docstring ≥ 20 chars` + `body ≥ 3 non-trivial statements`
- Phân bố: mail 85, account 61, stock 39, base 30, sale 25, product 16,
  contacts 1, sale_management 1
- Body size p50=1238 chars, p95=6539, max=17412
- Docstring p50=230, p95=1349

**Caveat truncation:** 30.2% bodies > 2048 chars (~512 tokens) → jina-v2
(max_seq 512) sẽ truncate. bge-code-v1 + bge-m3 (max_seq 8192) cover hết.

## Bước chạy trên osm-dev

```bash
# 1. Sync code
cd /home/son-odoo/git/odoo/17.0/odoo-semantic-mcp
git pull

# 2. Activate venv (đã có từ session trước)
source ~/embed-spike-venv/bin/activate

# 3. Install sentence-transformers (torch 2.6.0+cu124 đã có)
pip install 'sentence-transformers>=3.0' huggingface-hub

# 4. Extract corpus (pure stdlib, ~1s)
mkdir -p /tmp/embed-spike/results
python scripts/bench_corpus.py \
  --addons tests/fixtures/odoo_ce_subset \
  --out /tmp/embed-spike/corpus.jsonl
# expect: wrote 258 records

# 5. Smoke 1 model trước (nhẹ nhất)
python scripts/bench_embed.py \
  --corpus /tmp/embed-spike/corpus.jsonl \
  --model jinaai/jina-embeddings-v2-base-code \
  --out /tmp/embed-spike/results/jina-v2-code.json

# 6. Nếu smoke OK, chạy 2 model còn lại
python scripts/bench_embed.py \
  --corpus /tmp/embed-spike/corpus.jsonl \
  --model BAAI/bge-m3 \
  --out /tmp/embed-spike/results/bge-m3.json

python scripts/bench_embed.py \
  --corpus /tmp/embed-spike/corpus.jsonl \
  --model BAAI/bge-code-v1 \
  --out /tmp/embed-spike/results/bge-code-v1.json
```

Copy 3 file JSON về máy dev (hoặc paste stdout) để mình viết report.

## Nếu model load fail

- `trust_remote_code=True` đã set — OK cho jina.
- `bge-code-v1` có thể là gated repo, cần `huggingface-cli login` nếu 401.
- VRAM OOM: giảm `--batch-size 16` hoặc `--batch-size 8`.

## Kết quả kỳ vọng (dùng để sanity check)

- `bge-code-v1`: recall@5 ≥ 75%, VRAM ~3-4GB, P95 < 80ms
- `bge-m3`: recall@5 ~ 60-70% (không code-specific), VRAM ~1.5GB
- `jina-v2-code`: recall@5 ~ 55-70% (nhỏ + truncate 30%), VRAM ~0.7GB

Nếu numbers lệch mạnh so với kỳ vọng → re-check batch size, truncation,
normalize_embeddings flag. Không commit JSON sai.

## Sau khi có 3 JSON

1. Paste 3 file JSON vào chat.
2. Mình viết `research/embedding-self-host-spike.md` + update ADR-0002
   Revision section.
3. Xoá file scratch này trong commit đóng spike.

## Outstanding

- [ ] Sync scripts lên osm-dev (git push + pull)
- [ ] Install sentence-transformers trong embed-spike-venv
- [ ] Run 3 models
- [ ] Paste kết quả
- [ ] (optional, đã plan trước) wrap systemd user service theo
      `tasks/_scratch_server_setup.md` § Wrap systemd user service
