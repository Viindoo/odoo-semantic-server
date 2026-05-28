# nginx Rate-Limit Apply Runbook

> Apply 4 defence-in-depth nginx edge rate-limit zones on top of the existing
> application-layer limits. Reduces CPU during bot/crawler floods before requests
> ever reach Python. ADR-0011, ADR-0039.

---

## Nguyên Lý

PR #200 added plan-aware RPM gating at the `/mcp` app layer (FastMCP middleware,
per-API-key). PR #204 added per-IP flood protection at `/api/waitlist` (FastAPI,
trusting `TRUSTED_PROXY_CIDRS`). Neither layer stops requests that exhaust the
TCP/TLS handshake budget or overwhelm nginx workers before reaching the upstream
processes. These nginx zones close that gap — they fire before a single byte
reaches `:8002` or `:8003`.

**Three threat classes this addresses:**

1. **Unauthed floods** — `/install/` and `/api/waitlist` have no API-key
   requirement; a scraper can hammer them without ever authenticating.
2. **Slow Loris / slow-body attacks** — `limit_req` combined with existing
   `proxy_read_timeout` values throttles the connection-opening rate per IP.
3. **Authenticated-but-broken clients** — clients that forgot connection pooling
   and open N requests/second (not counted by plan quota, which counts MCP tool
   calls, not raw HTTP connections).

---

## Zone Catalogue

| Zone | Rate | Burst | Location(s) | SLAB |
|---|---|---|---|---|
| `mcp_edge` | 600r/m (10/s) | 20 nodelay | `/mcp` | 10m |
| `api_edge` | 300r/m (5/s) | 30 nodelay | `/api/` | 10m |
| `waitlist_edge` | 10r/m (~1/6s) | 5 nodelay | `/api/waitlist` | 5m |
| `install_edge` | 20r/m (~1/3s) | 10 nodelay | `/install/` | 5m |

All zones key on `$binary_remote_addr` (4 B IPv4 / 16 B IPv6 — minimal SLAB
footprint). `nodelay` on all zones: excess requests over burst are immediately
rejected with 429 rather than queued. `limit_req_status 429` (not 503) is
semantically correct and distinguishable in access logs from upstream errors.

---

## Preconditions

- Operator has `sudo` access on the production host.
- nginx is running: `systemctl is-active nginx` → `active`.
- No existing rate-limit in the vhost file (verified in step 1 below).
- `/etc/nginx/sites-available/odoo-semantic-mcp` exists and is the active
  vhost included in `http{}` via `/etc/nginx/sites-enabled/`.
- The diff patch `ops/nginx-ratelimit.conf.patch` is committed in the repo
  (created by W1C-2 in the parallel worktree). If applying manually, the
  anchor markers below are sufficient.

---

## Placeholder Reference

| Placeholder | Default | Note |
|---|---|---|
| `<VHOST_FILE>` | `/etc/nginx/sites-available/odoo-semantic-mcp` | nginx vhost for OSM |
| `<TS>` | `$(date +%Y%m%d-%H%M%S)` | Timestamp suffix for backup file |

---

## Procedure

### Step 1 — Pre-check (no existing rate-limit)

```bash
grep -rE 'limit_req(_zone)?\b' /etc/nginx/sites-available/odoo-semantic-mcp
```

Expected: **0 matches**. If any lines are returned, the patch has already been
applied or a conflicting rate-limit exists — do not re-apply. Investigate and
abort.

### Step 2 — Backup the current vhost

```bash
TS=$(date +%Y%m%d-%H%M%S)
sudo cp /etc/nginx/sites-available/odoo-semantic-mcp \
        /etc/nginx/sites-available/odoo-semantic-mcp.bak-${TS}
echo "Backup: /etc/nginx/sites-available/odoo-semantic-mcp.bak-${TS}"
```

Keep this backup until verification passes (Step 5).

### Step 3 — Apply the patch

**Option A (preferred): apply via patch file**

```bash
# From the repo root
sudo patch -p0 < ops/nginx-ratelimit.conf.patch
```

**Option B (manual): insert zone declarations at top of vhost**

Add these lines immediately after the opening comment block, before the first
`server {` block:

```nginx
# Phase 9A rate-limit zones — defence-in-depth edge layer.
# Primary enforcement is app-layer (PR #200 /mcp plan-aware RPM; PR #204 /api/waitlist per-IP).
# These zones protect against unauthed flood, slow Loris, and scrapers before they reach Python.
# All zones key on $binary_remote_addr (4 B IPv4 / 16 B IPv6 — minimal SLAB footprint).
limit_req_zone $binary_remote_addr zone=mcp_edge:10m      rate=600r/m;
limit_req_zone $binary_remote_addr zone=api_edge:10m      rate=300r/m;
limit_req_zone $binary_remote_addr zone=waitlist_edge:5m  rate=10r/m;
limit_req_zone $binary_remote_addr zone=install_edge:5m   rate=20r/m;
limit_req_status 429;
```

