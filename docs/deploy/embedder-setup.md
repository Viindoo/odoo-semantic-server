# Embedder Setup — Ollama + Qwen3-Embedding

> Tài liệu này hướng dẫn setup backend embedder cho **M3 Semantic Wow**
> (`find_examples` MCP tool). Tách khỏi `docs/deploy.md` vì:
> - Nhiều admin đã có Ollama instance riêng cho dự án khác (vd
>   qwen-coder, autocomplete) — chỉ cần **bổ sung 1 model**.
> - M1+M2+M4 KHÔNG cần file này — index `--no-embed` là đủ.

---

## 0. Khi nào cần đọc file này

- ✅ Bạn muốn dùng MCP tool `find_examples` (semantic code search,
  M3 Semantic Wow).
- ✅ Indexer đang chạy với embeddings (KHÔNG `--no-embed`).
- ❌ Bạn chỉ test E2E M1 (`resolve_model/field/method`), M2
  (`resolve_view`), M4 (`impact_analysis`) → skip file này.

---

## 1. Topology — chọn 1 trong 3

| Path | Khi nào chọn | Setup |
|------|-------------|-------|
| **A. Local Ollama** (cùng host MCP) | Dev / E2E test / single-host prod | §2 + §3 + §5 |
| **B. Remote Ollama dedicated** (Ollama VM riêng cho 1 dự án) | Production split-tier; cần GPU dedicated | §2 + §3 + §4 + §5 |
| **C. Remote Ollama shared** (1 Ollama instance cho nhiều dự án) | Đã có instance dùng cho qwen-coder/chat — chỉ thêm 1 model | §3 + §5 (skip §2 + §4 nếu remote đã setup) |

---

## 2. Path A/B: Cài Ollama from scratch (skip nếu Path C)

```bash
# Linux (Ubuntu/Debian) — official installer
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama

# Verify daemon đang chạy
curl -s http://localhost:11434/api/tags
# Expected: JSON {"models": [...]} — empty list nếu chưa load model nào
```

Ollama daemon chạy bằng system user `ollama` (tự tạo bởi installer).
Model storage default: `/usr/share/ollama/.ollama/models/`.

---

## 3. Add model `qwen3-embedding-q5km` (mọi Path)

Đây là bước duy nhất Path C cần làm trên Ollama có sẵn.

> **Tại sao Q5_K_M?** Default `ollama pull qwen3-embedding:4b` ship
> Q4_K_M. Q5_K_M cho recall cao hơn (~+3%) cho cùng latency, đáng đổi
> 800MB disk thêm.

### 3.1 Download GGUF vào dir Ollama daemon đọc được

Default model dir của Ollama systemd unit: `/usr/share/ollama/.ollama/`.
File phải readable bởi user `ollama` (daemon).

```bash
# Tạo dir + download (~3.2 GB)
sudo mkdir -p /usr/share/ollama/.ollama/models/gguf
sudo wget -O /usr/share/ollama/.ollama/models/gguf/qwen3-embedding-4b-q5km.gguf \
  "https://huggingface.co/Qwen/Qwen3-Embedding-GGUF/resolve/main/Qwen3-Embedding-4B-Q5_K_M.gguf"
sudo chown -R ollama:ollama /usr/share/ollama/.ollama/models/gguf
```

> ⚠️ **Lưu ý path**: KHÔNG dùng `~/.ollama/...` của user thường — daemon
> chạy bằng user `ollama`, sẽ không đọc được file ở home của bạn.

### 3.2 Tạo Modelfile + register

```bash
cat > /tmp/Modelfile-qwen3-embed << 'EOF'
FROM /usr/share/ollama/.ollama/models/gguf/qwen3-embedding-4b-q5km.gguf
EOF

# Register dưới user ollama (daemon)
sudo -u ollama ollama create qwen3-embedding-q5km -f /tmp/Modelfile-qwen3-embed

# Verify
sudo -u ollama ollama list | grep qwen3-embedding-q5km
# Expected: qwen3-embedding-q5km    <id>    3.2 GB    <date>
```

---

## 4. Path B/C: Configure remote access (skip nếu Path A)

Mặc định Ollama bind `127.0.0.1:11434` — chỉ local truy cập. Cho remote
App tier truy cập, override systemd unit:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf > /dev/null << 'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
EOF

sudo systemctl daemon-reload
sudo systemctl restart ollama
```

### Firewall — bắt buộc

Ollama **không có auth built-in**. Nếu để 11434 mở internet, ai cũng
chạy được model trên GPU của bạn (resource theft + potential abuse).

Whitelist IP App tier:

```bash
# UFW example — chỉ cho phép App VM IP truy cập 11434
sudo ufw allow from <APP_VM_IP> to any port 11434 proto tcp
sudo ufw deny 11434  # block mọi IP khác

