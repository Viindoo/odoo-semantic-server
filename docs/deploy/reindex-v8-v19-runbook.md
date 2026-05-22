# Reindex Runbook — v8→v19 Full DB-Impact Wave (PR #160)

> Operations to execute on **production server** after PR #160 is deployed.
> Run sequentially. Each section has the exact command + expected outcome + verification.
>
> **Context:** PR #160 ("reindex-prep DB-impact wave v8→v19") introduces 6 parser/indexer fixes
> that are behavior-preserving for existing graph data but require a full reindex to populate
> new nodes and correct mis-classified nodes:
>
> - WI-1: v18/v19 generic field classes (`Field[int]`) now classify as `kind='field_type'`
> - WI-2: v8/v9 `parser_cli` now produces `CLICommand` nodes (previously 0)
> - WI-3: LESS parser — `:Stylesheet {language: "less"}` nodes + `:IMPORTS` edges for v8-v11
> - WI-4: curated `odoo.tools` CoreSymbols + `_DEPRECATED_API_SYMBOLS` 14 → 19 entries
> - WI-5: lint rules ≥50/version for v10-v19 (JSON curation; picked up on next `index-core` run)
> - WI-6: `VersionRegistry` (ADR-0032) — behavior-preserving refactor, no data migration needed
>
> **Placeholder conventions:**
> - `<VENV>` = `~/.venv/odoo-semantic-mcp/bin/python`
> - `<ODOO_SRC_vN>` = path to checked-out Odoo source for version N (e.g. `~/git/odoo17`, auto-clone path from webui)
> - `<NEO4J_PASSWORD>` = set as env var: `export NEO4J_PASSWORD=<your-password>`
>
> **Start time:** ___________
> **Operator:** ___________

---

## Pre-flight Checks

- [ ] Pull latest code and install deps:
  ```bash
  git -C /opt/odoo-semantic-mcp pull
  <VENV> -m pip install -e ".[all]" --quiet
  ```

- [ ] All 3 systemd services running:
  ```bash
  systemctl status odoo-semantic-mcp odoo-semantic-webui odoo-semantic-astro
  ```
  Expected: `active (running)` for all three.

- [ ] Create safety backup before reindex:
  ```bash
  <VENV> -m src.cli backup \
      --output ~/backups/pre-rp-reindex-$(date +%Y%m%d-%H%M%S).tar.gz
  ```
  Expected: exits 0, `.tar.gz` file created under `BACKUP_DIR`.

- [ ] Note current CoreSymbol count per version (baseline for post-reindex comparison):
  ```bash
  cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
      "MATCH (c:CoreSymbol)
       RETURN c.odoo_version AS version, count(c) AS symbols
       ORDER BY toFloat(version) ASC;"
  ```
  Record result here: ___________

---

## 1. Cypher Cleanup — Pre-reindex (2 min)

Remove known-stale nodes BEFORE reindex to avoid re-merging into stale data.

**1a. Remove `snap_mod` test artifact (odoo_version='96.0'):**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (m:Module {odoo_version: '96.0', name: 'snap_mod'})
     DETACH DELETE m
     RETURN count(m) AS deleted_test_artifacts;"
```
Expected: `deleted_test_artifacts = 1` (or 0 if already removed).

**1b. Remove pre-v14 `__unresolved__` OWLComp stubs (239 anachronisms):**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (oc:OWLComp)
     WHERE oc.odoo_version IN ['8.0','9.0','10.0','11.0','12.0','13.0']
       AND oc.module = '__unresolved__'
     DETACH DELETE oc
     RETURN count(oc) AS deleted_anachronisms;"
```
Expected: `deleted_anachronisms = 239` (or 0 if already cleaned up from M10 ops).

**Result:** [ ] snap_mod = 0; pre-v14 OWLComp stubs = 0

---

## 2. Full Re-index Core Symbols v8-v19 (20-40 min)

