# Changelog

All notable changes to Odoo Semantic MCP are documented here.

## [Unreleased] — Data completeness + resource RBAC + observability + backup (feat/osm-data-completeness-rbac)

7 tool output gaps (G1-G7) + timeout fix (T1) + resource RBAC hardening (R1/R2/R5) + Era1 comodel fix (C2) + Prometheus histogram (M10C) + Neo4j online backup (#13).
**Tool count stays 24** (no new tool signatures, no new params) — no odoo-mcp-client mirror PR needed.
No new Postgres migration. No reindex auto-triggered; OPS re-index/re-embed actions documented in runbook.

### Added

- **`src/mcp/metrics.py`** — Prometheus `embedder_batch_duration_seconds` histogram (M10C WI-D1). Registered at `GET /metrics` on MCP port `:8002` (public, no auth — mirrors `/health`). Buckets: `(0.1, 0.25, 0.5, 1.0, 1.5, 2.5, 5.0, 10.0, 30.0, 60.0)` s. Per-sub-batch observation inside `Qwen3Embedder.embed()`. Cross-process caveat: only query-embed calls in MCP process are visible (batch indexer runs in a separate OS process). `prometheus_client>=0.20` added to `pyproject.toml`.
- **`tests/test_metrics_endpoint.py`** — 9 unit + endpoint tests for Prometheus histogram.
- **`tests/test_resource_tenant_isolation.py`** — 17 parametrized tests for resource RBAC: model/field/method/module/view handlers return scoped data when tenant context is set; no cross-tenant content leak.
- **`tests/test_neo4j_online_backup_roundtrip.py`** — integration round-trip test (export + restore) using testcontainers Neo4j Community image. Marked `neo4j`.

### Changed

#### Tool output completeness (ADR-0023 hardening — G1-G7)

- **`impact_analysis`** — views/methods/super-methods capped at 20 (`LIST_PREVIEW_MAX_ITEMS`) with `├─`/`└─` tree connectors + `... and N more` disclosure. Dependent-modules capped at 30 (`IMPACT_MODULES_MAX`, new constant) with "run with `profile_name=<p>` to scope" hint. Risk score computed from full count (not capped). (`src/mcp/server.py` G1)
- **`find_examples` / `find_style_override`** — adds ANN disclosure line: "showing N of M semantic candidates — increase `limit`" when `limit < ANN_LIMIT`; "ANN capped at 20 candidates" when `limit >= ANN_LIMIT`. (`src/mcp/server.py` G2)
- **`find_deprecated_usage`** — overflow message shows "showing N of M+ hits" (lower-bound total) + kind-filter hint. No new `start_index` parameter (avoids client mirror; full pagination deferred). (`src/mcp/server.py` G3)
- **`_resolve_method` override chain** — capped at 20 with `├─`/`└─` connectors + `... and N more` disclosure + `entity_lookup(method='…')` escape-hatch hint. (`src/mcp/server.py` G4)
- **`odoo://stylesheet` resource** — truncated at `STYLESHEET_RESOURCE_MAX_BYTES = 131_072` (128 KB); `# [truncated at 128 KB — full file: {N} bytes]` prepended. (`src/mcp/resources.py` G5)
- **`describe_module`** — adds `Next: module_inspect(method='dependencies')` hint when depends list > 20 entries. (`src/mcp/server.py` G6)
- **`suggest_pattern`** — adds `odoo://{version}/pattern/{id}` URI escape-hatch in snippet footer. (`src/mcp/server.py` G7)

#### Timeout fix (T1)

- **`setup_indexes()`** — new `CREATE INDEX IF NOT EXISTS FOR (n:Method) ON (n.model, n.odoo_version)` — resolves partial-scan timeout on `model_inspect`/`module_inspect`/`describe_module` for models with 50+ extending modules (e.g. `sale.order`). OPS: admin must re-run `python -m src.cli index --setup-indexes` on prod to create the index on existing data. (`src/indexer/writer_neo4j.py` T1)

#### Resource RBAC hardening (R1/R2)

- **Resource cache key** — gains `::t{tenant_id}` suffix (Option A): admin key → `::t_admin`, tenant key → `::t{id}`. Prevents cross-tenant cache pollution ahead of private-tenant indexing. Pattern + stylesheet handlers exempt (already globally scoped or use `_scope_pred`). (`src/mcp/resources.py` R1)
- **`resources_index` scope filter** — `_fetch_top_models` and `_fetch_indexed_versions` now use `_scope_pred` — discovery URIs are tenant-scoped; avoids over-inclusive `resources/list` response. (`src/mcp/resources_index.py` R2)
- **Cross-process scope cache invalidation** — DEFERRED (R3): staleness bounded at 60s TTL; Redis/PG-NOTIFY deferred to M14+.

#### Era1 comodel fix (C2)

- **`parser_python.py` `_extract_columns_dict_fields()`** — now extracts `comodel_name` for Many2one/One2many/Many2many from AST-parseable v8/v9 files (positional arg or `comodel_name` kwarg). Previously only the text-regex fallback path did this. Fixes `resolve_orm_chain` on v8/v9 AST-path modules. 2 regression tests added. OPS: re-index v8/v9 `--full` required. (`src/indexer/parser_python.py` C2)

#### Neo4j online backup (ADR-0018 update — WI-D2)

- **`src/cli.py`** — `backup` command now exports Neo4j via Bolt driver streaming (`MATCH (n) RETURN …` → CREATE + MATCH/MERGE relationship statements). Bundle contains `neo4j.cypher` (text, online) instead of `neo4j.dump` (binary, offline). Neo4j stays running during backup. Zero new server-side deps (uses existing `neo4j` Python package; no APOC, no Enterprise). `restore` auto-detects `neo4j.cypher` vs legacy `neo4j.dump` (prints manual-restore note for old bundles). Neo4j restore failure is non-fatal (postgres.sql already restored; Neo4j can be rebuilt via reindex).
- **`docs/adr/0018-backup-contract.md`** — updated contract (neo4j.dump → neo4j.cypher), rationale, restore prerequisites, consequences. (`src/cli.py`, `docs/adr/0018-backup-contract.md`)

### OPS — admin actions required on production (code done, not yet run)

See `docs/deploy/reindex-v8-v19-runbook.md §Post-PR Wave (feat/osm-data-completeness-rbac)` for the full checklist. Summary:

1. **Re-run `setup_indexes()`** — creates `Method(model, odoo_version)` index (T1 timeout fix).
2. **Re-index v8/v9 `--full`** — materializes `comodel_name` on Field nodes (Era1 C2 fix).
3. **Re-embed v9.0** — `find_examples` v9 returns empty; suspected partial re-embed on prod.
4. **M13 close OPS (pre-existing):** `ops/cleanup_absolute_path_nodes.cypher`, RLS FORCE cutover (`osm_reader` role + DSN split), FERNET credstore cut — see runbook §5.14.

---

## [Unreleased] — Web-UI multi-tenant RBAC + self-service portal (W0-W4)

Batch 5 PRs (#174/#177/#179/#180/#181). **DOCS-ONLY wave này (W5).** Tool count stays **24**. Một Postgres migration mới (`m13_005_tenant_members.sql`) — admin phải chạy `python -m src.db.migrate` trước khi deploy. Không cần reindex.

### Added — WI-7 FERNET credstore cut (feat/wi7-fernet-credstore-cut)

- **[WI-7] FERNET key delivered via systemd credential store (webui+backup `LoadCredential`,
  CLI via `osm-fernet-run`); removed from `.env`/`webui.env`. RLS enforcement still pending.**
  - `docs/deploy/odoo-semantic-webui.service` — `LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY`
    now active (replaces the commented-out line from #185). Key lives root:root 0600 at
    `/etc/credstore/FERNET_KEY`; PREREQUISITE: provision before enabling the unit
    (missing source = 243/CREDENTIALS hard-fail, NOT a soft fallback).
  - `docs/deploy/odoo-semantic-backup.service` — same `LoadCredential=` added; backup bundle
    (`fernet.enc`, ADR-0018) now sourced from credstore.
  - `docs/deploy/osm-fernet-run` (new, mode 0755) — `systemd-run -p LoadCredential=` wrapper
    for ad-hoc CLI (indexer/rotate-fernet/restore); closes the CLI delivery gap; must run as root.
  - `docs/adr/0020-fernet-key-delivery.md` — §5 and §6 updated: holistic cut realized;
    "zero net hardening / commented out" caveat resolved; 243/CREDENTIALS hard-fail warning retained;
    `$FERNET_KEY` env fallback for dev/non-systemd preserved.
  - `docs/deploy.md §12` Option B — updated to final design: provision credstore with EXISTING
    key, strict ordering, CLI via wrapper, 24.04+26.04 compatibility.
  - `docs/deploy/install-runbook.md` — REQUIRED credstore-provision step added before
    `systemctl enable --now` of webui/backup units.
  - `docs/deploy/reindex-v8-v19-runbook.md §FERNET cutover` — updated from "commented out /
    provision before uncommenting" to "LoadCredential now active; provision credstore as prerequisite".
  - `docs/deploy/backup-runbook.md` — FERNET delivery section added; ad-hoc CLI via `osm-fernet-run`.
  - `TASKS.md WI-7` — FERNET credstore sub-items marked `[x]` DONE; RLS sub-items remain `[ ]` pending.
  - Prod unaffected until the /tmp ops scripts (credstore provision + restart sequence) run.
  - RLS enforcement (`osm_reader`, `FORCE ROW LEVEL SECURITY`, DSN switch) explicitly OUT of
    scope for this PR — separate effort requiring prior code changes.

### Fixed — webui unit LoadCredential decoupled (#185)

- **`docs/deploy/odoo-semantic-webui.service`** — commented out
  `LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY` (was added in #173, caused
  status=243/CREDENTIALS on prod where `/etc/credstore/FERNET_KEY` does not yet exist).
  Root cause: systemd `LoadCredential` with a missing source is a **hard fail**, not a
  soft fallback to `EnvironmentFile=`. Additionally, `src/cli.py` (indexer +
  `rotate-fernet`) reads FERNET_KEY from env/`.env` only (no credential access), so
  a webui-only LoadCredential provides zero net hardening while risking a boot failure.
  The holistic WI-7 OPS cut (credstore + CLI coverage + `.env` removal) is the correct
  path; env delivery is the uniform source until then. No code change; unit template + docs only.

### W0 (#174) — Admin gate + SIGNUP_ENABLED

#### Added
- **`SIGNUP_ENABLED` config flag** (`src/web_ui/config.py`) — default `False` (invite-only). Đọc từ env var `SIGNUP_ENABLED=1` hoặc INI `[webui] signup_enabled = true`. Khi `False`, `POST /api/auth/register` và OAuth new-account path trả 403. Xem `docs/deploy.md §Auth - SIGNUP_ENABLED`.
- **`Depends(require_admin)` áp lên 19 route mutating** — repos, ssh_keys, operations, jobs. Route `restore` giữ `require_admin_with_fresh_mfa`. Self-service routes (api_keys/totp/feedback) giữ ownership-scope.

### W1 (#177) — Tenant membership + admin tenant CRUD (ADR-0038)

#### Added (migration required)
- **`migrations/m13_005_tenant_members.sql`** — 3-part migration:
  - `tenant_members(user_id, tenant_id, role, created_at)` M:N join table; `PRIMARY KEY (user_id, tenant_id)`.
  - `ALTER TABLE webui_users ALTER COLUMN password_hash DROP NOT NULL` — đóng issue #176 (OAuth-only users đã INSERT NULL trên prod).
  - `CHECK (profiles.name NOT LIKE '%,%')` — GUC-delimiter guard ngăn profile name chứa dấu phẩy, bảo vệ RLS `string_to_array` (ADR-0034 A4).
- **`resolve_tenant_scope_web(request)` / `ALL_TENANTS` / `is_in_scope`** trong `src/web_ui/auth.py` — write-side scope helper (admin = `ALL_TENANTS` sentinel; non-admin = set of tenant_id from `tenant_members`).
- **`routes/tenants.py`** — admin-only tenant/member/resource CRUD: `GET/POST /api/tenants`, `DELETE /api/tenants/{id}` (409 nếu còn resources), `GET/POST/DELETE /api/tenants/{id}/members`.
- **Astro page `/admin/tenants`** — quản lý tenant + thành viên (admin-only).
- **Membership model (b)** — user đa-tenant (consultant/agency persona). Active-tenant = **Option A** (explicit `tenant_id` trong request body, stateless, auditable).

#### Notes
- `#175` (audit coverage) đã FOLD vào W3; `#176` (password_hash nullable) đã FOLD vào W1 m13_005. Cả hai CLOSED.
- ADR-0038 `docs/adr/0038-tenant-rbac-web-ui-write-side.md` committed.

### W2 (#179) — Customer self-service portal

#### Added
- **`tenant_write_allowed(scope, tenant_id)`** trong `src/web_ui/auth.py` — write-side guard STRICTER than `is_in_scope`: `tenant_id IS NULL` (shared) → admin-only write; non-admin chỉ write vào tenant của mình.
- **`GET /api/repos/profiles` tenant-filtered** — non-admin chỉ thấy profile trong scope (`is_in_scope`) + shared; `tenant_id` field có trong mỗi profile/repo response.
- **4 route repo mở cho non-admin với tenant scope:**
  - `POST /api/repos/repos` — thêm repo vào tenant-owned profile
  - `PATCH /api/repos/repos/{id}` — cập nhật repo metadata trong scope
  - `DELETE /api/repos/repos/{id}` — xóa repo trong scope
  - `POST /api/repos/repos/{id}/index` — trigger index cho repo trong scope
- **`GET /api/account/tenants`** (`routes/account.py`) — trả danh sách tenant của session user kèm `role` (portal header).
- **Astro page `/account/repos`** — customer self-service repo management.

#### Notes (ADR-0038 D9-D13)
- Admin-only routes (profile CRUD, bulk ops, tenant CRUD, SSH keys, operations) KHÔNG thay đổi từ W0/W1.
- **SSH key cho non-admin (ADR-0038 D13):** non-admin quản lý repo SSH KHÔNG chọn key — server resolve key access dùng chung (`key_type='access_key'`, lấy row đầu theo id); client-supplied `ssh_key_id` của non-admin bị bỏ qua. Áp dụng cho **cả `POST add_repo` lẫn `PATCH update_repo`**: trên PATCH, `ssh_key_id`/`clear_ssh_key` của non-admin bị bỏ qua (giữ nguyên key hiện có; chỉ resolve shared key khi URL chuyển sang SSH mà repo chưa có key) — đóng lỗ chọn key chéo-tenant trên đường PATCH (code review PR #183). Admin vẫn chọn key từ dropdown trên cả hai route. Portal `/account/repos` hiển thị hướng dẫn: user tự thêm public key (admin công bố) vào git host của mình.

### W3 (#180) — Diagnostics + admin user creation + audit coverage

#### Added
- **`GET /api/operations/diagnose`** — delegate sang `src/diagnostics.py` (SSOT dùng chung với CLI `diagnose` subcommand). Trả trạng thái Postgres, Neo4j, Ollama, FERNET_KEY, config.
- **`src/diagnostics.py`** — module SSOT, tách khỏi `cli.py`.
- **`POST /api/admin/users`** (`routes/admin_users.py`) — admin tạo user mới với temp-pass hoặc invite link (one-time).
- **`GET /api/admin/audit-log`** — paginated + filterable audit log viewer (admin-only).
- **Trang `/admin/audit-log`** (Astro SSR).
- **`@audit_action` mở rộng** — bổ sung cho: `operations.index_all`, `jobs.reset`, `user.deactivate`, `user.reactivate`, `user.reset_password_link` (5 action mới).
- **Regression guard `enumerate-app`** — test kiểm tra mọi route mutating (HTTP method != GET) gắn với admin phải có `__audit_action__` marker; fail khi thêm route mới mà quên audit.

#### Changed
- ADR-0021 taxonomy cập nhật với 5 action mới.
- **BREAKING (CLI `osm diagnose --json`):** schema thống nhất theo SSOT `src/diagnostics.py` — mỗi check đổi key `"check"` → `"name"` và trạng thái lỗi `"status": "fail"` → `"status": "error"` (giá trị hợp lệ nay là `ok`/`error`/`skipped`), kèm trường `"overall": "ok"|"degraded"`. HTTP `GET /api/operations/diagnose` dùng cùng schema. Pipeline cron/alert nào parse output `--json` cũ (`check`/`fail`) cần cập nhật key.

### W4 (#181) — Data-driven version list + worker controls

#### Added
- **`GET /api/versions`** (`routes/versions.py`) — đọc `src/indexer/spec_data/bootstrap_versions.json` (12 phiên bản v8-v19), sort numeric, trả `{"versions": ["8.0", ..., "19.0"]}`. Dùng cho các dropdown version trong Admin UI.
- **3 dropdown version trong Admin UI** — index-core, seed-patterns (thêm option 'all'), add-repo (populate từ `GET /api/versions`).
- **Worker controls trong index-all:** `profile_workers` (1-4, parallel profiles) + `max_workers` (1-8, parallel repos per profile) + `--gc` flag (cleanup stale Module nodes).
- **Branch hint** trong form add-repo — chọn version ở dropdown tự pre-fill ô branch input (ví dụ chọn `17.0` → branch input điền sẵn `17.0`); user vẫn sửa được.

---

## [Unreleased] — WI-7 FERNET hardening + RLS armed-but-dormant + Path portability (ADR-0037)

### WI-7 — FERNET secrets hardening (M13)

**Security / breaking change.** No reindex required.

#### Changed
- **Central FERNET key getter (`src/crypto.py`)** — new `get_fernet_key()` /
  `get_fernet()` with two-source resolution: `$CREDENTIALS_DIRECTORY/FERNET_KEY`
  (systemd `LoadCredential`, preferred) → `$FERNET_KEY` env var (backward-compatible
  fallback). All five call sites refactored to use the central getter.
- **`rotate-fernet` now covers `totp_secrets`** — `totp_secrets.secret_encrypted`
  is re-encrypted in the same atomic transaction as `ssh_key_pairs.private_key_encrypted`.
  `row_count` in `key_rotation_log` = ssh_rows + totp_rows. If any row in either
  table fails to decrypt → rollback all.

#### Removed (breaking)
- **`--old-key` / `--new-key` CLI flags** removed from `rotate-fernet` sub-command.
  These flags were deprecated in M9 (ADR-0020 F13) and promised removal in M10.
  **Migration:** use `--old-key-env OLD_FERNET_KEY --new-key-env NEW_FERNET_KEY`
  (already the default) or set env vars directly.

#### Docs
- ADR-0020 updated: WI-7 findings, central getter, LoadCredential delivery,
  extended rotation atomicity, Consequences section.
- `docs/deploy.md` §12: LoadCredential OPS cutover steps + rotation flow update.

### WI-7 — RLS policy armed-but-dormant (M13, migration m13_004)

**Security / defense-in-depth.** No reindex required. Tool count stays **24**.

#### Added
- **`migrations/m13_004_embeddings_rls.sql`** — `ALTER TABLE embeddings ENABLE ROW LEVEL SECURITY`
  + `CREATE POLICY embeddings_tenant` dùng GUC `app.allowed_profiles` (sentinels: `'*'` = admin,
  `IS NULL` = shared, `= ANY(string_to_array(...))` = tenant). Policy wired vào read path MCP tier
  qua `SET LOCAL app.allowed_profiles` per request (code trong `src/mcp/server.py`).
- **`docs/deploy/odoo-semantic-webui.service`** — `LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY`
  initially added (#173), then **commented out** (#185): a missing `/etc/credstore/FERNET_KEY`
  hard-fails the unit at status=243/CREDENTIALS (NOT a soft fallback); `src/cli.py` (indexer +
  `rotate-fernet`) also needs FERNET_KEY via env and has no systemd credential access — env
  delivery is the uniform source for all consumers until WI-7 holistic OPS cut. The shipped
  template uses `EnvironmentFile=` only; LoadCredential will be uncommented at cut time.

#### Behaviour note
Migration này là **no-op trên production cho đến khi OPS chạy runbook §5.14**: app connect
bằng owner role (`odoo_semantic`), `ENABLE` không `FORCE` = owner bypass = policy không có
hiệu lực. Read-guard thực sự vẫn là SQL `AND profile_name = ANY(%s)` (WI-4, shipped v0.10.0).
`FORCE ROW LEVEL SECURITY` + non-owner read role `osm_reader` + tách read-DSN của MCP tier
là các bước OPS thủ công (reindex runbook §5.14), KHÔNG chạy tự động.

#### Docs
- ADR-0034 Amendment A4: giải thích partial landing, known-constraint GUC delimiter,
  quan hệ với A2.
- Reindex runbook §5.14: hướng dẫn FORCE + role + DSN-split + verify + rollback.
- `m13_001` comment cập nhật: trỏ đúng sang m13_004 thay vì "deferred to a later migration".

---

Single PR. File paths are now **repo-relative everywhere** instead of server-absolute,
so an AI client on a different machine can map them onto its own checkout, and moving
the server to a new host no longer requires a reindex. Tool surface stays **24**.
Requires a full reindex v8→v19 after deploy + post-reindex cleanup (see below).

### Changed
- **Stored paths are repo-relative** (`addons/sale/models/sale_order.py`), not absolute.
  `repos.local_path` is the single absolute anchor. Relativization happens at the writer
  boundary via a transient `ModuleInfo.repo_root` (set in `build_registry`): `Module.path`,
  `OWLComp/JSPatch.file_path`, `Stylesheet.file_path` + `@import` targets (writer_neo4j),
  and `embeddings.file_path` for method/field/view/qweb/js + css/scss/less (writer_pgvector).
- **CoreSymbol / CLICommand** relativize against the Odoo source root in their parser
  (`odoo/orm/models.py`, `odoo/cli/server.py`) — they have no `repos` anchor.
- **8 MCP render sites** emit repo-relative paths via the new `_portable_path()` helper
  (find_examples, lookup_core_api, describe_module, module_inspect JS, resolve_stylesheet,
  find_style_override, + import/override chains). Idempotent → permanent safety-net for
  any legacy absolute row even before the reindex lands.
- **Repo identity is the portable git URL, not the server dirname.** Every `[repo]` label
  and the `describe_module` repo line now show `repo_url` (e.g. `github.com/odoo/odoo`)
  instead of the host checkout dirname (`odoo_17.0`) — the dirname is server-detail an AI
  client can't use. Neo4j-sourced tools coalesce `repo_url`→`repo` in-query (zero render
  edits); `find_examples` resolves `repo_id`→url at render (cached); dirname remains a
  fallback only when no URL is known. (PR review — AI-client lens.)
- **Server migration is now a `local_path` re-point, no reindex**: the `odoo://stylesheet`
  resource reconstructs the absolute on-disk path dynamically from `repos.local_path`
  (`resources.py`), and the DR runbook documents the re-point + cache-clear procedure.

### Fixed
- **Provenance gap**: css/scss/less embedding chunks now carry `repo` + `repo_id`
  (previously only `module` + `odoo_version`), so dropping the absolute path loses no
  identifying information.
- **GC alignment**: `live_paths` is relativized to match the relative `Module.path` —
  prevents the catastrophic case where every module looks stale and gets deleted.

### Ops
- Full reindex v8→v19 required. **After** it completes, run
  `ops/cleanup_absolute_path_nodes.cypher` to drop stale absolute-keyed Stylesheet /
  LintViolation nodes (their `file_path` is a MERGE-key component). Verify Neo4j +
  `embeddings WHERE file_path LIKE '/%'` are 0. See reindex runbook §3b.

### Docs
- ADR-0037 (path portability); reindex runbook §3b; disaster-recovery §Migration to New Host.

## [0.11.1] — 2026-05-23 — Pre-LIVE hygiene (read-side; no reindex)

Small follow-up after #165 (v0.11.0). **Read-side only** — no parser/writer change, no
new migration, **no reindex required**. Tool surface stays **24**.

### Removed
- **`scripts/cleanup_v96.cypher` + `tests/test_no_v96_data.py`** (stale one-shot relics).
  The script was an unguarded, label-blind `DETACH DELETE n WHERE n.odoo_version='96.0'`
  with zero operational wiring; `96.0` is now an active test-sentinel version (the 94-99
  band, alongside `TEST_VERSION=99.0`), so the guard test was a false-positive generator
  (it asserted 0 nodes at `96.0` on a DB where sibling tests legitimately seed `96.0`).
  The runbook §1a `snap_mod`-scoped cleanup (name+version pinned) supersedes the script,
  and the mandatory full reindex v8→v19 rebuilds the graph regardless.

### Documented (no behaviour change)
- **R-1 — `describe_module` depends-list intentionally unscoped** (ADR-0034 T7): code
  comments at `_describe_module` + `_describe_module_structured` explain why the manifest
  depends list returns names with no `_scope_pred("d")` — the asymmetry with the
  content-returning `_module_dep_closure` is by design (the list returns only names from
  the caller's own scoped manifest; the closure returns `dep.repo`/`repo_url` and so must
  filter). Confirmed not-a-leak; documented to prevent re-flagging.
- **Public-share semantics + future direction** (ADR-0034 T6): the binary
  `tenant_id IS NULL` = shared model is the launch design; re-classification is a
  read-side `tenant_id` flip (no reindex); per-repo / per-tenant publishing is a deferred
  product feature, **not** a gate for going multi-tenant LIVE. Runbook §5.12c cross-refs.
- **MED-3 — cross-tenant over-eager re-index** (reindex runbook Known Constraints):
  `find_dependent_repos` + basename-collision can NULL another tenant's `head_sha`
  (integrity/cost, **not** a confidentiality leak); accepted at current scale, revisit
  before scaling tenant count (ADR-0007 W14, ADR-0034 A3).

## [0.11.0] — 2026-05-23 — Parser correctness v8-v19, arch_snippet, tenant isolation, query/render, enrichment (WG-1..WG-5)

Six work-groups landed on `feat/osm-final-stretch` via the fix-wave integration branch.
Tool surface stays **24**. No new Postgres migrations. Requires a full reindex v8→v19
after deploy (see runbook §5.11-5.12 for the new pre-traffic multi-tenant gate).

### Added / Fixed — WG-1: Python parser correctness (v8-v19)
- **v9 Py2-syntax fallback**: `ast.parse` failure on Python-2-only tokens (`<>`, etc.)
  now falls back to `_parse_era1_text()` regex for both `_columns` AND `fields.X` new-API
  fields — prevents `account.py` losing 82 fields on v9 reindex.
- **`Many2oneReference` + `PropertiesDefinition` + `property` field types**: added to
  `FIELD_TYPES` (v13+ `Many2oneReference`; v16+ `PropertiesDefinition`;
  v8/v9 legacy `fields.property`). Previously caused silent Field node drops.
- **F-14 Selection positional guard**: `fields.Selection('_compute_sel')` positional
  string no longer stored as `string=` label.

### Added / Fixed — WG-2: JS parser + query.py path + NewId (v8-v19)
- **OWLComp dual-dispatch (JS-G1)**: `parser_js.py` era2 files for major>=14 now also
  call `_extract_era3_components()` — fixes 0 OWLComp for v14 (96 files), v15 (41), v16 (18).
- **JSPatch member-expr (JS-G2)**: `MyClass.patch("key", fn)` pattern now matched for
  major>=14 era2 extractor — fixes 0 JSPatch for v14-v16.
- **`odoo/osv/query.py` version-aware path (CORE-Q)**: `_resolve_core_paths` maps
  `odoo/tools/query.py` logical path to `openerp/osv/query.py` (v8/v9) or
  `odoo/osv/query.py` (v10-v15) — `class Query` now indexed for all 8 versions.
- **NewId `_V19_CURATED_FILES` entry (V19-G5)**: `odoo/orm/identifiers.py` added so
  `api_version_diff("NewId", 18, 19)` returns moved-not-removed.

### Added / Fixed — WG-3w: writer schema correctness (F-5, F-13, F-8, F-12, arch_snippet, V16-G2)
- **arch_snippet on View nodes**: ~20-30 line excerpt of `<arch>` stored at index time;
  surfaces in `resolve_view` and `model_inspect` output so agents see base view structure.
- **F-5 XML comment-led arch**: `parser_xml.py` skips comment nodes when detecting
  `view_type` from first child — prevents 'form' fallback on comment-led `<arch>`.
- **F-13 USES_FIELD module scoping**: MATCH key includes `module` — eliminates fan-out
  where one `self.X` ref matched Field nodes in every module with that field name.
  Known limitation: cross-module USES_FIELD edges are not generated (same-module-only
  is a precision-over-recall trade-off; see ADR-0034 T5).
- **F-8 USES_FIELD/DEPENDS_ON_FIELD batched tx**: UNWIND batch per method eliminates N+1
  transactions at reindex.
- **F-12 Module MERGE ON MATCH coalesce**: `coalesce($repo_url, m.repo_url)` prevents
  a second-pass write of `repo_url=None` overwriting existing value in multi-repo pool.
- **V16-G2 JSPatch entity_name**: chunk `entity_name` uses patch target class, not
  patch name key, for better semantic search quality.

### Added / Fixed — WG-3t: multi-tenant choke-point (13 leak sites, RELEASE GATE)
- **13 confirmed leak sites closed** (`server.py` + `orm.py` + resources.py) via
  the `_scope` helper + uniform `($allowed IS NULL OR all(...))` guard fragment;
  `profile_name` narrowing is now non-escalating and applied consistently to both
  Neo4j and pgvector paths (eliminates split-brain — see ADR-0034 T2).
- **`tests/test_cross_tenant_isolation.py`** extended to cover all 13 paths (style
  override/resolve, lint xml, api_version_diff, set_active_version probe,
  validate_relation, resolve_view parent, structured variant). Gate must be red when
  any site leaks.

### Added / Fixed — WG-4: query/render correctness
- **F-4 load order** (`_module_dep_closure`): `ORDER BY min_depth DESC` (deepest =
  highest depth number = install first); comment corrected.
- **`<list>` vs `<tree>` view type** (v18+ rename): queries filter
  `v.type IN ['tree','list']` and normalize for render — fixes 0 v18 list views
  returned by `model_inspect` / `find_override_point`.
- **file:line breadcrumb**: `line_start` / `file_path` projected in `find_examples`
  and `model_inspect` render — agents now see source location without a separate lookup.

### Added / Fixed — WG-5: cheap enrichment
- **Edition derive**: `Module.license` → `edition` tag (`CE` / `Odoo EE` /
  `Viindoo EE`) surfaced in `check_module_exists` and `model_inspect` output.
- **Module.summary / description** surfaced in `describe_module` output.
- **OWL field-widget pattern** (`fieldRegistry.add`) added to `patterns.json`.

### Changed — docs / data (this PR, WG-6)
- **`bootstrap_versions.json`**: corrected Bootstrap version + preprocessor for all
  12 versions (v8-v19). Key corrections: v8 BS 3.2.0 (was `3.x`); v9-v11 BS 3.3.5 +
  LESS (v11 was wrong BS-major "4" + SCSS); v12 BS 4.1.3 (was `4.1`); v14 BS 4.3.1
  (was `4.4`); v15 BS 4.3.1 NOT 5 (was `5.1`); v16 BS 5.1.3 (was `5.1`);
  v18/v19 BS 5.3.3 (was `5.3`). `preprocessor` field added; LESS entry-point paths
  corrected for v8-v11. Evidence: source-verified per v*-ground-truth.md S10.
- **ADR-0034**: tenant model clarification amendment (T1-T5) — shared vs own profiles,
  choke-point invariant, cross-process cache 60s constraint, `profile=[]` pre-reindex
  gate, USES_FIELD same-module-only known limitation.
- **ADR-0005**: v10 `__openerp__.py`-only known-miss documented (3 modules:
  l10n_fr_sale_closing, account_cash_basis_base_account, l10n_fr_pos_cert) — Keep
  Simple decision; DualManifestFinder deferred.
- **Reindex runbook**: new §5.11 (multi-tenant pre-traffic gate: profile=[], edition,
  OWLComp/JSPatch v14-v16, Query CoreSymbol, NewId, arch_snippet, cross-tenant leak
  test) + §5.12 (tenant API key ops); 12 new checklist rows.

### Notes
- v18 status: indexer-ready (parser, schema, tools all handle v18). OBS-1 note in
  README updated — the "pending" was only because the v18 repo was not on disk at the
  time of the original note; v18 indexing is fully supported.

## [0.10.0] — 2026-05-23 — Final-stretch: pre-reindex enrichment + agent-convenient output + multi-tenant enforcement gate

One PR (`feat/osm-final-stretch`). Tool surface stays **24** (the module-dependency
capability is a `module_inspect(method='dependencies')` kind, not a new tool). One
Postgres migration (`m13_003`). **OPS follow-up (admin):** after deploy, run the full
reindex v8→v19 — Group A adds new graph/embedding data that is populated only on
re-index. The cross-tenant leak test is the release gate.

### Added — Group A (reindex-forcing graph/embedding enrichment)
- **v19 split-ORM core coverage (A1)** — `parser_odoo_core` resolves the v19 `odoo/orm/`
  package: the `Command` enum keeps its v18 qname `odoo.fields.Command` (via
  `orm/commands.py`, so `api_version_diff` sees a moved file, not a remove+add), plus a
  curated v19 allow-list (`_V19_CURATED_FILES`) for `Domain`/`DomainAnd`/`DomainOr`
  (`orm/domains.py`) and `TableObject`/`Constraint`/`Index`/`UniqueIndex`
  (`orm/table_objects.py`). ~48 internal domain helpers excluded.
- **Neo4j node/edge enrichment (A2)** — `Method.docstring`; `Module.auto_install` /
  `.application` / `.category` / `.external_python` / `.external_bin` (manifest) +
  `.repo_url` / `.repo_id` (repo provenance, threaded pipeline→registry→writer); new
  `(:Method)-[:USES_FIELD]->(:Field)` (direct `self.<field>` access) and
  `(:Method)-[:DEPENDS_ON_FIELD]->(:Field)` (`@api.depends`) edges, best-effort MATCH
  (no stub fields).
- **`Field.string` + `Field.help` (A2-followup)** — field label + help text captured
  (era2 kwarg/positional, era1 best-effort) + persisted + rendered in `resolve_field`.
- **pgvector embeddings provenance (A3) — migration `m13_003`** — `line_start`, `repo`,
  `repo_id` columns; method/field chunks now carry the REAL source `.py` path (was the
  module dir). `parser_xml`/`parser_qweb` switched to lxml for `.sourceline`.

### Added — Group B (agent-convenient tool output)
- **Render existing provenance/intent (B1)** — `resolve_field` (comodel/label/help),
  `resolve_method` (signature/convention), `describe_module` (repo + path),
  `list_js_patches` (file_path), `list_owl_components` (template), `list_fields`
  (ttype/stored/compute/comodel), `find_deprecated_usage` (repo), `validate_domain`
  (did-you-mean typo suggestion).
- **Render new data + module dependencies (B2)** — surfaces docstring / repo_url /
  manifest-deps / embeddings file+line / field-level `USES_FIELD` impact;
  `module_inspect(method='dependencies')` returns the transitive `DEPENDS_ON` closure +
  per-dependency repo + topological load order.

### Added — Group C (multi-tenant enforcement — ADR-0034 WI-3/WI-4, RELEASE GATE)
- **`resolve_tenant_scope(tenant_id)` (C1)** — `(own, shared)` profile sets (own = the
  tenant's profiles; shared = all `tenant_id IS NULL` global base), 60s-cached.
- **Fail-closed Neo4j filter at all 61+4 Cypher sites (C2)** — uniform fragment
  `($own IS NULL OR all(__p IN <alias>.profile WHERE __p IN $own OR __p IN $shared))`:
  a node is granted iff every profile on it is own-or-shared, so another tenant's
  base-tagged private node is denied and a same-name collision fail-closes. `admin`
  (own=None) stays unrestricted; the optional `$profile_name IS NULL OR` bypass is
  fully removed. `_latest_version` + `find_override_point` now scoped too.
- **pgvector + list-tool scoping (C3/C4)** — `find_examples` / `find_style_override`
  filter `profile_name = ANY(own ∪ shared)` (`suggest_pattern` exempt — global
  catalogue); `list_available_versions` / `list_available_profiles` tenant-scoped.
- **Cross-tenant leak test (C6) — `tests/test_cross_tenant_isolation.py`** — the release
  gate: a tenant sees its own + the shared base, never another tenant's private node
  (with or without an explicit `profile_name`); spec data + admin stay unrestricted.

### Changed
- **ADR-0034 amendment** — records WI-3/WI-4 shipped; documents the pooled MERGE-key
  same-name collision limitation + the operator namespacing convention (proper
  MERGE-key discriminator = deferred REC-8 RFC); D6 Postgres RLS deferred to WI-7
  (the SQL filter is the read-guard; RLS needs `FORCE` + a non-owner read role).
- **`profile_name` is now ADVISORY** (M13 supersedes ADR-0029 "profile is convenience,
  not authz"): the tenant boundary is the isolation mechanism. The pre-M13
  `resolve_view` profile-filter test updated to the new semantics.
- **ADR-0005** corrected (v19 had a residual `Command` gap, now fixed);
  `bootstrap_versions.json` v11 `3.3.4`→`3.3.5`; 4 stale TASKS.md markers de-drifted;
  reindex runbook gains v19/provenance verification queries.

### Notes
- **DEFERRED:** Postgres RLS (WI-7), FERNET secrets manager, M10B Stripe, Prometheus
  histogram, nonce-CSP, VN persona docs + the cross-repo `odoo-mcp-client` mirror for
  `module_inspect(method='dependencies')`.

## [0.9.1] — 2026-05-22 — M13 pre-reindex wave: DB schema + multi-tenant foundation + git integrity

Eight work items (WI-A/B/C/D/E/G/H/I). No new MCP tools; tool surface remains **24**. Two Postgres migrations (`m13_001`, `m13_002`). Admin must run `python -m src.db.migrate` before deploying services, then execute the full reindex runbook.

### Added
- **License policy engine — ADR-0036** (WI-A) — `src/constants.py` `LICENSE_POLICY` config map assigns each license class an action (`serve` / `ingest_flagged` / `skip`). Default: OEEL-1 → `skip` (Viindoo's Odoo SA obligation); copyleft + OPL-1 + unknown → `serve`. `src/indexer/parser_python.py` extracts `license` + `copyright_owner` into `ModuleInfo`; `src/indexer/registry.py` enforces the policy at `build_registry()` (single chokepoint); `src/indexer/writer_neo4j.py` persists `Module.license` + `.copyright_owner` + `.license_notice`. MCP tool output surfaces `license_notice` for skipped/restricted modules — never a silent gap. Config flip (`OEEL-1 → serve`) exposes content with no code change. Test coverage: `tests/test_license_policy.py` (287 lines). Known OEEL-1 modules (skipped by default): v15/v16 — `l10n_it_edi_website_sale`; v17 — `account_payment_term` + `l10n_it_edi_website_sale`; v18 — `certificate`, `l10n_hr_edi`, `l10n_it_edi_website_sale`, `l10n_jo_edi_pos`, `project_hr_skills`; v19 — same minus `l10n_it_edi_website_sale`.
- **`embeddings.profile_name` column — migration m13_001** (WI-B) — `migrations/m13_001_embeddings_profile_name.sql`: `ALTER TABLE embeddings ADD COLUMN profile_name TEXT`; UNIQUE constraint updated; `idx_embeddings_filter` updated. `EmbeddingChunk` dataclass gains `profile_name`; INSERT and per-module DELETE in `src/indexer/writer_pgvector.py` updated. Profile-scoped chunk writes now active. **Postgres RLS deferred** — enforcement (WI-4 choke point) ships in the next enforcement wave. Test coverage: `tests/test_writer_pgvector.py` (142 lines new).
- **`tenants` table + tenant_id FKs + repos uniqueness — migration m13_002** (WI-C) — `migrations/m13_002_tenants_and_fks.sql`: `CREATE TABLE tenants`; `ALTER TABLE api_keys / profiles / ssh_key_pairs ADD COLUMN tenant_id` (FK `ON DELETE CASCADE`, `NULL` = shared/global); `ssh_key_pairs.key_type TEXT CHECK ('deploy_key','access_key')`; `repos` UNIQUE narrowed to `(url, branch, profile_id)` (allows cross-profile duplicates). Backward-compatible — existing rows default `NULL`. Test coverage: `tests/test_db_migrate.py` extended (191 lines total).
- **RelaxNG XML validation → `:LintViolation` nodes** (WI-E) — `src/indexer/parser_xml.py` post-parse step validates each view (v15+) against the version-exact RelaxNG schema read directly from the indexed Odoo source tree at index time (`<core_repo>/odoo/addons/base/rng/<view_type>_view.rng`) — no vendored copy, so every version validates against its own grammar. Correctness is driven purely by file existence: v15-v17 ship `tree_view.rng`, v18-v19 ship `list_view.rng` (Odoo renamed `<tree>` → `<list>`); `<include href>` resolves relative to the same source dir. Errors surface as `:LintViolation` nodes linked via a `(view)-[:HAS_VIOLATION]->(lv)` edge. `lint_check(language='xml')` returns the graph's RelaxNG `:LintViolation` nodes for a version (the `code` argument is not used for xml — this is corpus-level, not snippet-level, linting). Test coverage: `tests/test_relaxng_violations.py` (242 lines) + `tests/test_relaxng_violations_unit.py` (self-contained CI-safe RNG fixtures under `tests/fixtures/rng/`).
- **Git-URL-only repo registration + server-managed `local_path`** (WI-G) — `src/db/repo_registry.py` + `src/web_ui/routes/repos.py`: repos registered by git URL only; `local_path` derived server-side; `tenant_id` FK propagated on creation. Per-profile UNIQUE(url, branch, profile_id) allows the same URL to be registered under different profiles.
- **Known_hosts pinning + strict host checking** (WI-H/WI-9) — `src/git_utils.py`: replaces `StrictHostKeyChecking=accept-new` with a pre-populated pinned known_hosts for GitHub/GitLab/Bitbucket + `StrictHostKeyChecking=yes`. Eliminates TOFU MITM exposure + concurrent known_hosts write race at multi-tenant scale. **MED-2 onboarding constraint:** self-hosted forges require their SSH host key be added to the pinned file as a one-time step. Per-repo Postgres advisory lock (`lock_id` from `repo_id`) wraps every mutating git op (clone/fetch/reset). `git fetch` + `git reset --hard origin/<branch>` refresh path added. Test coverage: `tests/test_git_hardening.py` (487 lines).
- **Self-service deploy-key endpoint** (WI-I/WI-6) — `GET /api/tenant/deploy-key` (`src/web_ui/routes/deploy_key.py`): X-API-Key → tenant_id scoped; returns non-secret public key + add-as-deploy-key instructions; cross-tenant-safe (a key can only read its own tenant's deploy key). Test coverage: `tests/test_tenant_deploy_key.py` (393 lines).

### Changed
- **`verify_api_key` returns `tenant_id`** (WI-D) — `src/db/auth_registry.py` extended; `src/mcp/middleware.py` writes `request.state.tenant_id`; `src/mcp/tool_log_middleware.py` threads tenant context; tool-context thread-local in `src/mcp/server.py` exposes it. Legacy `tenant_id NULL` keys behave as admin/global (only unscoped path). **No read-side filtering yet** — enforcement deferred to WI-3/WI-4. Test coverage: `tests/test_tenant_id_plumbing.py` (397 lines).

### Notes
- No new MCP tools. Tool surface remains **24**. `GET /api/tenant/deploy-key` is a REST endpoint, not an MCP tool.
- **Read-enforcement DEFERRED:** WI-3 (`resolve_allowed_profiles`) + WI-4 (mandatory 61-site filter) + cross-tenant leak-test release gate ship in the next enforcement wave.
- **Verified Cypher site count for WI-4 scope:** 61 user-data Cypher query sites (57 in `src/mcp/server.py` + 4 in `src/mcp/orm.py`) PLUS 3 embeddings queries with no Neo4j filter (`find_examples`, `find_style_override`, `suggest_pattern`). The "~27 sites" figure in ADR-0034 is a pre-survey estimate; correct figure is 61 + 3.
- **OPS follow-up (admin):** `python -m src.db.migrate` to apply m13_001 + m13_002; then run full reindex v8→v19 per `docs/deploy/reindex-v8-v19-runbook.md` (needed for license/copyright_owner backfill + LESS nodes + LintViolation nodes + profile_name backfill on embeddings).

---

## [0.9.0] — 2026-05-22 — Reindex-prep DB-impact wave v8→v19

Bundled under PR #160. Six parser/indexer fixes that require a full reindex v8→v19 to take effect. No new MCP tools; tool surface remains 24.

### Added
- **LESS stylesheet indexing for v8-v11** (WI-3) — `src/indexer/parser_less.py` (regex-based, matching the `parser_scss` approach — no `tree-sitter-less` available on PyPI). Produces `:Stylesheet {language: "less"}` Neo4j nodes, `:IMPORTS` edges for `@import` chains, and `chunk_type='less'` pgvector embeddings (selectors, variables, mixins, imports, raw fallback). `find_examples` and `find_style_override` now accept `less` as a filter. `VALID_CHUNK_TYPES` in `src/constants.py` extended with `"less"`. ADR-0025 addendum added. Test coverage: `test_parser_less.py` (534 lines).
- **Curated `odoo.tools` CoreSymbol coverage — ADR-0033** (WI-4) — 12 `spec_data/tools_symbols_X.0.json` files (v8-v19) with curated `tool_export` CoreSymbols (not auto-parsed — manual curation for accuracy). New `src/indexer/parser_tools_symbols.py` loader. Enables: `lookup_core_api("odoo.tools.SQL","16.0")` = not-available; `"17.0"` = stable. `_DEPRECATED_API_SYMBOLS` expanded from 14 → 19 entries: +4 `image_resize_image*` (removed v13, `image_process` replacement) + `pycompat` (dropped from `odoo.tools.__init__` v19). `safe_eval` dedup: parsed CoreSymbol wins over curated when both exist. Test coverage: `test_parser_tools_symbols.py` + `test_tools_symbols_integration.py`.
- **v8/v9 CLICommand nodes from `parser_cli`** (WI-2) — `parser_cli.py` now resolves `openerp/` paths for v8/v9 (via `_PKG_PREFIX_REGISTRY`, see WI-6 below) and loads the static `commands` array from `spec_data/cli_flags_8.0.json` / `cli_flags_9.0.json` (the `"commands"` key inside each file) to produce `CLICommand` nodes. Test coverage: `test_parser_cli.py` extended with v8/v9 fixtures.
- **Lint rules ≥50/version for v10-v19** (WI-5) — all 10 `spec_data/lint_rules_X.0.json` files (v10-v19) expanded to ≥50 curated rules. `test_lint_rules_minimum_count.py::test_minimum_50_per_version` passes. v8/v9 remain at curation baseline (era1 scarce source data, expected).
- **`VersionRegistry` shared abstraction — ADR-0032** (WI-6) — `src/indexer/version_registry.py`: `VersionRegistry(min_major, max_major|None, handler)` — first-match wins, sorted by `min_major` ascending. Three registries wired: `_ERA_REGISTRY` (parser_python — era1/era2), `_PREFIX_REGISTRY` (parser_odoo_core — openerp//odoo/ prefix), `_OWL_ENABLED_REGISTRY` (parser_js — OWL v14+). `parser_cli` also gets `_PKG_PREFIX_REGISTRY`. Adding Odoo v20 behaviour is a 1-line registry append. Behavior-preserving: all existing era1/era2/era3 tests pass unchanged. OWL guard fails-soft on unparseable/`"unknown"` version (returns `None` = skip) vs prior `int()` which would raise. Test coverage: `test_version_registry.py` (216 lines).

### Fixed
- **v18/v19 generic field classes now classify as `field_type`** (WI-1) — `parser_odoo_core.py` detects `ast.Subscript` (e.g. `Field[int]`, `Field[str]`) in addition to `ast.ClassDef` when classifying CoreSymbols as `kind='field_type'`. Before this fix, v18/v19 generic field classes (`Integer`, `Many2one`, `Char`, etc.) were missing from the CoreSymbol graph after Odoo introduced generic field syntax. Test coverage: `test_parser_odoo_core.py` extended with Subscript fixtures.
- **PR #160 review fixes** — `VALID_CHUNK_TYPES` now includes `"less"` (was missing from initial WI-3 commit); `safe_eval` CoreSymbol dedup: parsed wins over curated (prevents duplicate nodes when both exist); LESS variable regex (`_RE_LESS_VAR`) uses a line-anchored negative lookahead to exclude CSS at-rule keywords (`import`, `media`, `charset`, `keyframes`, etc.) — the lookahead uses `(?![\w-])` so that variable names whose first token happens to start with a keyword prefix (e.g. `@media-breakpoint-xs`, `@page-header-height`) are still captured as variables; `parser_cli` registry wired via `_PKG_PREFIX_REGISTRY` (consistency with WI-6 pattern).

### Changed
- **`bootstrap_versions.json` corrected** (WI-7 docs) — v11 Bootstrap version `"4.0"` → `"3.3.4"` (v11 ships Bootstrap 3.3.4, not 4.x; v11 was the LESS→SCSS/Bootstrap 4 transition version but the actual shipped library is 3.3.4); v17 Bootstrap version `"5.3"` → `"5.1.3"` (precise patch version). The `site/src/pages/bootstrap.astro` page reads this file dynamically and inherits the correction automatically.
- **ADR drift corrections** — ADR-0002 §3 `_DEPRECATED_API_SYMBOLS` count updated 14 → 19; ADR-0025 `language` enum extended to `"css"|"scss"|"less"`, `mixin_count` now documented for LESS too, LESS addendum section added; ADR-0032 Consequences note added for OWL fail-soft robustness.
- **`view_type` docstrings** — `src/mcp/dto.py` `ResolveViewOutput.view_type` + `src/mcp/server.py` `_list_views_core` + `model_inspect`/`module_inspect` Args blocks now mention `'list'` (v18+ tag alias for `'tree'`). No logic change.

### Notes
- No new MCP tools. Tool surface remains 24. No Postgres migration required.
- **OPS follow-up (admin, after deploy):** run the full reindex v8→v19 per `docs/deploy/reindex-v8-v19-runbook.md`. Covers: `index-core` v8-v19 (tools symbols + LESS nodes + CLICommand v8/v9 + lint rules ≥50 + field_type v18/v19 fix); `index-repo --all --full` (LESS nodes + mth.depends backfill); Cypher cleanup (OWLComp pre-v14 + snap_mod); `reembed-stubs` per profile.

---

## [Unreleased] — M10C Polish Wave (PR #159)

### Added
- **`reembed-stubs` CLI subcommand** (`python -m src.indexer reembed-stubs --profile <name>`) — enumerates modules where `field_count > 0` but `embeddings_count == 0` via `LEFT JOIN embeddings`, re-runs `make_chunks` + `write_module_embeddings`; idempotent; log line summarises count + total embed calls per ADR-0010. (WI-3)
- **`audit-repo` CLI subcommand** (`python -m src.indexer audit-repo --profile <name> --output audit.json`) — emits a per-module JSON coverage report (field count, method count, embedding count, last indexed at) to the path given by the required `--output` flag. Closes M10 Quick Win "CLI batch audit". (WI-3)
- **`GET /api/repos/{id}/core-symbol-counts`** — new FastAPI endpoint returning per-version CoreSymbol counts for a repo; used by the admin UI core-index status column. Auth-gated, admin only. (WI-5)
- **Admin UI "Core Index" column** (`site/src/components/RepoTable.astro`) — per-version CoreSymbol count badge in `/admin/repos`, fetched from the new API endpoint above. Prevents user confusion between "repo indexed" and "core symbols indexed". (WI-5)

### Changed
- **`parser_odoo_core.py` body-level `DeprecationWarning` detection** — method body AST walk (`_has_body_level_deprecation_warning`) now detects `warnings.warn(...)` calls where `DeprecationWarning` appears as any positional arg or as the `category=` keyword (e.g. `warnings.warn("...", DeprecationWarning, stacklevel=2)`). After re-index, `lookup_core_api("name_get", "17.0")` returns `status='deprecated'` instead of incorrect `'stable'`. Detection tightened in review-followup (matches only `warnings.warn`, not `logger.warn`/`self.warn`). (WI-2)
- **`parser_js.py` OWLComp pre-v14 guard** — `_extract_era3_patches` returns early when `major < 14`, symmetric with the existing `_extract_era3_components` guard. Prevents new anachronistic `__unresolved__` OWLComp stubs being written to Neo4j for v8-v13 repos on future reindex. Existing 239 stubs require a one-time Cypher cleanup (see Full Reindex Runbook). (WI-1)
- **`admin_audit_log` legacy column drop** — `actor_id`, `target_id`, `detail_text` columns removed via migration `m9_010_drop_audit_legacy_columns.sql`; dual-write removed from `AuthRegistry.log_audit()` (now canonical-only INSERT). All consumers use the canonical columns `actor`, `action`, `target`, `success` (+ `detail` JSONB via `src.db.audit.write_audit_log`). (WI-4)

### Fixed (review-followup)
- N+1 query hoist in `core-symbol-counts` endpoint - single Cypher query replaces per-version round-trips.
- Neo4j driver close guard in `core-symbol-counts` to prevent connection leaks on error paths.
- Version sort uses `toFloat(v)` in Cypher (not lexicographic) — consistent with ADR-0013 tiebreak policy.
- Migration file renamed `0006_drop_audit_legacy_columns.sql` → `m9_010_drop_audit_legacy_columns.sql` for yoyo ordering consistency.
- Body-level `DeprecationWarning` AST match tightened in `parser_odoo_core.py` to require the callable be exactly `warnings.warn` (`ast.Attribute` `attr=='warn'` with `func.value` Name `'warnings'`) — avoids false positives from `logger.warn`/other `.warn` calls.
- Docstrings corrected for `core_symbol_counts` and `log_audit` to match actual behaviour.

### Notes
- No new MCP tools in this release. Tool surface remains 24.
- **OPS follow-up (admin, weekend):** run `python -m src.db.migrate` to apply `m9_010`; then run full reindex v8-v19 (see Full Reindex Runbook in `docs/deploy/m10-postmerge-ops.md`) to backfill `mth.depends` + correct `name_get` status + clear pre-v14 OWLComp stubs.

---

## [0.8.0] — 2026-05-21 — M10.5 Phase 2: ORM validation tools

### Added
- **`resolve_orm_chain(model, dotted_path, odoo_version)`** — new MCP tool. Walks a dotted field path (e.g. `partner_id.country_id.code`) hop by hop across the indexed Field graph, returning the terminal field type or a `BROKEN` line naming the first unresolved hop. Handles ORM magic fields (`create_uid` → `res.users`, etc.) and inherited fields reached via `INHERITS`/`DELEGATES_TO` (e.g. `message_ids` from a `mail.thread` mixin).
- **`validate_domain(model, domain, odoo_version)`** — new MCP tool. Parses a domain literal and validates each `(field_path, operator, value)` term: every field-path hop must resolve, and the operator must be valid for the version. Operator validity is **version-aware** (cross-version survey v8→v19): `parent_of` from v9, `any`/`not any` only from v17, v19 access-rights variants (`any!`/`not any!`). Logical connectors (`&`, `|`, `!`) are skipped.
- **`validate_depends(model, method, odoo_version)`** — new MCP tool. Reads the indexed `@api.depends('a.b', ...)` arguments of a compute method and validates each dependency path; flags depends on `id` (Odoo raises `NotImplementedError`) and suggests the closest field name for typos. Era1 (v8/v9, no decorator depends) surfaces a clear "no @api.depends" note.
- **`validate_relation(model, field, target_model, odoo_version)`** — new MCP tool. Asserts a field is a many2one/one2many/many2many whose comodel is `target_model` (or a subtype via inheritance); reports the actual comodel on mismatch and suggests the closest field name when missing.
- **`MethodInfo.depends` graph property** (M10.5 Phase 2 data layer) — parser now extracts `@api.depends` string args (era2 AST; lambda/callable args skipped as non-static; era1 has none); writer persists `mth.depends` in Neo4j. Powers `validate_depends`.
- **`valid_domain_operators(odoo_version)` + `RELATIONAL_TTYPES`** in `src/constants.py` — version-keyed domain operator sets; unknown/sentinel versions return a permissive superset (no false positives).

### Changed
- **Tool surface 20 → 24** — four ORM-validation tools added. `tools/list` now reports 24 tools. The four tools read version-tagged graph nodes, so they are version-agnostic; the only version-aware logic is the domain operator set and the era1 depends gate.

### Notes
- Implementation in new module `src/mcp/orm.py` (primitive `_traverse_field_chain` + 4 impls), mirroring `src/mcp/inspect.py` (late-import of `server` to avoid a circular dependency).
- **Ops follow-up:** run `python -m src.indexer index-repo --all --full` on prod to backfill `mth.depends` for existing Method nodes (mirrors the M10.5 Phase 1 `comodel_name` reindex).
- **Cross-repo follow-up:** routing matrix EN+VI + adapters/persona skills for the 4 ORM tools need updating at [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client) (the client hand-mirrors the server tool surface — no generator).

---

## [0.7.1] — 2026-05-21

### Fixed

- **Superset filter parity:** `model_inspect` now forwards `kind` (method='fields') and `view_type` (method='views') to the underlying enumeration impls; `module_inspect` now forwards `view_type` (method='views'), `bound_model` (method='owl'), and `era` + `target` (method='js'). Completes the filter-forwarding started by `from_module` in 0.7.0 — the supersets now expose every filter the removed flat tools had (ADR-0028).

---

## [0.7.0] — 2026-05-21 — M10A + M10.5-P1: stylesheet tools, magic fields, from_module, noqa, comodel_name

### Added
- **`resolve_stylesheet(module, odoo_version)`** (M10A) — new MCP tool (#19). Returns the full stylesheet chain for a module: file path, import graph, CSS custom properties / SCSS variables. Output follows ADR-0023 tree-grammar contract.
- **`find_style_override(selector_or_variable, odoo_version)`** (M10A) — new MCP tool (#20). Traces which module last re-declares a CSS custom property or overrides a selector across the indexed stylesheet graph.
- **Magic-fields `<builtin>` prelude** (M10A D2) — `resolve_model`, `list_fields`, `resolve_field` now include a synthetic `<builtin>` section listing `id`, `display_name`, `create_uid`, `create_date`, `write_uid`, `write_date` for all `models.Model` subclasses. Source-of-truth: `src/constants.py::MAGIC_FIELDS`. Not written to Neo4j; injected at query time.
- **`from_module` param** (M10A D3) — `model_inspect` (kind=fields) and `entity_lookup` (kind=field) accept an optional `from_module` argument to restrict field declarations to those originating from a specific module.
- **`noqa` suppression in `lint_check`** (M10A D4) — inline `# noqa: <rule_id>` comment suppresses the matching lint rule for that line. Multiple rules: `# noqa: ORM001,ORM002`. Bare `# noqa` suppresses all rules on that line.
- **`Field.comodel_name` graph property** (M10.5 Phase 1) — `FieldInfo.comodel_name: str | None` dataclass field; parser extraction for `fields.Many2one`/`One2many`/`Many2many` (era1 text-regex + era2 AST); writer persists `f.comodel_name` in Neo4j. Enables M10.5 Phase 2 ORM validation tools.

### Changed
- **Tool surface 18 → 20** (M10A D5+D6) — two stylesheet tools added. `tools/list` now reports 20 tools.

### Notes
- PR #156 — includes code-review fixes: model-scoped field dedup, `(none)`-sentinel for missing comodel, hint-variable naming, stylesheet tree-grammar contract + batch Cypher, header decoration for builtin prelude.
- Cross-repo follow-up: routing matrix EN+VI for `resolve_stylesheet` / `find_style_override` needs update at [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/reference/mcp-tool-routing.md).
- M10.5 Phase 1 data layer: run `python -m src.indexer index-repo --all --full` on prod to backfill `comodel_name` for existing Field nodes.

## [0.6.0] — 2026-05-21 — v0.6: remove 10 deprecated flat tools (ADR-0028 timeline)

### Added
- `model_inspect` / `module_inspect` now accept `start_index` + `limit` and forward them to the underlying field/method/view/owl/qweb/js listings — preserves the paginated drill-down that the removed flat `list_*` tools provided (the pager continuation hint now names a superset that actually paginates).

### Removed
- Removed 10 deprecated flat MCP tools (ADR-0028 deprecation timeline): `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches`. Tool surface 28 → 18. Use the `model_inspect` / `module_inspect` / `entity_lookup` supersets instead.

### Fixed
- `resources/read` now honours `set_active_version` — added `on_read_resource` hook to `UsageLogMiddleware` so the sticky per-API-key version applies to `odoo://` resource reads, not just tool calls. [WI-B1]
- `set_active_version` / `set_active_profile` validate inputs — pinning a non-indexed version or unknown profile returns an error tree listing valid options instead of silently falling back. [WI-B2]
- Surviving tools' next-step/pager hints + `TRIGGER/PREFER/SKIP` docstrings no longer reference the removed flat tools — all redirected to the `model_inspect` / `module_inspect` / `entity_lookup` supersets (caught + fixed in-PR by the code-review pass).

### Changed
- ADR-0029 amended: `set_active_profile` documented as default-arg convenience, not an access-control boundary.

---

## [0.5.0] — 2026-05-21 — M10.5 + M11 tool UX · go-live deploy · open-core split · security hardening

Consolidated release covering all work since v0.4.1: the M10.5 + M11 tool-UX/architecture batch, the go-live production deploy, the M9 Coverage Fill + RBAC follow-ups, the open-core repo split with AGPL license metadata, the internal-data security purge, and SPDX/housekeeping. Sub-sections below are grouped by theme and date.

### Housekeeping — SPDX headers + script fix + ADR-0031 (2026-05-21)
- [SPLIT] Housekeeping: added SPDX-License-Identifier: AGPL-3.0-or-later headers to all 200 `tests/**/*.py` and 6 `scripts/` files (`.py` + `.sh`). Fixed `add-spdx-headers.sh` `prepend_py()` to insert SPDX as line 2 when shebang is present (preserves shebang executability). Extended script to cover `tests/`, `scripts/*.py`, and `scripts/*.sh` targets. Added ADR-0031 (python-dotenv auto-load at CLI entry points) to `CLAUDE.md` ADR list.

### Security — purge internal deployment data (2026-05-20)
- [SECURITY] Purged private Viindoo deployment topology (private repo names, seed roster, version presets) from the public repository. Master-data seed roster removed; profiles and repos are now created by admins via the web UI or JSON API. History rewrite applied.

### Open-core repo split + AGPL license metadata (2026-05-20)
- [SPLIT] Moved MIT plugin + client docs to Viindoo/odoo-mcp-client. Server repo retains AGPL-3.0 backend + Astro web UI. Added SPDX-License-Identifier: AGPL-3.0-or-later headers across all 88 `src/**/*.py` files and 42 `site/src/**` files (.ts/.tsx/.astro). Added license field to `pyproject.toml` and `site/package.json`. Added copyright + applicability notice atop `LICENSE`. Added `NOTICE` (Viindoo trademark statement + common_passwords attribution) and `data/common_passwords.txt.LICENSE`.

### Post-0.4.1 hardening + go-live deploy + M9 Coverage Fill + M9 RBAC follow-up (2026-05-18)

6 PRs merged after v0.4.1. Production deployed at PR #119 / commit `3f081b9` (admin-invite signup model active). PR #120 (M9 Coverage Fill) + PR #121 (docs signoff) merged but not yet deployed to prod. Two post-deploy hotfixes shipped 2026-05-18 — PR #124 (`init_pool` ordering in seed_patterns CLI) and PR #125 (CLIFlag null command_name MERGE bug surfaced when running `index-core` against M9 curated spec_data). PR #<TBD> (M9 RBAC follow-up) in progress.

### Migration 0004 self-contained SQL rescue (PR #117)

#### Added
- `migrations/0004_add_missing_version_profiles.sql` seeds all 12 root CE profiles (`odoo_8` through `odoo_19`) with `ON CONFLICT (name) DO NOTHING`. SQL is self-contained for DBA-only rescue paths (no Python required).
- `src/db/seed_master_data.py` remains source of truth for the CE root profiles and still handles 2-pass FK inserts for hierarchical profiles.

#### Tests
- Profile-touching tests migrated to distinct test names (`test_root_99`, `test_mid_99`, `test_leaf_99` at version 99.0) or switched to a seeder-only fixture profile for conflict-test scenarios.
- Seed count assertion in `test_master_data_seed.py` bumped 5 → 12.

### Security headers — CSP + Permissions-Policy (PR #118)

#### Added — closes M9 CSP gap (memory: m9_csp_permissions_policy_gap.md)
- FastAPI `_SecurityHeadersMiddleware` injects `Content-Security-Policy: default-src 'none'` + `Permissions-Policy` on every JSON-API response (ADR-0015 — JSON-only, never serves HTML).
- Astro SSR `_addSecurityHeaders()` emits per-path tighter CSP on every SSR response (`/admin/*`, `/signup`, `/verify-email`, `/reset-password`). `script-src 'self' 'unsafe-inline'` because Astro inlines small page scripts.
- Edge nginx/Caddy emits permissive superset CSP that covers prerendered static pages (`/`, `/pricing`, `/bootstrap`, `/benchmarks`).
- 8 regression tests in `TestSecurityHeadersFastAPI` replace nginx-placeholder `TestNginxHeadersDocumented`.

#### Notes
- Nonce-based CSP migration tracked as M10 followup.

### Go-live batch — writer profile + MFA sync + backup CLI + /api/health (PR #119)

5 commits squashed: 4 WIs (Pattern 1 orchestration) + 1 followup commit (Opus review HIGH fixes + boil-the-lake findings + sanitization). Verified end-to-end on production 2026-05-17 (deploy + post-deploy ops phase). See PR description + `docs/deploy/pre-launch-checklist.md` followups #12-#15 for known gaps.

#### Fixed — WI-1 indexer writer + parser_js + ADR-0016 D7
- `src/indexer/writer_neo4j.py`: 6 placeholder MERGE sites (Module dep, Model INHERITS, Model DELEGATES_TO, View INHERITS_VIEW, QWebTmpl EXTENDS_TMPL, OWLComp PATCHES) now inherit the referencing module's profile array:
  - `ON CREATE SET <node>.profile = $profiles` on first MERGE.
  - `ON MATCH SET <node>.profile = [x IN coalesce(<node>.profile, []) WHERE NOT x IN $profiles] + $profiles` on subsequent MERGEs — UNION semantics mirroring real-node pattern from commit `4ff56a8` (prevents clobber when profile B references a stub previously created for profile A).
- `src/indexer/writer_neo4j.py`: 3 resolver MATCH sites (INHERITS Model, DELEGATES_TO Model, PATCHES OWLComp) now exclude `__unresolved__` stubs via `WHERE NOT coalesce(<var>.unresolved, false)` — symmetric with existing INHERITS_VIEW + EXTENDS_TMPL pattern. Without this, second referencer would resolve INHERITS to first referencer's stub and skip the union write.
- `src/indexer/parser_js.py`: `_extract_era3_components()` returns early when `int(odoo_version.split('.')[0]) < 14` — OWL framework only exists v14+.
- `docs/adr/0016-profile-hierarchy-and-neo4j-isolation.md`: new section **D7 — Stub node ownership policy** documenting the UNION pattern + 6 writer sites + future-contributor guidance.

#### Fixed — WI-2 webui auth MFA sync
- `src/web_ui/routes/totp.py`: `_enable_totp()` and `_delete_totp()` now also `UPDATE webui_users SET mfa_enabled = TRUE/FALSE WHERE id = %s` in the same transaction as the `totp_secrets` write. Login still gates on `totp_secrets.enabled`; users column is now authoritative for queries.
- `migrations/m9_009_backfill_mfa_enabled.sql`: idempotent symmetric reconciliation — sets TRUE for users with `totp_secrets.enabled=TRUE`, FALSE for any user `mfa_enabled=TRUE` without a matching TOTP row. Followup commit added the FALSE-reset half (boil-the-lake F).

#### Added — WI-3 backup CLI + systemd + runbook
- `src/cli.py` `_get_pg_dsn()`: refactored to use `config.from_env_or_ini("PG_DSN", "database", "pg_dsn")` helper (consistent with rest of codebase).
- `src/cli.py` `_resolve_postgres_tool(tool)`: new helper returns `[tool]` if `shutil.which` finds it locally, else `["docker", "exec", "-i", "-e", "PGPASSWORD", container, tool]` (PGPASSWORD forwarded via `-e VAR` syntax — host env propagates into container). Container name from `POSTGRES_CONTAINER` env, default `odoo-semantic-mcp-postgres-1`.
- `src/cli.py` `_resolve_neo4j_tool(tool)`: parallel helper for Neo4j tools (`neo4j-admin database dump`). Container env `NEO4J_CONTAINER`, default `odoo-semantic-mcp-neo4j-1`. No PGPASSWORD bleed.
- `src/cli.py` `_cmd_backup` pg_dump: stdout redirect (`stdout=open(pg_out, "wb")`) instead of `-f <host_path>` so docker-exec'd pg_dump pipes output back to host. psql restore paths already use stdin redirect (no change needed).
- `docs/deploy/odoo-semantic-backup.service` + `.timer` + extended `logrotate.d/odoo-semantic` + bilingual `backup-runbook.md`. Systemd unit uses canonical placeholders (`User=odoo-semantic` + `/opt/odoo-semantic-mcp`) per public-repo convention; `ExecStart` wraps in `/bin/sh -c '... $(date +%Y%m%d-%H%M%S) ...'` so timestamp expands per run (systemd `%` specifiers don't include strftime).
- 4 new docker-fallback tests in `test_backup_cli_docker_fallback.py` + 4 new Neo4j docker-fallback tests in `test_neo4j_cli_docker_fallback.py` + 5 existing CLI tests patched to mock `shutil.which` (environment-sensitive baseline).
- `migrations/m9_007_totp_secrets.sql` stale comment ("no mfa_enabled needed in webui_users") replaced with reference to WI-2 m9_009 sync.

#### Added — WI-4 /api/health auth-exempt endpoint
- `src/web_ui/app.py` `GET /api/health` returns `{"status": "ok", "version": "<__version__>"}` HTTP 200.
- `src/web_ui/middleware.py` `_EXEMPT_EXACT` set includes `/api/health` so unauthenticated requests bypass `AuthRequiredMiddleware`. Loopback-only + security header middlewares still apply.
- `src/_version.py`: new single-source version reader via `importlib.metadata.version("odoo-semantic-mcp")` with `PackageNotFoundError` fallback (no hardcoded duplication of `pyproject.toml`).
- 1 new TestClient test asserting unauthenticated 200 + `status` + `version` keys.

#### Fixed — Followup commit consolidates Opus review HIGH findings + 6 boil-the-lake fixes
- Docker-exec pg_dump no longer writes `-f <host_path>` inside container (loses output). Now uses stdout redirect.
- PGPASSWORD forwarded into container via `docker exec -e PGPASSWORD` (host env override didn't reach pg_dump inside).
- systemd `osm-%%Y%%m%%d-%%H%%M%%S.tar.gz` placeholder fixed: ExecStart wraps `/bin/sh -c '… $(date +%Y%m%d-%H%M%S) …'` (systemd specifiers don't expand strftime; nightly runs now produce distinct files).
- psql call sites switched from `text=True` to bytes mode for consistency with pg_dump fix; stderr decoded with `errors='replace'` for human-readable errors.
- `tests/test_writer_neo4j_stub_profile.py`: module-level `pytestmark = pytest.mark.neo4j` per CLAUDE.md convention; pure-unit OWL era guard test moved to `tests/test_parser_js.py`.
- `_version.py` deduplication (importlib.metadata).
- m9_009 migration symmetric backfill (also resets FALSE for users without active TOTP).
- Neo4j docker-exec fallback (parallel to Postgres helper).
- `src/web_ui/middleware.py` module docstring updated with `/api/health` in exempt-paths list.

#### Tests
- 11 new tests across 4 new files (writer stub profile, MFA sync, backup CLI docker, /api/health) + Neo4j docker fallback tests (post-followup).

#### Sanitization
- Initial commit history had host-specific paths (`/home/<user>/...`) and prod state in PR body; force-pushed to clean 1-commit branch using canonical `/opt/odoo-semantic-mcp` + `User=odoo-semantic` placeholders matching existing `docs/deploy/odoo-semantic-mcp.service`. Memory: `feedback_public_repo_sanitize.md`.

### M9 Coverage Fill batch (PR #120)

7 WIs landed: CSS/SCSS parser, v8 era1 field gap fix, pattern backfill, lint/CLI curation, deferred items absorption.

#### Added
- CSS/SCSS indexing: new `parser_css.py` + `parser_scss.py` with tree-sitter-css backend (regex fallback). Creates `:Stylesheet` Neo4j nodes (composite key `(file_path, module, odoo_version)`) + `:DEFINED_IN` + `:IMPORTS` edges. Pgvector chunk_types `css`/`scss`. (WI-A1, ADR-0025)
- PatternExample catalogue v9-v15: 30 curated patterns from real Odoo sources (`patterns.json` 83→113). (WI-A3)
- LintRule static curation v8-v19: 12 `spec_data/lint_rules_X.json` populated with ~270 rules + schema. (WI-A4)
- CLIFlag static curation v8-v19: 12 `spec_data/cli_flags_X.json` populated with ~880 flags + schema + cross-version deprecation tracking. (WI-A5)

#### Fixed
- v8 era1 `_columns` extraction: string-aware brace scan no longer truncates blocks at `{` inside string literals. `FieldInfo.source_definition` now populated for era1. (WI-A2)

#### Notes
- Post-deploy ops B1-B11 (CoreSymbol/LintRule/CLI ingestion runs, OBS-1 reindex, additional profile registration, full reindex for CSS/SCSS embeddings) tracked in the post-deploy ops plan.
- WI-A7 (deferred items absorption into TASKS.md M10/M10.5/M11 + ADR follow-up sections) pending Opus dispatch.

### Pre-launch checklist signoff (PR #121, docs only)

#### Changed
- `docs/deploy/pre-launch-checklist.md` items §4.1, §5.1, §8.6, §10.5 `/api/health` flipped to `[x]` post PR #119 deploy. §4.2, §5.2 marked `[~]` partial with followup references. §11 sign-off table filled (9 of 11 sections `[x]`).
- Known followups appended: #12 OWLComp v14 anachronism (239 stubs from JSPatch era3 in pre-v14 modules — read-side era guard already protects user output), #13 Neo4j online backup (Cypher export OR Enterprise backup cmd), #14 logrotate `/var/log` perms (pre-existing stanza), #15 §6 tools 15-21 prod smoke (deferred next session).

### Post-deploy hotfixes (2026-05-18)

#### PR #124 — `[FIX] indexer: init_pool before job_store in seed_patterns CLI`
- `src/indexer/seed_patterns.py` now calls `init_pool(dsn, ...)` before resolving `_get_job_store()`. Previous ordering raised `PostgreSQL pool is not initialized` when invoking `python -m src.indexer.seed_patterns --force`, blocking the B10 PatternExample reseed step of the M9 Coverage Fill post-deploy ops sequence.

#### PR #125 — `[FIX] indexer: coalesce CLIFlag command_name null → "server"`
- `src/indexer/parser_cli.py::_load_static_cli_flags` coerces `command_name` `None` → `"server"`, matching the live parser default for `odoo-bin server` flags.
- M9 Coverage Fill curated `cli_flags_*.json` files (12 versions × ~70-88 flags each) declared `command_name: null` for global flags like `--config`, `--init`, `--update`. Neo4j 5.x rejects null property values in MERGE identity keys (`Cannot merge ... null property value for 'command_name'`), aborting every `index-core` invocation before any CLIFlag node was written.
- Regression test covers explicit null, explicit "server", and missing key.

### Documentation

- Closed 4 de-facto-done backlog items in TASKS.md: M11 pattern catalogue target met (113 patterns), lint_json_response.sh advisory clean (0 violations), Reseed Patterns Web UI button verified wired end-to-end, M7.5-P2-SEED production seeding completed in B10 ops phase.
- Deduplicated 9 redundant TASKS.md backlog entries (NAMEGET, v8 era1 CLI, VN translation, pricing, nonce CSP) — each item now lives in exactly one canonical milestone location.
- Split Milestone 10 into M10A (Tool Surface Expansion) + M10B (Billing Wow Core) + M10C (Polish + Observability) for clearer scope.

### Production state at go-live cut (2026-05-18)

- Production HEAD: PR #119 / commit `3f081b9` deployed 2026-05-17 (PR #120 + #121 not yet deployed to prod).
- Neo4j: 0 NULL profile nodes (down from 5,988 pre-cleanup); 0 pre-v14 OWLComp anachronisms among NULL-profile set; 239 `__unresolved__` v8-v13 OWLComp stubs remain (have profile set; tracked as followup #12).
- Backup automation: systemd nightly timer scheduled 03:00:00; first manual run produced 2.55 GB postgres bundle (Neo4j component skipped — followup #13).
- Webui crash sim: passed (SIGKILL → 5s auto-restart).
- Embeddings: 528,577 across all profiles (unchanged from pre-deploy; `--no-embed` verify pass did not touch pgvector).

### M9 RBAC + Key-Ownership Bug Fix (PR #<TBD>)

6 WIs orchestrated (5 code, 1 docs). Root cause: `request.session.get("is_admin")` returned False because login never wrote that field; all 5 legacy API keys had `user_id IS NULL` → admin saw empty list. Additionally closes a security hole (unauthenticated users could not deactivate keys, but any authenticated user could deactivate any key by ID without ownership check) and completes M9 §3.4 admin user management.

#### Fixed
- **API key list filter restored for admins** — new `is_admin_session(request)` helper in `src/web_ui/auth.py` DB-sources `is_admin` per request instead of reading absent session field. Clarifies ADR-0011 rule 6 and prevents regression.
- **API key deactivate endpoint now enforces ownership** — `PATCH /api/api-keys/{id}/deactivate` checks that requesting user owns the key OR is an admin (HTTP 403 if neither). Closes M9 security gap.

#### Added
- **Admin promote/demote** — `PATCH /api/admin/users/{id}/admin` endpoint + UI toggle on `/admin/users` with last-admin protection (refuse demote if it leaves 0 active admins). New `set_user_admin()` AuthStore method.
- **Key→owner attribution** — `owner_username` field on `GET /api/api-keys`; Owner column + "Assign owner" banner on `/admin/api-keys` for legacy NULL-owner keys. New `PATCH /api/admin/api-keys/{id}/owner` endpoint for admin assignment. Self-service UI deactivate on `/account/api-keys`.
- **`/account/api-keys` self-service surface for non-admin users** (slim `AccountLayout`). Non-admins hitting `/admin/*` now redirect to `/account/api-keys` (via Astro middleware). New `/account/index` dashboard (read-only, shows "Profile access: VIEW" status).

#### Architecture
- `is_admin_session(request: Request) -> bool` replaces all `request.session.get("is_admin")` calls. DB-sourced, cached 5 min per existing auth cache.
- Web UI surface split: `/admin/*` for admins (full sidebar); `/account/*` for non-admins (slim sidebar).
- Last-admin protection on demote/deactivate via `set_user_admin()` and `set_user_active()` SQL logic.
- NULL-owner system keys assignable by admins interactively (modal + PATCH).

#### Tests
- 28 new backend + frontend tests (WI-1 through WI-5).

#### Fixed — post-Opus-review follow-ups (committed after PR #127 initial review)
- **browser-tests-admin admin seed**: `set_user_password(TEST_ADMIN_USERNAME, ..., is_admin=True)` — the test admin was seeded with `is_admin=False` (default), causing WI3 middleware to redirect the "admin" browser to `/account/api-keys` and all 70+ admin browser tests to time out (25-min wall clock in CI).
- **ADR-0026 doc drift**: last-admin protection status corrected 409→422 (matches `admin_users.py:285`); `/account/index` described as thin redirect not a dashboard (matches `account/index.astro`); audit action names corrected to `user.set_admin` + `api_key.assign_owner` (matches `@audit_action` decorators).
- **`is_admin_session` fail-closed**: `uid=None` now returns `False` instead of `True`. Malformed session cookie or SessionMiddleware crash no longer grants implicit admin privilege.
- **`set_user_admin` / `set_user_active` concurrent demote serialisation**: added `SELECT ... FOR UPDATE` on the target row before the admin-count check, preventing TOCTOU race where two concurrent demotes could both pass the guard and leave 0 admins.
- **`assign_key_owner_route` audit detail**: old_user_id → new_user_id transition now captured in `request.state.audit_detail` before the PATCH call, giving forensic before/after in the audit log.

#### Docs
- ADR-0026 — RBAC + key ownership (5 design decisions, 2 consequences sections, alternatives considered).
- TASKS.md Stream J (6 WIs + completion note).
- CLAUDE.md new section "Auth — is_admin Source of Truth" (1 paragraph clarifying the DB-sourced rule).
- CHANGELOG.md (this section).

### Tool UX + Architecture — M10.5 + M11 (2026-05-19)

6 waves + 8 patterns landed in a single worktree via the `feat/m10-5-m11-tool-ux-architecture` branch (33 commits over Waves A–F + F-FINAL). Plan: internal plan (archived). Research: 12 MCP design patterns evaluated, 8 adopted (archived internally). 3 new ADRs (0028/0029/0030) + ADR-0023 amended.

### Wave A — Quick Wins (M10.5)

- **Tool annotations** (WI-A1): `READONLY_TOOL_KWARGS = {"read_only_hint": True, "idempotent_hint": True}` applied to all 21 existing `@mcp.tool()` decorators. Signals to MCP hosts that no write side-effects occur. ADR-0023 §2 docstring language policy re-affirmed.
- **Next-step hints SSOT** (WI-A2): centralized into `src/mcp/hints.py` — single dict maps tool name → hint string. All 18 drill-down tools import from there; 4 CI assertions added.
- **Grammar consistency tests** (WI-A3): `tests/test_grammar_consistency.py` — 4 tests (language-policy regex, no-self-loop, truncation-disclosure, next-step-present).
- **Self-mythology docstrings** (WI-A4): `lookup_core_api` and `find_deprecated_usage` TRIGGER/PREFER/SKIP blocks updated with accurate self-description.

### Wave B — Output Envelope (M10.5)

- **Shared TreeBuilder** (WI-B1): `src/mcp/tree_builder.py` — `TreeBuilder` class with `add_branch`, `add_sublist`, `add_next` methods. `_resolve_model` and `_list_fields` migrated as PoC.
- **Pydantic DTOs** (WI-B2): `src/mcp/dto.py` — 6 `*Ref` + 7 `*Output` Pydantic models. `ModelRef`, `FieldRef`, `MethodRef`, `ViewRef`, `ModuleRef`, `PatternRef`; `ModelOutput`, `FieldOutput`, etc.
- **Dual-channel ToolResult** (WI-B3): 7 priority tools (`resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`, `describe_module`, `list_fields`, `list_methods`) return `{"content": tree_text, "structuredContent": dto.model_dump()}`. AI clients that support `structuredContent` get machine-parseable data; others fall back to tree text.
- **Dual-channel tests** (WI-B4): `tests/test_dual_channel_envelope.py` — 8 tests asserting both channels non-empty + DTO schema round-trips.

### Wave C — Drill-down Cohesion (M10.5)

- **Opaque ref IDs** (WI-C1/C2/C3): `src/mcp/refs.py` — per-call ref minter with API-key tenancy + 5min TTL. 6 `_list_*` tools emit `[ref=fN]` row tokens; 4 `_resolve_*` tools accept `target=<ref>` OR canonical `model+field+version` — backward compatible. Pagination: `start_index: int = 0` added to all 6 list tools.
- **Ref drilldown tests** (WI-C4): `tests/test_drilldown_refs.py` — 8 tests (ref lifecycle, cross-tenant isolation, ref→resolve round-trip).

### Wave D — Discriminator Consolidation (M11)

- **3 superset tools** (WI-D1): `model_inspect(target, odoo_version, kind)`, `module_inspect(target, odoo_version, kind)`, `entity_lookup(target, odoo_version)` implemented in `src/mcp/inspect.py`. Discriminator field in `structuredContent` signals which sub-tool was invoked.
- **10 deprecation shims** (WI-D4): `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view` + 6 `list_*` tools wrapped with `DeprecationWarning` footer + ADR-0028 migration hint. `@deprecated` decorator in `src/mcp/server.py` adds `[DEPRECATED: v0.5 → v0.6]` prefix to tool description.
- **Tests** (WI-D5): `tests/test_mcp_inspect_router.py` (12 tests) + `tests/test_mcp_deprecation_shims.py` (8 tests).
- **ADR-0028** (`docs/adr/0028-discriminator-consolidation.md`): discriminator field contract, deprecation timeline (v0.5 shim → v0.6 removal), migration guide for callers.

### Wave E — Implicit Context (M11)

- **Session state migration** (WI-E1): `migrations/0005_api_key_session_state.sql` — `api_key_session_state` table with `api_key_id PK`, `active_version`, `active_profile`, `updated_at`.
- **Session module** (WI-E2): `src/mcp/session.py` — `read_session()`, `write_session()`, `normalize_version_arg()`, `resolve_version_v2()`. 60s in-process cache per `api_key_id`. 6 sentinel strings collapse to per-key active version.
- **4 session tools + resolver patches** (WI-E3): `set_active_version`, `set_active_profile`, `list_available_versions`, `list_available_profiles` registered in `server.py`. All 21 existing tool wrappers patched to call `resolve_version_v2` so sentinels work transparently.
- **Session tests** (WI-E4): `tests/test_mcp_session_state.py` — 11 tests (read/write round-trip, sentinel collapse, 60s cache, 24h TTL, concurrent tenant isolation).
- **ADR-0029** (`docs/adr/0029-implicit-session-context.md`): 6 sentinels, 3-tier resolution (explicit → session → latest-indexed), TTL policy, concurrent-tenant isolation guarantee.

### Wave F — MCP Resources (M11)

- **7 resource handlers** (WI-F1): `src/mcp/resources.py` — `register_resources(mcp_instance)` wires `@mcp.resource` for 7 `odoo://` URI templates. LRU cache 1000/300s. Cache key formed from **resolved** version (not raw sentinel) — prevents tenant leakage when two API keys with different active versions read `odoo://auto/model/X`.
- **Top-100 popular models** (WI-F2): `src/mcp/resources_index.py` — `odoo://index/popular_models` resource returns top-100 models by field+method count across all indexed versions; cached 1h.
- **Server wiring + docstring hints** (WI-F3): `register_resources(mcp)` called at startup; 7 `_render_*` functions referenced in their respective tool docstrings as "→ available as `odoo://{version}/kind/...`".
- **Tests** (WI-F4): `tests/test_mcp_resources.py` (6 tests), `tests/test_mcp_resource_cache.py` (5 tests), `tests/test_mcp_resources_auth.py` (4 tests including tenant-leakage regression).
- **ADR-0030** (`docs/adr/0030-mcp-resources-uri-scheme.md`): URI scheme rationale, 7 kinds, MIME-native content negotiation, cache architecture, sentinel handling.

### F-FINAL gate followups

- **Pre-launch checklist** (AC-6): §6 updated to 28 tools, §6.5 added (7 MCP Resources sign-off table).
- **ADR-0023 pagination amendment** (AC-7): `start_index` parameter contract, continuation hint grammar (plain text, not `<error>` tag), `[ref=fN]` row token alignment.
- **README + CHANGELOG** (AC-8): MCP section updated to 28 tools + 7 Resources table; this entry.
- **Tenant leakage fix** (latent bug): All 7 resource handlers now resolve version sentinel before forming cache key; regression test `test_two_keys_different_active_versions_get_their_own_bodies` added to `tests/test_mcp_resources_auth.py`.

---

## [0.4.1] — 2026-05-16 — M9 follow-up: Web UI parity for repo & profile management

5 WIs merged via PR #116.

### Added (M9 follow-up: Web UI parity)

- `PATCH /api/repos/repos/{id}` — edit URL/branch/ssh_key_id/local_path qua Web UI; preserves `head_sha` (incremental indexer compatible). ADR-0024.
- `PATCH /api/repos/profiles/{id}` — edit name/version/description; rejects `name`/`version` change on indexed profiles (HTTP 409 `ProfileIndexedError`); enforces ancestor + descendant version-match invariant (HTTP 422). ADR-0024.
- Admin UI: Edit Repo form, Edit Profile form, profile hierarchy tree view (toggle flat/tree, localStorage persist).
- RepoTable surfaces `clone_error_msg`, `error_msg`, `last_indexed_at` columns.
- Index + Index-All buttons: `--full` checkbox (expose ADR-0007 cleanup flag).
- Audit log captures before/after snapshots for PATCH mutations (ADR-0021 extension).

### Fixed

- TOCTOU race in `update_repo` UNIQUE check — catch `psycopg2.errors.UniqueViolation` → HTTP 409 instead of 500.
- ProfileTree.astro testid clash with flat list (namespaced `profile-tree-*`).
- ProfileTree.astro client-side DOM build → SSR template (Astro convention parity).

### Tests

- +9 backend tests for PATCH endpoints (empty body, single field, indexed guard, ancestor/descendant version match, concurrent UniqueViolation).
- +5 browser tests for tree view toggle and localStorage persistence.

---

## [0.4.0] — 2026-05-15 — M9 "Auth Wow" + M8 cleanup + comprehensive security hardening

19 worktrees merged via 9-phase orchestration. PR #100.

### Added — Auth Wow features

- **OAuth (Google + GitHub)** via `arctic` + `oslo` in Astro SSR. State + PKCE CSRF protection. Account linking on verified email. ADR-0017.
- **Public signup** (`/signup`) with email verification (256-bit token, 24h TTL, single-use), hCaptcha, 3/hour resend rate-limit, HTML-escaped email templates.
- **MFA TOTP** enrollment via `pyotp` with Fernet-encrypted secrets + 10 HMAC-hashed backup codes. Admin user enforced after 7-day grace. ADR-0022.
- **Multi-user admin** (`/admin/users`) — `is_admin` gating, deactivate (revokes sessions), reactivate, reset-password-link (1h TTL token).
- **Tenant API keys** — `user_id` FK scoping; users see only their own keys, admin sees all. `expires_at` filter.
- **Backup CLI bundle** (`.tar.gz`: postgres.sql + neo4j.dump + fernet.enc passphrase-encrypted + manifest.json) + Web UI trigger with SSE log stream. ADR-0018.
- **Restore upload** (`/api/operations/restore`) with full OWASP 10-item checklist: size, content-type, extension, `tarfile.extractall(filter='data')`, disk space, SHA-256 audit, maintenance mode 503, pre-restore safety backup, admin + fresh-MFA (5 min). ADR-0019.
- **Admin audit log** (`admin_audit_log` table) + `@audit_action` decorator + `audit_cli` context manager. 18+ routes covered. ADR-0021.

### Added — Security hardening (30+ findings closed)

- **F1**: Login dummy-hash unconditional bcrypt verify (timing oracle fix — closes username enumeration).
- **F2**: Postgres-backed `login_attempts` rate-limit (multi-worker safe, survives restart).
- **F3**: `TRUSTED_PROXY_CIDRS` env allowlist for `X-Forwarded-For` parsing (prevents IP spoofing).
- **F5**: OAuth `state` + PKCE mandatory.
- **F6**: CSP + Permissions-Policy headers in nginx + Caddyfile parity.
- **F7**: Server-side session store (`active_sessions` table) — instant revoke on logout + session ID rotation on login.
- **F8**: API key hash HMAC-SHA256 (was SHA-256 plain) + 30-day SHA-256 fallback for legacy keys (deadline 2026-06-15).
- **F12**: FERNET startup fail-fast in production if key unset.
- **F13**: `--old-key-env` / `--new-key-env` for `rotate-fernet` (eliminates `/proc/<pid>/cmdline` leak). Atomic rotation with transaction rollback. ADR-0020.
- **F15**: `WEBUI_SECURE_COOKIE` opt-out (`!= "0"` instead of `== "1"`).
- **F20**: `conftest._bypass_webui_auth_for_legacy_tests` now excludes both `test_web_ui_auth.py` AND `test_web_ui_browser.py` (was silent auth bypass).

### Added — DB schema

- 8 new yoyo migrations: `m9_001_oauth_columns`, `m9_002_api_keys_user_fk`, `m9_003_admin_audit_log`, `m9_004_login_attempts`, `m9_005_active_sessions`, `m9_006_email_verifications`, `m9_007_totp_secrets`, `m9_008_key_rotation_log`. `9001_m9_user_mgmt.sql` harmonized as canonical schema.

### Added — UI

- `/admin/users` (list + deactivate + reactivate + reset password).
- `/admin/security` (TOTP enrollment + backup codes).
- `/signup`, `/verify-email`, `/reset-password` (public, prerender=false).
- `/admin/operations` extended: Backup section with SSE log, Restore section with file upload + safety backup display, Migrations read-only display (yoyo `_yoyo_migrations` table), FERNET rotation CLI placeholder.
- `/admin/repos` extended: per-profile parent dropdown (handles 404/422 typed errors from W-RC), "Clone all pending" button + JobStatus wiring, RepoTable SSH key dropdown JS toggle by URL pattern (`git@` → show, `https://` → hide).
- Login page: OAuth "Sign in with Google/GitHub" buttons + MFA step section.

### Added — CLI

- `python -m src.manager` new subcommands: `delete-profile <name>`, `delete-repo <id|url>`, `delete-webui-user <username>`, `list-webui-users`. All deletes require `--yes` or interactive `YES` confirm + write audit log.
- `create-webui-user --admin` flag (bootstraps admin user post-M9 schema where `is_admin DEFAULT FALSE`).

### Added — REST polish

- `POST /api/repos/profiles/{id}/clone-all` returns 404 for nonexistent profile (was 200 "no pending repos").
- `PATCH /api/repos/profiles/{id}/parent` distinguishes 404 (not found) vs 422 (cycle / version mismatch) via typed exceptions (`ProfileNotFoundError`, `ProfileCycleError`, `ProfileVersionMismatchError` in `src/db/exceptions.py`).
- `GET /api/admin/migrations` lists applied yoyo migrations (read-only, admin-gated).

### Added — CI / DX

- Bump `actions/setup-node@v4 → v5`, `pnpm/action-setup@v4 → v5`, `actions/checkout@v4 → v5` (pre-empts GitHub forced Node 24 upgrade — deadline 2026-06-02).
- Replace `python -m jsonschema` with `check-jsonschema` CLI (eliminates DeprecationWarning).
- Add `actionlint` job via `rhysd/actionlint@v1`.
- Top-level `permissions: contents: read` on all workflows (anti-pattern fix).
- `.github/dependabot.yml` for weekly GitHub Actions updates.
- 2 advisory lint scripts: `lint_json_response.sh` (catches `JSONResponse(dict)` missing `_json_safe`), `lint_fetch_content_type.sh` (catches `fetch()` POST/PATCH/DELETE missing `Content-Type` header). Wired into `make lint` as `lint-shell-advisory` (warn-only — 127 legacy JSONResponse violations tracked in backlog for dedicated cleanup PR; lint_fetch_content_type 0 violations).
- New ADRs: 0017 (OAuth), 0018 (backup contract), 0019 (restore upload security), 0020 (FERNET key delivery), 0021 (admin audit log), 0022 (MFA TOTP).

### Changed — Test debt

- Deleted 8 MIGRATED tombstone test files (`test_web_ui_*_browser.py` — coverage moved to `tests/browser/admin/test_repos.py` in M8 W7).
- Fixed httpx per-request cookies + Neo4j session close deprecation warnings (2 of 3 fixed; remaining 1 is documented upstream).
- 656 unit tests + 360 postgres integration tests + 68 neo4j tests pass.

### Operational

- Production runbook `docs/deploy/m9-postmerge-ops.md`: 99.0 test artifact cleanup, index-core v9-v19 re-run, seed-patterns, admin bootstrap, audit log verification, daily cleanup cron (login_attempts, email_verifications, active_sessions).

### Fixed

- `[FIX] indexer: replace urllib with httpx for true wall-clock timeout, fix indexer freeze when embed backend slow/silent`

### Security

- **`site/`: bump `astro` 5.x → 6.x and `@astrojs/node` 9.x → 10.x.** Closes 5 dependabot alerts (CVE-2026-42570 / 45028 / 41067 / 41322 / 29772). Major bump required — Astro 5.x and @astrojs/node 9.x are EOL with no CVE backports.
  - `devalue` pinned to `^5.8.1` via `pnpm-workspace.yaml` `overrides` (transitive — astro 6 still pulls 5.8.0 by default).
  - **Deploy upgrade required:** Node.js ≥ 22.12.0 (was 20+), pnpm ≥ 10 (was 9+). `pnpm-workspace.yaml` now uses `allowBuilds:` + `overrides:` fields (pnpm 10+ format).
  - CI bumped: Node 20 → 22, pnpm 9 → 10 in `.github/workflows/ci.yml`.

## [0.3.0] — 2026-05-14 — M8 "Public Wow"

### Breaking Changes

- **Web UI rewritten as Astro SSR (port 4321 default).** FastAPI dropped all Jinja2 templates and now returns JSON only (port 8003).
  - Deployers must add `odoo-semantic-astro.service` (systemd unit provided at `docs/deploy/odoo-semantic-astro.service`) and run `pnpm build` in `site/` before starting.
  - Nginx config: use `docs/deploy/nginx-m8.conf` — routes `/api/*` → 8003, `/admin/*` + `/` → 4321, `/mcp` → 8002.
  - Direct browser requests to `/api/*` now return `Content-Type: application/json` — no HTML pages served from FastAPI.

### Added

- **Astro 5.x SSR server** (`output: 'server'`, Tailwind CSS, pnpm) in `site/`
- **6 admin pages** SSR-rendered by Astro: login, dashboard, repos, api-keys, ssh-keys, operations
- **AdminLayout** Astro component + Astro middleware session auth (`GET /api/auth/verify` → 401 → redirect `/admin/login`)
- **Landing page** with React Flow `GraphAnimation` island + cinematic 5-frame hero reveal; baked graph snapshot (`site/public/graph-snapshot.json` from `scripts/dump_graph_snippet.py`)
- **Public install page** at `/install/` — Astro SSR, API-key onboarding flow
- **Pricing placeholder page** at `/pricing/` — teaser for M9 SaaS tiers
- **68 browser tests** (Playwright) split across `tests/browser/admin/` (auth-gated flows) + `tests/browser/public/` (landing + install page); 2 parallel CI jobs (`browser-admin`, `browser-public`)
- **ADR-0014** Astro unified UI architecture decision
- **ADR-0015** FastAPI pure JSON API policy
- **ADR-0016** Profile hierarchy + Neo4j Option Y isolation (`parent_profile_id` FK, ancestor array, cycle-free validation) — renumbered from draft 0014 to avoid clash with Astro ADR
- **`_json_safe` helper** (`src/web_ui/utils.py`) for safe `datetime` → ISO string conversion in `JSONResponse` — prevents 500 errors on datetime-bearing objects
- **`/api/jobs/{id}/status` endpoint** extracted to dedicated jobs router (`src/web_ui/routers/jobs.py`)
- **CI Node 20** setup via `actions/setup-node@v4` + `pnpm/action-setup@v3`; `pnpm run check` (TypeScript + Astro type-check) added as required CI gate
- **Auto-seed 26 master data profiles** via `python -m src.db.migrate`: Odoo CE v8–v19, Standard Viindoo v8–v19, Viindoo Internal v17/v18 (48 repos total, `clone_status='manual'`)
- **CLI `seed-master-data`**: idempotent re-seed with `--profiles-only` / `--reset` flags
- **Upgrade runbook** `docs/deploy/master-data-upgrade.md`

### Removed

- All Jinja2 templates (`src/web_ui/templates/*.html`)
- `jinja2` dependency from `pyproject.toml`
- Direct HTML rendering from any FastAPI route

### Fixed (during M8)

- **Astro 5.x `checkOrigin` security:** all mutation fetches in Astro pages now send `Content-Type: application/json` (Astro 5 rejects requests without this header for CSRF protection)
- **Session datetime serialization 500** in `/api/dashboard/stats` and SSH key listing — root cause: `datetime` objects not JSON-serializable in `JSONResponse`; fixed with `_json_safe` wrapper
- **Logout endpoint missing** — `POST /api/auth/logout` added; Astro logout page wired correctly

## [0.2.0] — 2026-05-12

### M7.5 "Persona Wow"

**Track 1 — TRIGGER/PREFER/SKIP docstrings**
- Rewrote all 14 MCP tool docstrings with structured routing blocks (`TRIGGER when:`, `PREFER over:`, `SKIP when:`) so AI clients auto-pick the right tool from natural-language utterances (EN + VN)
- Added `tests/test_mcp_tool_descriptions.py` — enforces all 14 tools have TRIGGER/PREFER/SKIP and descriptions ≤ 1500 chars
- Extended `tests/test_smoke_e2e_mcp_http.py` with stub coverage for 11 previously uncovered tools

**Track 2 — Claude Code plugin package**
- New `dist/odoo-semantic-plugin/` — installable Claude Code plugin with:
  - 11 persona SKILL.md files: CEO (risk-overview, customization-inventory), Developer (override-finder, deprecation-audit, version-diff), Consultant (feature-check, gap-analysis), Marketer (feature-highlights, addon-diff), Sales (capability-proof, objection-handler)
  - 2 sub-agent files: `odoo-router.md` (Haiku classifier) + `odoo-upgrade-planner.md` (Sonnet orchestrator)
  - `/odoo-semantic:connect` slash command for interactive API-key setup
  - `.mcp.json` template with `${ODOO_SEMANTIC_API_KEY}` env interpolation
- New `dist/marketplaces/viindoo/marketplace.json` for self-host distribution
- Added `tests/test_skill_disambiguation.py` — 31/31 parametrized routing accuracy tests (100%)

**Track 3 — Cross-vendor adapters + persona docs**
- New `dist/gemini-gem-instructions.md` — Gemini Gem system instructions with full tool routing for all 14 tools + 5 persona modes
- New `dist/openai-gpt-instructions.md` — Custom GPT instructions with routing rules + OpenAPI Action schema
- New `dist/cursor-rules.md` — Cursor `.cursorrules` with file-type-based auto-triggers for Odoo files
- New `docs/personas/{ceo,dev,consultant,marketer,sales}.md` — 5 EN persona onboarding guides with sample prompts and tool workflows
- Updated `README.md` — added Persona Guides section with cross-vendor adapter links

**Track 4 — Architecture & checklist**
- New `docs/adr/0012-persona-skill-architecture.md` — ADR for TRIGGER protocol + persona skill approach + rejected alternatives
- Extended `docs/deploy/pre-launch-checklist.md` — 11 persona skill sign-off rows in §6

## [0.1.0] — 2026-05-11

- M1–M7 Complete: resolve_model, resolve_field, resolve_method, resolve_view, find_examples, impact_analysis, lookup_core_api, api_version_diff, find_deprecated_usage, lint_check, cli_help, suggest_pattern, check_module_exists, find_override_point
- API key auth + Web UI admin (M5)
- SSH auto-clone, incremental indexer, cross-profile parallel indexing (M6)
- Qualified-name AST scope resolver, yoyo-migrations, Web UI session auth, nightly recall benchmark, go-live docs (M7)
