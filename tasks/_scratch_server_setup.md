---
status: scratch
scope: tasks/_scratch_server_setup
audience: operator (not a permanent doc)
last-updated: 2026-04-22
TODO: DELETE trước Gate 2 release hoặc trước khi publish OSS. File này
      là ghi chú tạm cho việc setup máy server test dev-loop. Khi
      docs/docker-quickstart.md + docs/dev-workflow.md chính thức
      ship ở WP-10, xoá file này và chỉ giữ redirect comment trong
      tasks/todo.md.
---

# Scratch — setup máy server để test Phase 1

Ghi chú tạm cho workflow "laptop code, server chạy test". Xoá khi
deploy / public release. Không phải permanent doc.

## Trạng thái hiện tại (2026-04-22)

**Server đã dựng xong.** Bundle Phase 1 đang chạy live trên máy
`osm-dev` (WSL2 Ubuntu 24.04, Tailscale IP `100.102.154.32`).

**Máy dev đã connect 2026-04-22** — Tailscale up cùng tailnet, MCP
endpoint wired vào Claude Code ở project scope của workspace
`/home/soncrits/git/17.0/` (xem bước 4 dưới).

Đã xác nhận:

- Postgres 16.13 + pgvector native (systemd cluster, port 5432)
- DB `osm` + user `osm` + extension `vector` — OK
- `uv 0.11.7` user-scope (`~/.local/bin/uv`)
- `uv sync --extra dev` → 227 deps
- `uv run pytest -q` → **217 passed, 10 skipped** (skipped do hardcoded
  path `/home/soncrits/...` trong `tests/indexer/test_python_parser_real.py` —
  drift từ máy trước, không block runtime, fix sau)
- Fixture corpus indexed vào tenant `public`: 67 cache rows, 16 override
  links, 6 expected warnings
- MCP server (PID 97100) nghe `0.0.0.0:8765`, log `/tmp/osm-logs/server.log`
- Tailscale: hostname `osm-dev`, IP `100.102.154.32`, `--ssh` enabled
- DNS-rebind whitelist server: `127.0.0.1`, `localhost`, `[::1]`,
  `100.102.154.32`, `osm-dev`
- Handshake `initialize` từ cả `localhost:8765` và `100.102.154.32:8765`
  trả về `serverInfo: osm-mcp v1.27.0` ✅

## Kiến trúc thực tế (skip Docker)

```
[Máy dev]                           [Server osm-dev, WSL2]
  Tailscale client                    Tailscale 100.102.154.32
  VS Code + Remote-SSH  ─ tailnet ─>  sshd :22
                                      uv + python MCP :8765
                                      Postgres native :5432 (local only)
```

**Lý do bỏ Docker so với plan ban đầu:** máy server đã cài sẵn Postgres 16
+ pgvector native, WP-10 Dockerfile chưa wire CMD (chỉ placeholder print)
nên dùng Docker = thêm lớp phức tạp không thu thêm giá trị. Khi WP-10
đóng, quay lại Docker theo kế hoạch gốc.

## 2 code patch đã áp dụng (ghi vào `lessons.md` sau)

### Patch 1 — `tests/test_schema_diff.py:_normalize_constraints`

PG 16.4+ expose auto-generated NOT NULL check constraints với tên chứa
OID (`<schema_oid>_<table_oid>_<colnum>_not_null`). Các OID khác giữa
tenant schemas → test fail giả. Thêm regex filter drop các row này
trong `_normalize_constraints`; NOT NULL đã được verify qua
`is_nullable` ở `_dump_columns`.

### Patch 2 — `osm/server/app.py` thêm `--allowed-host` flag

FastMCP SDK mặc định whitelist DNS-rebinding protection chỉ
`127.0.0.1:*`, `localhost:*`, `[::1]:*`. Khi bind `0.0.0.0` cho
tailnet access, mọi Host header khác bị reject với `421 Misdirected
Request`. Thêm `--allowed-host` (repeatable) và env `OSM_ALLOWED_HOSTS`
(comma-separated) để mở rộng whitelist. Khi `--host` là non-loopback
hoặc có extras, construct `TransportSecuritySettings` với
`enable_dns_rebinding_protection=True` + `allowed_hosts` / `allowed_origins`.

## Lệnh bootstrap đã chạy (server)