Re-runs `index-core` for all versions. Picks up:
- WI-1: v18/v19 generic field classes (`Field[int]` Subscript) classified as `field_type`
- WI-2: v8/v9 CLICommand nodes from `openerp/` paths + static commands JSON
- WI-4: curated `odoo.tools` CoreSymbols (12 `tools_symbols_X.0.json` files) + `_DEPRECATED_API_SYMBOLS` 19 entries
- WI-5: lint rules ≥50/version for v10-v19

CLI verified: `python -m src.indexer index-core --help` shows `--source SOURCE --version VERSION [--static-data-dir STATIC_DATA_DIR]`.

```bash
for V in 8 9 10 11 12 13 14 15 16 17 18 19; do
    ODOO_SRC=<ODOO_SRC_v${V}>   # e.g. ~/git/odoo${V} or auto-clone path
    [ -d "$ODOO_SRC" ] || { echo "SKIP: $ODOO_SRC not found"; continue; }
    echo "=== index-core Odoo v${V}.0 ===" >&2
    <VENV> -m src.indexer index-core \
        --source "$ODOO_SRC" \
        --version "${V}.0" || {
        echo "ERROR: index-core v${V}.0 failed. Investigate before continuing." >&2
        exit 1
    }
done
echo "All index-core runs complete"
```

**Verification:**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (c:CoreSymbol)
     RETURN c.odoo_version AS version, count(c) AS symbols
     ORDER BY toFloat(version) ASC;"
```
Expected: 12 rows (v8.0-v19.0), each with `symbols > 0`.

Alert: if any version shows a drop >20% from the pre-flight baseline, suspect a path refactor.
- v8/v9: check `openerp/` prefix in source tree.
- v19: check `odoo/orm/` split — `parser_odoo_core._resolve_core_paths()` has fallback.
- See `docs/adr/0005-core-coverage-version-paths.md`.

**Spot-check odoo.tools.SQL availability (WI-4):**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (c:CoreSymbol {qualified_name: 'odoo.tools.SQL', odoo_version: '17.0'})
     RETURN c.status AS status, c.qualified_name AS qname;"
```
Expected: `status = 'stable'`, `qname = 'odoo.tools.SQL'`.

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (c:CoreSymbol {qualified_name: 'odoo.tools.SQL', odoo_version: '16.0'})
     RETURN count(c) AS count;"
```
Expected: `count = 0` (correct — SQL is absent in v16; the tool returns "not found in indexed Odoo core for version 16.0").

**Spot-check field_type fix for v18/v19 (WI-1):**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (c:CoreSymbol)
     WHERE c.odoo_version IN ['18.0','19.0'] AND c.kind = 'field_type'
       AND (c.qualified_name ENDS WITH '.Integer'
         OR c.qualified_name ENDS WITH '.Many2one'
         OR c.qualified_name ENDS WITH '.Char'
         OR c.qualified_name ENDS WITH '.Float')
     RETURN c.odoo_version AS version, c.qualified_name AS qname, c.kind AS kind
     ORDER BY version, qname;"
```
Expected: ≥8 rows (4 field types × 2 versions), all `kind = 'field_type'`.

**Spot-check CLICommand for v8/v9 (WI-2):**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (c:CLICommand)
     WHERE c.odoo_version IN ['8.0','9.0']
     RETURN c.odoo_version AS version, count(c) AS cmd_count;"
