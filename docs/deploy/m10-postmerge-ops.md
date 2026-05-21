# M10 Post-Merge Operations Runbook

> Operations to execute on **production server** after M10 (PR #159) is deployed.
> Run sequentially. Each section has commands + expected outcome + verification steps.
>
> **Placeholder conventions:** `<ODOO_SRC>` = path to checked-out Odoo source for that version
> (e.g. `~/git/odoo17`, `~/git/odoo_17.0`, or auto-clone path from webui); `<VENV>` =
> `~/.venv/odoo-semantic-mcp/bin/python`; `<NEO4J_PASSWORD>` = set as env var or via shell.
>
> **Start time:** ___________
> **Operator:** ___________

---

## Pre-flight Checks

- [ ] All 3 systemd services running:
  ```bash
  systemctl status odoo-semantic-mcp odoo-semantic-webui odoo-semantic-astro
  ```
  Expected: `active (running)` for all three.

- [ ] Pull latest code and install deps:
  ```bash
  git -C /opt/odoo-semantic-mcp pull
  <VENV> -m pip install -e ".[all]" --quiet
  ```

- [ ] Apply migration `m9_010` (drops audit_log legacy columns):
  ```bash
  <VENV> -m src.db.migrate
  ```
  Expected: yoyo applies `m9_010_drop_audit_legacy_columns` (or "already applied" if up to date).

- [ ] Create safety backup before reindex:
  ```bash
  <VENV> -m src.cli backup \
      --output ~/backups/pre-m10-ops-$(date +%Y%m%d-%H%M%S).tar.gz
  ```

---

## 1. Full Re-index Core Symbols v8-v19 (20-40 min)

Re-run `index-core` for all versions to pick up:
- **M10C WI-2:** `name_get` body-level DeprecationWarning detection — `status` will flip from `stable` to `deprecated` for v17+.
- Any CoreSymbol/LintRule/CLIFlag curation updates from the M9 Coverage Fill batch.

```bash
for V in 8 9 10 11 12 13 14 15 16 17 18 19; do
    ODOO_SRC=<ODOO_SRC_v${V}>   # e.g. ~/git/odoo${V} or auto-clone path
    [ -d "$ODOO_SRC" ] || { echo "SKIP: $ODOO_SRC not found"; continue; }
    echo "=== Indexing Odoo v${V}.0 ===" >&2
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
     ORDER BY toFloat(version) DESC;"
```
Expected: 12 rows (v8.0-v19.0), each with `symbols > 0`.

Alert: if any version shows a drop >20% from the previous run, suspect a path refactor - check `docs/adr/0005-core-coverage-version-paths.md`.

**Spot-check name_get deprecation (v17):**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (c:CoreSymbol {name: 'name_get', odoo_version: '17.0'})
     RETURN c.status AS status, c.qualified_name AS qname;"
```
Expected: `status = 'deprecated'` (NOT `'stable'`).

**Result:** [ ] _____ all versions indexed; name_get status=deprecated on v17

---

## 2. Full Re-index All Repos (30-90 min, run off-peak)

Backfills two new Neo4j properties introduced in v0.7.0 and v0.8.0:
- **`f.comodel_name`** (M10.5 Phase 1 — Many2one/One2many/Many2many relational comodel)
- **`mth.depends`** (M10.5 Phase 2 — `@api.depends` string args for compute methods)

Without this run, ORM-validation tools (`validate_domain`, `validate_depends`,
`validate_relation`, `resolve_orm_chain`) will have partial data.

```bash
<VENV> -m src.indexer index-repo --all --full --no-embed
```

> `--full` bypasses incremental head_sha skip; `--no-embed` skips pgvector re-embed
> (run reembed-stubs separately in step 3 to avoid double work).

**Verification (after completion):**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (f:Field) WHERE f.comodel_name IS NOT NULL
     RETURN f.odoo_version AS version, count(f) AS fields_with_comodel
     ORDER BY toFloat(version) DESC;"
```
Expected: non-zero rows for each version with relational fields (Many2one/One2many/Many2many).

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (m:Method) WHERE size(m.depends) > 0
     RETURN m.odoo_version AS version, count(m) AS methods_with_depends
     ORDER BY toFloat(version) DESC;"
```
Expected: non-zero rows for v10+ (era2 decorated computes; era1 v8/v9 have no @api.depends).

**Result:** [ ] _____ comodel_name populated; mth.depends populated

---

## 3. Cypher Cleanup — Pre-v14 OWLComp Anachronism (2 min)

239 `__unresolved__` OWLComp stubs at v8-v13 exist from pre-PR #159 indexer runs (JSPatch era3
detection triggered for pre-OWL versions). The parser guard added in PR #159 WI-1 prevents NEW
stubs from being created on future runs; this step removes the existing ones.

Also removes the test-artifact `snap_mod` node (odoo_version='96.0') left from a prior CI run.

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (oc:OWLComponent)
     WHERE oc.odoo_version IN ['8.0','9.0','10.0','11.0','12.0','13.0']
       AND oc.module = '__unresolved__'
     DETACH DELETE oc
     RETURN count(oc) AS deleted_anachronisms;"
```
Expected: `deleted_anachronisms = 239` (or 0 if already cleaned up).

```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (m:Module {odoo_version: '96.0', name: 'snap_mod'})
     DETACH DELETE m
     RETURN count(m) AS deleted_test_artifacts;"
```
Expected: `deleted_test_artifacts = 1` (or 0 if already cleaned).

**Result:** [ ] _____ pre-v14 OWLComp stubs = 0; snap_mod = 0

---

## 4. Re-embed Stubs (run overnight, off-peak)

Re-embeds modules that have `field_count > 0` but `embeddings_count == 0` — catches modules
previously skipped due to historical embedder errors or stub fields backfilled by the v8 era1
field-gap fix (M9 Coverage Fill WI-A2).

Run per profile (replace `<PROFILE>` with each registered profile name).
`src.manager list` prints one `[<profile>] odoo_version=...` header per profile;
the `grep` below extracts the bracketed profile name:
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
    SELECT e.module_name, e.odoo_version, count(emb.id) AS embed_count,
           count(f.id) AS field_count
    FROM (SELECT DISTINCT module_name, odoo_version FROM embeddings) e
    LEFT JOIN embeddings emb USING (module_name, odoo_version)
    LEFT JOIN fields f USING (module_name, odoo_version)
    GROUP BY e.module_name, e.odoo_version
    HAVING count(f.id) > 0 AND count(emb.id) = 0
) AS t;
"
```
Expected: `zero_embed_modules = 0` (or low count for modules still being embedded).

**Result:** [ ] _____ zero-embed modules count acceptable

---

## 5. Verify ORM Validation Tools (smoke, 5 min)

After steps 1-2, the ORM-validation tools should return non-error results.

```bash
# Quick MCP smoke via curl (replace <API_KEY> and <MCP_HOST>):
curl -s -X POST "https://<MCP_HOST>/mcp" \
    -H "X-API-Key: <API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"resolve_orm_chain","arguments":{"model":"sale.order","dotted_path":"partner_id.country_id.code","odoo_version":"17.0"}}}' \
    | python3 -m json.tool | grep -E '"text"|BROKEN|country_id'