```bash
# 1. uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Postgres provision (cần sudo 1 lần)
sudo -u postgres psql <<EOF
CREATE USER osm WITH PASSWORD '<random>';
CREATE DATABASE osm OWNER osm;
\c osm
CREATE EXTENSION IF NOT EXISTS vector;
GRANT ALL ON SCHEMA public TO osm;
EOF

# 3. .env với DATABASE_URL=postgresql://osm:<random>@127.0.0.1:5432/osm
# Password sinh ngẫu nhiên (openssl rand -base64 18 | tr -d '/+=' | head -c 24)
# Lưu .env chmod 600. Raw password cached tại /tmp/osm_pwd.txt (tạm).

# 4. Deps + verify
cd /home/son-odoo/git/odoo/17.0/odoo-semantic-mcp
export PATH="$HOME/.local/bin:$PATH"
uv sync --extra dev
export DATABASE_URL=$(grep ^DATABASE_URL .env | cut -d= -f2-)
uv run pytest -q                    # 217 passed, 10 skipped

# 5. Migrate + index
uv run python scripts/migrate.py --schema public
uv run python scripts/index.py \
  --addons tests/fixtures/odoo_ce_subset \
  --addons tests/fixtures/custom_addons \
  --tenant public --git-sha smoke-20260422

# 6. Tailscale (cần sudo 1 lần)
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up --ssh --hostname=osm-dev
tailscale ip -4                     # → 100.102.154.32

# 7. Start MCP native
export OSM_TENANT=public
nohup uv run python -m osm.server.app --http --host 0.0.0.0 --port 8765 \
  --allowed-host 100.102.154.32 --allowed-host osm-dev \
  > /tmp/osm-logs/server.log 2>&1 &
disown
```

## Bước tiếp — làm trên máy dev

### 1. Cài Tailscale trên máy dev + login cùng account

Login cùng Google account `truongson290893@` đã dùng lúc auth `osm-dev`
để 2 máy chung tailnet.

- **Linux**: `curl -fsSL https://tailscale.com/install.sh | sudo sh && sudo tailscale up --ssh`
- **macOS**: `brew install --cask tailscale`, mở app, sign in
- **Windows**: tải MSI tại https://tailscale.com/download/windows

### 2. Verify mạng từ máy dev

```bash
tailscale status                           # thấy dòng osm-dev 100.102.154.32
ping -c 2 100.102.154.32
curl -sS http://osm-dev:8765/mcp -X POST \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"dev-smoke","version":"1"}}}'
```

Phải thấy `event: message` + `"serverInfo":{"name":"osm-mcp"...}` →
mạng OK.

### 3. VS Code Remote-SSH (workflow chính)

1. Cài extension `ms-vscode-remote.remote-ssh` trong VS Code máy dev.
2. `Ctrl+Shift+P` → **Remote-SSH: Connect to Host...** →
   host: `son-odoo@osm-dev` (Tailscale SSH đã bật nên không cần
   cấu hình SSH key local).
3. Lần đầu VS Code cài `vscode-server` vào máy server (~30s).
4. **File > Open Folder** → `/home/son-odoo/git/odoo/17.0/odoo-semantic-mcp`.
5. Terminal VS Code giờ native trên server. Edit code, `uv run pytest`,
   git commit từ máy dev như local.

### 4. Dùng server như MCP endpoint từ AI client

Endpoint: `http://osm-dev:8765/mcp` (hoặc `http://100.102.154.32:8765/mcp`),
transport `streamable-http`, không cần auth (tailnet ACL là boundary).

**Claude Code (đã wire 2026-04-22)** — project scope vào workspace
`/home/soncrits/git/17.0/`:

```bash
claude mcp add --transport http --scope project osm http://osm-dev:8765/mcp
# ghi vào /home/soncrits/git/17.0/.mcp.json
claude mcp list    # verify: osm: http://osm-dev:8765/mcp (HTTP) - ✓ Connected
```

Remove khi cần: `claude mcp remove osm -s project`.

Tools lộ ra ở session mới: `mcp__osm__resolve_model`,
`mcp__osm__resolve_field`, `mcp__osm__resolve_method`. Session hiện tại
không pick up — phải restart Claude Code.

**Client khác (Cursor / Cline / Claude Desktop)** — cú pháp tuỳ client,
nhưng cùng endpoint + transport.

## Onboard dev mới join dự án

Dev mới clone repo về máy mình, code local, chạy test local. MCP server
trên `osm-dev` chỉ là **shared endpoint** để dev dùng tool osm từ Claude
Code khi làm việc với codebase Odoo 17.0 — không phải remote workspace
(không ai SSH vào osm-dev để code).

### Kiến trúc (nhìn từ dev mới)

```text
[Máy dev mới]                         [osm-dev, WSL2, shared]
  Tailscale client                      Tailscale 100.102.154.32
  git clone osm-mcp repo                Postgres + pgvector :5432 (local)
  uv sync + pytest (local)              MCP server :8765 (tailnet)
  Claude Code + tool osm  ─ tailnet ─> MCP :8765 + fixture đã index
```

Code chạy local. Chỉ MCP query đi qua tailnet tới osm-dev.