```
Expected: non-zero `cmd_count` for both v8.0 and v9.0 (previously 0).

**Result:** [ ] all 12 versions indexed; odoo.tools.SQL correct; field_type v18/v19 correct; CLICommand v8/v9 non-zero

---

## 3. Full Re-index All Repos (30-90 min, run off-peak)

Backfills new properties + indexes LESS stylesheets for v8-v11. Picks up:
- WI-3: LESS parser — `:Stylesheet {language: "less"}` nodes + `:IMPORTS` edges + `chunk_type='less'` pgvector embeddings
- Carry-over from M10/M10.5: `f.comodel_name` (PR #156) and `mth.depends` (PR #158) for any repos not yet fully indexed

CLI verified: `python -m src.indexer index-repo --help` confirms `--all`, `--full`, and `--no-embed` flags exist.

```bash
<VENV> -m src.indexer index-repo --all --full --no-embed
```

> `--full` bypasses incremental `head_sha` skip (ensures LESS files are scanned for v8-v11 repos).
> `--no-embed` skips pgvector re-embed here — run `reembed-stubs` in step 4 separately.

**Verification:**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (s:Stylesheet {language: 'less'})
     RETURN s.odoo_version AS version, count(s) AS stylesheet_count
     ORDER BY toFloat(version) ASC;"
```
Expected: **v9.0, v10.0, v11.0 > 0**; **v8.0 = 0 (correct — vendored Bootstrap only)**; zero for v12.0+ (LESS → SCSS cutover at v12).

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (src:Stylesheet {language:'less'})-[:IMPORTS]->(tgt:Stylesheet)
     RETURN count(*) AS import_edge_count;"
```
Expected: > 0 (at least one LESS `@import` chain resolved).

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (f:Field) WHERE f.comodel_name IS NOT NULL
     RETURN f.odoo_version AS version, count(f) AS fields_with_comodel
     ORDER BY toFloat(version) DESC;"
```
Expected: non-zero rows for each version with relational fields (v10+).

**Result:** [ ] LESS nodes present for v8-v11; IMPORTS edge count > 0; comodel_name populated

---

## 4. Re-embed Stubs (run overnight, off-peak)

Re-embeds modules with `field_count > 0` but `embeddings_count == 0`. Includes:
- New LESS stylesheet chunks (chunk_type='less') from WI-3
- Any modules previously missed by the embedder

CLI verified: `python -m src.indexer reembed-stubs --help` shows `--profile PROFILE` (required).
The `src.manager list` command prints one `[<profile>] odoo_version=...` line per profile.

```bash
for PROFILE in $(<VENV> -m src.manager list | grep -oP '^\[\K[^\]]+'); do
    echo "=== reembed-stubs: $PROFILE ===" >&2
    <VENV> -m src.indexer reembed-stubs --profile "$PROFILE"
done
```

**Verification:**
```bash
docker compose exec postgres psql -U odoo_semantic -c "
SELECT COUNT(*) AS zero_embed_modules
FROM (
    SELECT e.module_name, e.odoo_version
    FROM (SELECT DISTINCT module_name, odoo_version FROM embeddings) e
    LEFT JOIN (SELECT module_name, odoo_version, count(id) AS ecnt
               FROM embeddings GROUP BY module_name, odoo_version) ec
        USING (module_name, odoo_version)
    WHERE coalesce(ec.ecnt, 0) = 0
) AS t;"
```
Expected: `zero_embed_modules = 0` (or low, for newly indexed but not yet embedded modules).

**Result:** [ ] zero-embed modules count acceptable

---

## 5. Post-Reindex Verify Block

### 5.1 CoreSymbol counts per version (Cypher)

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (c:CoreSymbol)
     RETURN c.odoo_version AS version, count(c) AS symbols
     ORDER BY toFloat(version) ASC;"
```
Alert rule: if any version drops >20% vs its neighbours in the list, suspect a path refactor.
- Particularly watch v8/v9 (`openerp/` source) and v19 (`odoo/orm/` split).

### 5.2 `odoo.tools` MCP smoke (WI-4)

```bash
# Replace <API_KEY> and <MCP_HOST> with real values:
curl -s -X POST "https://<MCP_HOST>/mcp" \
    -H "X-API-Key: <API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
          "name":"lookup_core_api",
          "arguments":{"name":"odoo.tools.SQL","odoo_version":"16.0"}}}' \
    | python3 -m json.tool | grep -E "not found|not.available"
```
Expected: response text contains `"not found in indexed Odoo core for version 16.0"` (SQL is absent in v16).

```bash
curl -s -X POST "https://<MCP_HOST>/mcp" \
    -H "X-API-Key: <API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
          "name":"lookup_core_api",
          "arguments":{"name":"odoo.tools.SQL","odoo_version":"17.0"}}}' \
    | python3 -m json.tool | grep -E "status|stable"