```
Expected: tree output showing hop resolution (not `BROKEN`).

```bash
curl -s -X POST "https://<MCP_HOST>/mcp" \
    -H "X-API-Key: <API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"validate_domain","arguments":{"model":"sale.order","domain":"[(\"partner_id.country_id\", \"=\", \"VN\")]","odoo_version":"17.0"}}}' \
    | python3 -m json.tool | grep -E '"text"|VALID|ERROR'
```
Expected: `VALID` for all terms.

**Result:** [ ] _____ ORM tools return valid (non-error) output

---

## 6. Verify Coverage Gaps — Stylesheet LESS v8-v11 (informational, 2 min)

CSS/SCSS parser (ADR-0025) handles `.css` and `.scss` only. Odoo v8-v11 used `.less` files
which are NOT parsed. This is a known accepted gap (`.less` toolchain retired in v12+).

**Coverage note:** `:Stylesheet` nodes for v8-v11 will be absent or sparse. This is expected
behaviour — MCP stylesheet tools (`resolve_stylesheet`, `find_style_override`) are designed
for v15+ where SCSS is the canonical stylesheet format.

**No action required.** Record below for audit trail.

**Result:** [ ] _____ acknowledged; v8-v11 Stylesheet node count = 0 (acceptable)

---

## 7. Post-Ops Verification Checklist

| Item | Expected | Checked |
|------|----------|---------|
| `m9_010` migration applied | `python -m src.db.migrate` exits 0 with no unapplied migrations | [ ] |
| `name_get v17 status` | `deprecated` (not `stable`) | [ ] |
| CoreSymbol count per version | > 0 for all v8-v19 | [ ] |
| `snap_mod` (96.0) node | 0 nodes | [ ] |
| Pre-v14 `__unresolved__` OWLComp | 0 nodes | [ ] |
| `f.comodel_name` populated | Non-zero for v10+ relational fields | [ ] |
| `mth.depends` populated | Non-zero for v10+ compute methods | [ ] |
| NULL-profile nodes | 0 (`MATCH (n) WHERE n.profile IS NULL RETURN count(n)`) | [ ] |
| `reembed-stubs` complete | zero_embed_modules = 0 (or low) | [ ] |
| ORM tools smoke (resolve_orm_chain) | Returns hop resolution, not BROKEN | [ ] |

---

## Rollback (if migration breaks something)

```bash
# Restore pre-ops backup:
sudo systemctl stop odoo-semantic-mcp odoo-semantic-webui

<VENV> -m src.cli restore ~/backups/pre-m10-ops-<TIMESTAMP>.tar.gz

sudo systemctl start odoo-semantic-mcp odoo-semantic-webui
```

Migration `m9_010` (dropping legacy columns) is irreversible without a backup restore.
If the restore is needed, re-apply only after root-cause is confirmed.

---

**Date completed:** ___________
**Completion time:** ___________
**Issues encountered:** ___________
**Sign-off:** ___________ (Operator) ___________ (Lead)
