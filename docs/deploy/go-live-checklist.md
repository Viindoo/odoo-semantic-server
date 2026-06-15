# Go-Live Ops Checklist (post-deploy, prod host)

Single ordered list of the **operator-run** actions that remain to put OSM fully into production.
Every item's code/script/runbook is already shipped — these are host-side actions only (the dev repo
cannot run them). Run in order; each links its detailed runbook. Tick the TASKS.md line when done.

> Prereqs: app + DB tiers deployed per [`deploy.md`](deploy.md); pre-launch signoff per
> [`pre-launch-checklist.md`](pre-launch-checklist.md) complete. Take a backup first (ADR-0018).

| # | Action | Command / runbook | Acceptance | Closes |
|---|--------|-------------------|-----------|--------|
| 1 (P0) | **RLS FORCE cutover** (multi-tenant isolation gate) | `sudo ops/rls_cutover.sh` — see [`runbooks/rls-cutover.md`](runbooks/rls-cutover.md). Creates `osm_reader` role, `FORCE ROW LEVEL SECURITY` on `embeddings`, flips MCP `:8002` PG_DSN to `osm_reader`. | Cross-tenant leak test (script step 5) PASS; MCP reads still work. | TASKS 1180 / 1187 |
| 2 | **Cleanup test artifact node** | `docker compose exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" -f /dev/stdin < ops/cleanup_snap_mod.cypher` | Returns `deleted=1` (first run) or `0` (already clean). | TASKS 374 |
| 3 | **Logrotate stanza fix** | `sudo cp docs/deploy/logrotate.d/odoo-semantic /etc/logrotate.d/odoo-semantic && sudo logrotate --debug /etc/logrotate.d/odoo-semantic` | `logrotate --debug` reports no permission error. | TASKS 579 |
| 4 | **Backup timer** (replace raw pg_dump cron) | `sudo systemctl enable --now odoo-semantic-backup.timer` then remove the old raw `pg_dump` cron.d entry. See [`disaster-recovery.md`](disaster-recovery.md). | `systemctl list-timers` shows the timer; one manual `python -m src.cli backup` produces a valid bundle. | TASKS 1230 |
| 5 | **Offsite backup** | Pick provider (Backblaze B2 recommended), then [`runbooks/offsite-backup-bootstrap.md`](runbooks/offsite-backup-bootstrap.md) (rclone crypt + `odoo-semantic-offsite-sync.timer`). | First sync uploads an encrypted bundle; restore-list verifies. | TASKS 1232 / 1246 |
| 6 | **SMTP env** | Set `SMTP_HOST/PORT/USER/PASSWORD/FROM` + `WAITLIST_NOTIFY_EMAIL` in prod `.env`, then `sudo systemctl restart odoo-semantic-webui.service`. | A waitlist/test email is delivered (not just logged). | TASKS 1243 |
| 7 | **Nginx rate-limit zones** | Apply `ops/nginx-ratelimit.conf.patch` per [`runbooks/nginx-ratelimit-apply.md`](runbooks/nginx-ratelimit-apply.md); `nginx -t && systemctl reload nginx`. | 4 zones active (mcp/api/waitlist/install); `nginx -t` OK. | TASKS 1244 |
| 8 | **Neo4j container recreate** | From canonical compose path, per [`runbooks/neo4j-container-recreate.md`](runbooks/neo4j-container-recreate.md). `NEO4J_dbms_security_auth__max__failed__attempts=10` already in `docker-compose.yml`. | `docker inspect` shows canonical WorkingDir; named volume preserved (no data loss). | TASKS 1245 |
| 9 | **Enable signup** | Set `SIGNUP_ENABLED=1` in prod `.env` (+ optional `GOOGLE_CLIENT_ID`/`GITHUB_CLIENT_ID`, `HCAPTCHA_SITE_KEY`/`HCAPTCHA_SECRET_KEY`), `sudo systemctl restart odoo-semantic-webui.service`. **Precondition: WS4b GDPR delete/export endpoints deployed** before opening public signup. | `/signup` reachable; a test account provisions a free key. | TASKS 1247 |

## Already resolved (live-verified 2026-06-14, no action)
- **Profile + core index gap v9-v19** (TASKS 370) and **internal profile 18.0** (TASKS 666): `list_available_versions`
  returns all 12 versions v8.0-v19.0; `list_available_profiles` returns 24 public+standard profiles +
  `viindoo_internal_17/18`.
- **Optional, upstream-blocked**: `viindoo_internal_19` (TASKS 671 / OBS-3) — create only once upstream cuts a 19.0
  branch on the required internal repo.

## Open evaluation (not blocking)
- **Neo4j BOLT thread-pool tuning** (TASKS 1231): if `/metrics` shows BOLT rejection under burst, add
  `NEO4J_dbms_connector_bolt_unsupported__thread__pool__queue__size` to `docker-compose.yml` then recreate (item 8).