```
Expected: `status` = `"stable"`.

### 5.3 LESS stylesheet nodes (WI-3)

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (s:Stylesheet {language: 'less'})
     WHERE s.odoo_version IN ['8.0','9.0','10.0','11.0']
     RETURN s.odoo_version AS version, count(s) AS count
     ORDER BY version;"
```
Expected: **v9.0, v10.0, v11.0 > 0**; **v8.0 = 0 (correct — all v8 LESS is vendored Bootstrap, not module LESS source)**. v8 legitimately produces zero `:Stylesheet {language:'less'}` nodes; v9-v11 have real Odoo module LESS source. LESS → SCSS cutover happened at v12 (not v11).

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH ()-[:IMPORTS]->(s:Stylesheet {language:'less'})
     RETURN count(*) AS import_edges;"
```
Expected: > 0 (at least one LESS `@import` chain resolved in v9-v11).

### 5.4 field_type v18/v19 (WI-1)

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (c:CoreSymbol)
     WHERE c.odoo_version IN ['18.0','19.0'] AND c.kind = 'field_type'
     RETURN c.odoo_version AS version, count(c) AS field_type_count;"
```
Expected: count matches v17 field_type count approximately (same classes exist). Flag if either version returns 0.

### 5.5 Null-profile nodes (carry-over check)

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (n) WHERE n.profile IS NULL RETURN labels(n) AS lbl, count(n) AS cnt LIMIT 10;"
```
Expected: 0 rows (or only legacy nodes from before ADR-0016 profile enforcement).

### 5.6 `__unresolved__` OWLComp post-cleanup

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (oc:OWLComp {module:'__unresolved__'})
     WHERE oc.odoo_version IN ['8.0','9.0','10.0','11.0','12.0','13.0']
     RETURN count(oc) AS count;"
```
Expected: 0 (cleaned in step 1b; new reindex should not create any due to WI-6 parser guard).

### 5.7 MCP smoke — superset tools + ORM tools

```bash
# model_inspect smoke
curl -s -X POST "https://<MCP_HOST>/mcp" \
    -H "X-API-Key: <API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
          "name":"model_inspect",
          "arguments":{"model":"sale.order","method":"summary","odoo_version":"17.0"}}}' \
    | python3 -m json.tool | grep -E '"text"|\bsale.order\b'
```
Expected: tree output containing `sale.order` summary.

```bash
# resolve_orm_chain smoke
curl -s -X POST "https://<MCP_HOST>/mcp" \
    -H "X-API-Key: <API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{
          "name":"resolve_orm_chain",
          "arguments":{"model":"sale.order","dotted_path":"partner_id.country_id.code","odoo_version":"17.0"}}}' \
    | python3 -m json.tool | grep -E '"text"|BROKEN|country_id'
```
Expected: hop resolution output (not BROKEN).

```bash
# validate_domain smoke
curl -s -X POST "https://<MCP_HOST>/mcp" \
    -H "X-API-Key: <API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{
          "name":"validate_domain",
          "arguments":{"model":"sale.order","domain":"[(\"state\", \"=\", \"sale\")]","odoo_version":"17.0"}}}' \
    | python3 -m json.tool | grep -E '"text"|VALID|ERROR'
```
Expected: VALID for this term.

```bash
# resolve_stylesheet smoke (LESS v9)
curl -s -X POST "https://<MCP_HOST>/mcp" \
    -H "X-API-Key: <API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{
          "name":"resolve_stylesheet",
          "arguments":{"module":"web","odoo_version":"9.0"}}}' \
    | python3 -m json.tool | grep -E '"text"|less|stylesheet'
```
Expected: output showing `.less` file paths (v9 uses LESS).

---

## 5.8 Post-wave3 new data verify (M13 pre-reindex wave)

Run after applying migration m13_001 + m13_002 and full reindex.