**NOTE:** `limit_req_zone` directives must live at `http{}` scope. This vhost
file is included inside `http{}` via `sites-enabled/` (verified by default
nginx.conf). Placing these lines before the first `server{}` block in this file
is valid. If your nginx.conf includes `sites-available` at a different scope,
move these 5 lines to `/etc/nginx/conf.d/osm-ratelimit-zones.conf` instead.

Then add `limit_req` directives inside each location block:

- `/api/waitlist` (add a separate `location` block before `/api/`, more
  specific prefix takes precedence):
  ```nginx
  location /api/waitlist {
      limit_req zone=waitlist_edge burst=5 nodelay;
      proxy_pass         http://127.0.0.1:8003;
      proxy_http_version 1.1;
      proxy_set_header   Host              $host;
      proxy_set_header   X-Real-IP         $remote_addr;
      proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
      proxy_set_header   X-Forwarded-Proto $scheme;
      proxy_read_timeout 30s;
      proxy_send_timeout 30s;
  }
  ```
- Inside `location /api/`:  add `limit_req zone=api_edge burst=30 nodelay;`
- Inside `location /mcp`:   add `limit_req zone=mcp_edge burst=20 nodelay;`
- Inside `location /install/`: add `limit_req zone=install_edge burst=10 nodelay;`

### Step 4 — Validate nginx config

```bash
sudo nginx -t
```

Expected output:
```
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
```

**If `nginx -t` FAILS:**

```bash
# Restore backup and re-test before aborting
sudo cp /etc/nginx/sites-available/odoo-semantic-mcp.bak-${TS} \
        /etc/nginx/sites-available/odoo-semantic-mcp
sudo nginx -t
sudo systemctl reload nginx
echo "ABORT — vhost restored to pre-patch state"
```

Do NOT proceed to Step 5 if `nginx -t` fails. Investigate the error, fix the
patch, and restart from Step 1.

### Step 5 — Reload nginx

```bash
sudo systemctl reload nginx
```

Reload (not restart) is sufficient — `limit_req_zone` changes take effect on
reload. Zero connection downtime.

---

## Verification

### 5a — waitlist rate-limit fires

Send 7 rapid requests to the fully unauthed waitlist endpoint:

```bash
for i in $(seq 1 7); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H 'Content-Type: application/json' \
    -d '{"email":"smoke@example.invalid"}' \
    https://odoo-semantic.viindoo.com/api/waitlist)
  echo "Request $i: HTTP $HTTP"
done
```

Expected: first 1–5 requests return 200 or 422 (FastAPI validation); request
\#6 or #7 returns 429 (burst=5 exhausted, rate=10r/m). Exact crossover depends
on burst drain speed.

### 5b — 429 count baseline in access log

```bash
grep ' 429 ' /var/log/nginx/access.log | wc -l
```

Record this baseline. Monitor again after 24h and 48h — the count should track
only legitimate rate-limit fires (no spike from legit traffic).

### 5c — MCP and API not affected by normal traffic

Run a normal MCP tool call and confirm 200:

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST https://odoo-semantic.viindoo.com/mcp \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{}}'
```

Expected: 200.

---

## Tuning

**Zone firing too aggressively (legit traffic getting 429):**
- Raise `burst` first — zero memory cost, absorbs spikes without changing
  steady-state rate.
- If steady-state is the problem, double the `rate` (e.g., `600r/m` → `1200r/m`).
- Monitor: `grep ' 429 ' /var/log/nginx/access.log | awk '{print $1}' | sort | uniq -c | sort -rn`

**Zone not firing when it should:**
- Reduce rate or lower burst. Check if attacking IPs share a CDN exit node (in
  which case `$binary_remote_addr` is the CDN IP — consider `real_ip_from` CDN
  CIDR + `$http_x_forwarded_for` zone as secondary).

**Memory sizing:** Each zone entry is ~64 bytes. `10m` ≈ 160,000 entries.
If `limit_req_zone could not be allocated` appears in `error.log`, increase the
SLAB size.

---

## Rollback

Remove all four zone declarations and all `limit_req` directives via the backup:

```bash
sudo cp /etc/nginx/sites-available/odoo-semantic-mcp.bak-${TS} \
        /etc/nginx/sites-available/odoo-semantic-mcp
sudo nginx -t && sudo systemctl reload nginx
echo "Rate-limit zones removed — vhost restored"
```

Alternatively, for a precise revert using the patch in reverse:

```bash
sudo patch -p0 -R < ops/nginx-ratelimit.conf.patch
sudo nginx -t && sudo systemctl reload nginx
```

---

## References

- **`ops/nginx-ratelimit.conf.patch`** — unified-diff patch with the 4 zones (created by W1C-2)
- **`src/web_ui/rate_limit.py`** — in-app per-IP rate-limit (PR #204, `/api/waitlist`)
- **`docs/adr/0011-web-ui-session-auth.md`** — session auth policy including rate-limit posture
- **`docs/adr/0039-commercialization-platform.md`** — plan-aware RPM quota at `/mcp` (PR #200)
- **nginx `ngx_http_limit_req_module` docs** — https://nginx.org/en/docs/http/ngx_http_limit_req_module.html