### Prereq máy dev mới

- OS: Ubuntu / macOS / WSL2 Ubuntu
- Python 3.10+
- Git
- `uv` (cài bằng `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Claude Code CLI (đã có sẵn trên máy dev)
- Tailscale (sẽ cài ở bước dưới)

### Thao tác của operator (người đang giữ osm-dev)

1. Lấy email Tailscale của dev mới.
2. Vào `https://login.tailscale.com/admin/machines` → chọn node
   `osm-dev` → **Share...** → nhập email dev mới → Send invite.
3. Verify server đang chạy bằng systemd (không phải `nohup`) — xem
   "Wrap systemd user service" trong section "Lệnh quản lý trên
   server" dưới. Bước này bắt buộc: `nohup` chết khi WSL reboot /
   logout, dev mới test nhầm lúc server down → confuse.
4. Gửi dev mới: git remote URL repo + message hướng dẫn dưới.

### Hướng dẫn gửi dev mới (copy-paste, chỉnh 2 placeholder)

```text
Repo: <GIT_REMOTE_URL>
Tailscale invite: mình sẽ share node osm-dev sang email bạn dùng
  cho Tailscale — báo mình email đó.

Setup ~10 phút, thứ tự:

1. Clone + cài deps (chạy local trên máy bạn):

   git clone <GIT_REMOTE_URL> osm-mcp
   cd osm-mcp
   curl -LsSf https://astral.sh/uv/install.sh | sh
   export PATH="$HOME/.local/bin:$PATH"
   uv sync --extra dev
   uv run pytest -q        # kỳ vọng pass 217+ tests (10 skip là drift
                           # path hardcode — không block)

2. Cài Tailscale + join tailnet:

   curl -fsSL https://tailscale.com/install.sh | sudo sh
   sudo tailscale up

   Login bằng Google/Microsoft/GitHub account sẵn có. Báo mình email
   dùng cho Tailscale, mình sẽ share node osm-dev sang. Bạn nhận mail
   invite, bấm accept.

   Verify: tailscale status → phải thấy dòng osm-dev 100.102.154.32

3. Wire MCP server osm vào Claude Code (user scope → dùng ở mọi
   project cần query Odoo 17):

   claude mcp add --transport http --scope user osm http://100.102.154.32:8765/mcp
   claude mcp list   # verify "osm: ... ✓ Connected"

4. Restart Claude Code, prompt smoke test:

   Dùng tool osm resolve_model res.partner, liệt kê override chain
   + indexed_at_sha.

   Resolve field name trên res.partner — module nào define, module
   nào override.

Nếu output có field list + indexed_at_sha → end-to-end OK, bắt đầu code.

Gỡ khi cần: claude mcp remove osm -s user + sudo tailscale down.
```

### Dev workflow sau khi setup

- Code local (edit trong repo clone).
- `uv run pytest -q` để test đổi code — không cần tailnet, không cần
  Postgres trên osm-dev (pytest dùng fixture + ephemeral schema local
  nếu cần DB).
- Muốn query MCP để research Odoo code khi code → tool osm trong
  Claude Code (query tới osm-dev qua tailnet).
- Push code qua git remote như bình thường.
- **Không** tự ý restart / reindex MCP server trên osm-dev — ping
  operator. Fixture indexed trên osm-dev là shared state.

### Checklist operator trước khi gửi hướng dẫn

- [ ] `<GIT_REMOTE_URL>` đã push, dev mới pull được
- [ ] Server đang chạy bằng systemd — `systemctl --user status osm-mcp`
- [ ] Handshake từ localhost pass — `curl http://localhost:8765/mcp ...`
- [ ] Node `osm-dev` ở trạng thái **Shared** trong admin console với
      email dev mới
- [ ] IP `100.102.154.32` verify còn đúng — `tailscale ip -4` trên
      osm-dev

### Troubleshooting thường gặp của dev mới

- `claude mcp list` báo **connection refused** → server die, operator
  chạy `systemctl --user restart osm-mcp` trên osm-dev.
- `tailscale status` không thấy `osm-dev` → dev mới chưa accept
  invite, hoặc invite hết hạn (re-send).
- MagicDNS cross-tailnet không resolve `osm-dev` → dùng IP trực tiếp
  (`100.102.154.32`) thay hostname. Thường IP chắc ăn hơn khi share
  node.
- Claude Code restart xong vẫn không thấy tool `mcp__osm__*` →
  `claude mcp list` kiểm tra connected chưa; nếu connected nhưng
  tool không load → exit Claude Code hẳn (không chỉ Ctrl+C) rồi
  mở lại.
- `uv run pytest` fail nhiều hơn ~10 skip → đọc output, thường do
  thiếu `.env` local (Postgres connection) hoặc deps chưa đồng bộ
  (`uv sync --extra dev` lại).

## Lệnh quản lý trên server (tham khảo nhanh)

```bash
# Xem log MCP
tail -f /tmp/osm-logs/server.log

# Restart server (giữ env cũ)
pkill -f 'osm.server.app'; sleep 2
cd /home/son-odoo/git/odoo/17.0/odoo-semantic-mcp
export PATH="$HOME/.local/bin:$PATH"
export DATABASE_URL=$(grep ^DATABASE_URL .env | cut -d= -f2-)
export OSM_TENANT=public
nohup uv run python -m osm.server.app --http --host 0.0.0.0 --port 8765 \
  --allowed-host 100.102.154.32 --allowed-host osm-dev \
  > /tmp/osm-logs/server.log 2>&1 &
disown

# Re-index sau khi đổi fixture
uv run python scripts/index.py \
  --addons tests/fixtures/odoo_ce_subset \
  --addons tests/fixtures/custom_addons \
  --tenant public --git-sha dev-$(date +%Y%m%d-%H%M%S)

# Chạy test suite
uv run pytest -q
```

### Wrap systemd user service (ổn định dài hạn — chuẩn bị cho reviewer)

`nohup &` chết khi WSL reboot / session logout. Reviewer test mà server
down = tệ. Wrap thành systemd user service, `loginctl enable-linger`
để chạy cả khi không login.

```bash
# trên osm-dev
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/osm-mcp.service <<'EOF'
[Unit]
Description=osm MCP server (Phase 1 demo)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/son-odoo/git/odoo/17.0/odoo-semantic-mcp
EnvironmentFile=/home/son-odoo/git/odoo/17.0/odoo-semantic-mcp/.env
Environment=OSM_TENANT=public
Environment=PATH=/home/son-odoo/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/son-odoo/.local/bin/uv run python -m osm.server.app --http --host 0.0.0.0 --port 8765 --allowed-host 100.102.154.32 --allowed-host osm-dev
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

# cần sudo 1 lần để user services chạy ngoài session login
sudo loginctl enable-linger son-odoo

# stop nohup cũ trước khi systemd bind cùng port
pkill -f 'osm.server.app'; sleep 2

systemctl --user daemon-reload
systemctl --user enable --now osm-mcp.service
systemctl --user status osm-mcp.service --no-pager
journalctl --user -u osm-mcp -n 30 --no-pager
```

Lệnh thường dùng sau khi wrap:

```bash
systemctl --user restart osm-mcp       # restart (thay cho pkill + nohup)
systemctl --user stop osm-mcp          # dừng
journalctl --user -u osm-mcp -f        # tail log (thay cho tail /tmp/osm-logs)
```

Lưu ý: sau khi enable systemd, block `nohup &` ở trên chỉ còn giá trị
lịch sử — **không chạy song song** cả hai (port 8765 sẽ conflict).

## Khi nào xoá file này

Xoá khi WP-10 đóng VÀ có 2 file chính thức sau:

- `docs/docker-quickstart.md` (self-host recipe)
- `docs/dev-workflow.md` (Tailscale + VS Code Remote hay tương đương)

Khi xoá, cũng bỏ dòng "scratch doc cleanup" trong `tasks/todo.md`
(mục Backlog).

## Outstanding drift / technical debt (để WP-10 dọn)

- [ ] `tests/indexer/test_python_parser_real.py` hardcode path
      `/home/soncrits/...` → 10 tests skip trên máy mới. Refactor thành
      env var hoặc pytest fixture trỏ `ODOO_SOURCE_PATH`.
- [ ] Patch `tests/test_schema_diff.py` (OID NOT NULL filter) cần review
      + commit chính thức; hiện là local change chưa commit.
- [ ] Patch `osm/server/app.py` (`--allowed-host`) cần unit test
      (`test_app_cli.py`) cho flag parsing + env `OSM_ALLOWED_HOSTS`.
- [ ] MCP server chưa wrap thành systemd service → reboot WSL phải start
      lại thủ công. Option: viết `~/.config/systemd/user/osm-mcp.service`
      (user-scope, `loginctl enable-linger`) hoặc wait WP-10 Docker.
- [ ] Raw DB password cache ở `/tmp/osm_pwd.txt` — xoá sau khi verify
      `.env` đọc OK. (Tmp sẽ clear khi reboot WSL dù sao.)
- [ ] `/home/soncrits/git/17.0/.mcp.json` đang reference hostname
      `osm-dev` (tailnet-local). Nếu workspace này về sau biến thành
      git repo / share công cộng, endpoint sẽ không resolve cho người
      clone. WP-10 nên cung cấp cách override qua env var hoặc
      per-user `.mcp.local.json`.