**5.8a — Module license/copyright_owner populated:**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (m:Module)
     WHERE m.license IS NOT NULL
     RETURN m.odoo_version AS version, count(m) AS licensed_count
     ORDER BY toFloat(version) ASC
     LIMIT 15;"
```
Expected: non-zero rows per version (v8 base AGPL-3; v9-v11 mostly LGPL-3 key missing → AGPL-3 fallback; v12+ explicit LGPL-3).

**5.8b — OEEL-1 modules NOT served + carry license_notice:**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (m:Module {license: 'OEEL-1'})
     RETURN m.odoo_version AS version, m.name AS module, m.license_notice AS notice
     ORDER BY toFloat(version) ASC, m.name ASC;"
```
Expected: rows for known OEEL-1 modules (v15/v16: `l10n_it_edi_website_sale`; v17: `account_payment_term` + `l10n_it_edi_website_sale`; v18: `certificate`, `l10n_hr_edi`, `l10n_it_edi_website_sale`, `l10n_jo_edi_pos`, `project_hr_skills`; v19: same minus `l10n_it_edi_website_sale`). Each row must have a non-null `license_notice`. These modules are NOT served in MCP tool output — verify that `model_inspect` on a model defined only in `account_payment_term` (v17) returns an empty result or license_notice only.

**5.8c — embeddings.profile_name populated (no unexpected NULLs for new chunks):**
```bash
docker compose exec postgres psql -U odoo_semantic -c "
SELECT profile_name, count(*) AS chunk_count
FROM embeddings
GROUP BY profile_name
ORDER BY chunk_count DESC
LIMIT 20;"
```
Expected: chunks written after the reindex carry non-NULL `profile_name`. Legacy chunks (written before m13_001) may still have NULL — those will be backfilled on next `reembed-stubs` run per profile.

**5.8d — :LintViolation nodes v15+ > 0 with :HAS_VIOLATION edge:**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (v:View)-[:HAS_VIOLATION]->(lv:LintViolation)
     WHERE v.odoo_version >= '15.0'
     RETURN v.odoo_version AS version, count(lv) AS violations
     ORDER BY toFloat(version) ASC;"