# Hoặc iptables:
# sudo iptables -A INPUT -p tcp --dport 11434 -s <APP_VM_IP> -j ACCEPT
# sudo iptables -A INPUT -p tcp --dport 11434 -j DROP
```

### Qua Internet công cộng → SSH tunnel hoặc TLS reverse proxy

Nếu App tier và Embedder tier không trong cùng VPC:

| Option | Setup | Trade-off |
|--------|-------|-----------|
| **SSH tunnel** | App tier mở `ssh -L 11434:localhost:11434 user@embedder` (autossh + systemd) | Đơn giản, encrypted; cần SSH key + autossh service |
| **TLS reverse proxy** | Nginx/Caddy trước Ollama + cert + Basic Auth hoặc mTLS | Production-grade; setup phức tạp hơn |

> Cấu hình chi tiết SSH tunnel/reverse proxy nằm ngoài scope dự án —
> dùng pattern chuẩn của ops team.

---

## 5. Configure odoo-semantic.conf trên App tier

Thêm/cập nhật section `[embedder]` trong file config:

```ini
[embedder]
# Path A (local):    http://localhost:11434
# Path B (dedicated): http://<embedder-vm-ip>:11434
# Path C (shared):   http://<existing-ollama>:11434
url   = http://localhost:11434
model = qwen3-embedding-q5km
dim   = 1024
# auth_token = <bearer-token>   # optional — see §5.1 below
```

Hoặc env override (precedence cao hơn INI — phù hợp systemd
`Environment=`):

```
EMBEDDER_URL=http://<host>:11434
EMBEDDER_MODEL=qwen3-embedding-q5km
EMBEDDER_DIM=1024
```

### 5.1 Bearer auth — Ollama behind authenticated reverse proxy

Nếu Ollama được đặt sau Caddy/nginx với `Authorization: Bearer` check
(ví dụ để expose qua Internet mà không dùng VPN/SSH tunnel), thêm token:

```ini
[embedder]
url        = https://ollama.example.com
auth_token = <your-bearer-token>
```

Hoặc env var (khuyến nghị cho systemd — không lưu secret trong INI file):

```
EMBEDDER_AUTH_TOKEN=<your-bearer-token>
```

Khi `auth_token` được set, mọi request tới `/api/embed` sẽ gửi header
`Authorization: Bearer <token>`. Khi không set (default), header này bị
bỏ qua hoàn toàn — không ảnh hưởng gì đến loopback / VPC setups.

> **pgvector extension**: phải có trước khi indexer ghi embeddings.
> Docker compose của project tự init pgvector qua
> `docker/initdb.d/01-pgvector.sql` khi volume mới. Nếu volume `pg_data` đã
> tồn tại từ trước (vd test với `--no-embed` rồi mới setup embedder),
> rerun migrations là đủ:
> ```bash
> sudo -u odoo-semantic -H bash -c '
>     export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
>     /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate
> '
> ```
> Migration tạo bảng `embeddings` + HNSW index (idempotent).

---

## 6. Verify

### 6.1 Test embedder reach + dimension

```bash
# Từ App tier — gọi /api/embed (batch endpoint, input là array)
curl -s http://<embedder-host>:11434/api/embed \
    -d '{"model":"qwen3-embedding-q5km","input":["sale order tax compute"]}' \
    | python3 -c 'import json,sys; r=json.load(sys.stdin); print("dim =",len(r["embeddings"][0]))'
# Expected: dim = 1024
```

Nếu lỗi connection → check firewall + `OLLAMA_HOST`. Nếu `dim` ≠ 1024
→ sai model (không phải Qwen3-Embedding-4B), check `ollama list`.

### 6.2 Re-index với embeddings

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
        -m src.indexer index-repo --profile viindoo_17
'
# KHÔNG có --no-embed → indexer sẽ gọi Ollama
# ~400 modules × ~500 chunks × 1024 dim ≈ 20 GB disk
# Thời gian: ~30-60 phút lần đầu (incremental sau đó <5 phút)
```

Verify embeddings vào pgvector:

```bash
docker compose exec postgres psql -U odoo_semantic \
    -c "SELECT count(*) AS embeddings, count(DISTINCT module) AS modules FROM embeddings;"
# Expected: embeddings ≥ 50000, modules ≥ 100 cho Odoo 17 base
```

### 6.3 Smoke test `find_examples`

Dùng MCP client (xem `docs/deploy.md` §5.2):

```
find_examples("compute tax based on partner country")
```

Expected: list 5 results, mỗi entry có `file`, `module`, `score` (0-1).

### 6.4 Recall benchmark (optional, dev-side)

Trên máy có code repo + Ollama reach được:

```bash
make test-integration -- -m ollama tests/test_find_examples_recall.py
```

Expected pass: VN ≥ 0.75, EN ≥ 0.80.

---

## 7. License note

- **Qwen3-Embedding** Apache 2.0 — OK cho commercial use.
- **MS MARCO training data**: pending issue
  ([QwenLM/Qwen3-Embedding#166](https://github.com/QwenLM/Qwen3-Embedding/issues/166)).
- **Internal tooling** (Viindoo team dùng): OK.
- **External SaaS** (bán cho khách): cần legal review trước khi ship —
  cùng file thu phí dùng kèm model có thể vướng MS MARCO terms.

---

## 8. Tài liệu liên quan

| File | Đọc khi nào |
|------|-------------|
| [`../deploy.md`](../deploy.md) | Setup MCP server tổng thể (DB, App, Proxy) |
| [`../deploy.md` §3.4](../deploy.md#34-đăng-ký-repos--index-lần-đầu) | Re-index với embeddings sau khi setup xong file này |
| [`../deploy.md` §5.2](../deploy.md#52-verify-qua-mcp-client-claude-code) | Smoke test `find_examples` từ Claude Code |
| [`../deploy.md` §8.2](../deploy.md#82-tách-embedder-tier-ollama-ra-vm-riêng-hoặc-dùng-instance-shared) | Migration sang split-tier với Embedder tier riêng |