```
Expected: non-zero for v15.0, v16.0, v17.0, v18.0, v19.0. v18/v19 violations may include `<list>` views validated against `list_view.rng` (Odoo renamed `<tree>` → `<list>`; RNG is read version-exact from the indexed Odoo core source at index time).

---

## 5.9 Repos-hygiene normalize (post-wave3)

The wave3 PR changes repo registration semantics. Audit existing rows for consistency.

**5.9a — Normalize local_path derivation:**
Existing repos may have user-supplied `local_path` values that differ from the new server-derived convention. Query:
```bash
docker compose exec postgres psql -U odoo_semantic -c "
SELECT id, url, branch, local_path, profile_id
FROM repos
WHERE local_path IS NOT NULL
ORDER BY id;"
```
Review each row. If `local_path` was manually set and differs from what the server would derive (URL+branch-based), decide whether to update or leave as-is (both are supported — the column is informational for the cloner, not a unique key).

**5.9b — Review cross-profile (url, branch) duplicates:**
The UNIQUE constraint was narrowed from `(url, branch)` to `(url, branch, profile_id)`. This allows the same repo to be registered under multiple profiles. Verify intentional duplicates:
```bash
docker compose exec postgres psql -U odoo_semantic -c "
SELECT url, branch, count(*) AS profile_count, array_agg(profile_id) AS profiles
FROM repos
GROUP BY url, branch
HAVING count(*) > 1
ORDER BY url, branch;"
```
Each duplicate must represent a deliberately multi-profile registration (e.g. the same repo indexed under both a shared base and a tenant overlay profile). Unexpected duplicates should be reviewed and de-duplicated via `DELETE FROM repos WHERE id = <stale_id>`.

---

## Known Constraints (post-wave3)

### MED-2 — Private forges require manual known_hosts onboarding

`StrictHostKeyChecking=yes` is now enforced (replaces `accept-new`). GitHub, GitLab, and Bitbucket SSH host keys are pre-pinned in the bundled known_hosts. **Self-hosted or other forges are not pre-pinned** — any clone attempt against an unpinned host will be rejected with a `Host key verification failed` error.

**Resolution (per-host, one-time):**
1. Obtain the SSH host key fingerprint from the forge admin:
   ```bash
   ssh-keyscan -H <your-forge-hostname> 2>/dev/null
   ```
2. Append the output to `src/git_utils.py`'s pinned known_hosts constant (or the configured known_hosts file path).
3. Restart the MCP service: `sudo systemctl restart odoo-semantic-mcp`.
4. Verify clone succeeds for a test repo on that forge.

This is a one-time step per forge host. Once pinned, subsequent clones for any repo on that forge require no further action.

---

## 6. Post-Ops Verification Checklist

| Item | Expected | Checked |
|------|----------|---------|
| Safety backup created | `.tar.gz` file under `BACKUP_DIR` | [ ] |
| `snap_mod` (96.0) node | 0 nodes | [ ] |
| Pre-v14 `__unresolved__` OWLComp | 0 nodes | [ ] |
| CoreSymbol count per version (v8-v19) | > 0 for all; no version drops >20% vs neighbours | [ ] |
| `odoo.tools.SQL` v16 | not-available | [ ] |
| `odoo.tools.SQL` v17 | stable | [ ] |
| `field_type` count v18/v19 | Non-zero, comparable to v17 | [ ] |
| CLICommand v8/v9 | > 0 | [ ] |
| `:Stylesheet {language:'less'}` v9-v11 | > 0 for v9/v10/v11; **v8 = 0 (correct — vendored only)** | [ ] |
| `:IMPORTS` edges for LESS | > 0 | [ ] |
| `f.comodel_name` populated | Non-zero for v10+ relational fields | [ ] |
| `mth.depends` populated | Non-zero for v10+ compute methods | [ ] |
| NULL-profile nodes | 0 (or only pre-ADR-0016 legacy) | [ ] |
| `reembed-stubs` complete | zero-embed modules = 0 (or low) | [ ] |
| `model_inspect` smoke | Returns `sale.order` summary | [ ] |
| `resolve_orm_chain` smoke | Returns hop resolution, not BROKEN | [ ] |
| `validate_domain` smoke | Returns VALID for simple domain | [ ] |
| `resolve_stylesheet` smoke (v9 LESS) | Returns `.less` file entries | [ ] |
| Module.license populated (5.8a) | Non-zero per version | [ ] |
| OEEL-1 modules carry license_notice (5.8b) | Per-version list present; not served in tool output | [ ] |
| embeddings.profile_name populated (5.8c) | New chunks have non-NULL profile_name | [ ] |
| :LintViolation v15+ > 0 with :HAS_VIOLATION (5.8d) | Non-zero for v15-v19 incl. v18/v19 list views | [ ] |
| Repos local_path normalized (5.9a) | No unexpected user-supplied paths | [ ] |
| Cross-profile (url,branch) duplicates reviewed (5.9b) | All duplicates intentional | [ ] |
| MED-2 self-hosted forges (if any) | SSH host keys pinned before clone attempt | [ ] |

---

## Rollback

```bash
# Stop services:
sudo systemctl stop odoo-semantic-mcp odoo-semantic-webui

# Restore pre-ops backup:
<VENV> -m src.cli restore ~/backups/pre-rp-reindex-<TIMESTAMP>.tar.gz

# Restart:
sudo systemctl start odoo-semantic-mcp odoo-semantic-webui
```

PR #160 adds no Postgres migrations — rollback only affects Neo4j graph data (restored from backup).
Re-running `index-core` / `index-repo` after rollback is safe (MERGE is idempotent).

---

**Date completed:** ___________
**Completion time:** ___________
**Issues encountered:** ___________
**Sign-off:** ___________ (Operator) ___________ (Lead)
