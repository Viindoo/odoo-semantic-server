# M9 Post-Merge Operations Runbook

> Operations to execute on **production server** after M9 (v0.4.0) is deployed.  
> Run sequentially. Each section has commands + expected outcome + verification steps.
>
> **Start time:** ___________  
> **Operator:** ___________

---

## Pre-flight Checks

- [ ] All 3 systemd services running:
  ```bash
  systemctl status odoo-semantic-mcp odoo-semantic-api odoo-semantic-astro
  ```
  Expected: `active (running)` for all three.

- [ ] Migration check (verify all M9 migrations applied):
  ```bash
  ~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate --check
  ```
  Expected output: all `m9_001` through `m9_008` migrations listed as applied.

- [ ] Create safety backup before operations:
  ```bash
  mkdir -p ~/backup
  ~/.venv/odoo-semantic-mcp/bin/python -m src.cli backup \
      --output ~/backup/pre-m9-ops-$(date +%Y%m%d-%H%M%S).sql
  ```
  Save the output filename for reference.

---

## 1. Clean Up `99.0` Test Artifact Nodes (5 min)

Test data from CI occasionally pollutes production Neo4j with `odoo_version: '99.0'` (the `TEST_VERSION` constant from `tests/conftest.py`). Clean these up.

**Command:**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    -d odoo-semantic \
    "MATCH (m:Module {odoo_version: '99.0'}) DETACH DELETE m; RETURN 'Cleanup complete';"
```

**Expected output:**
```
0 nodes deleted
```
(or N if test artifacts were present — both are OK)

**Verification:**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    -d odoo-semantic \
    "MATCH (m:Module {odoo_version: '99.0'}) RETURN count(m) AS test_artifact_count;"
```
Expected: `test_artifact_count = 0`.

**Result:** [ ] _____ count verified

---

## 2. Index Odoo Core Symbols v9–v19 (15–30 min)

The `index-core` command indexes framework-level symbols (ORM models, fields, decorators) for each Odoo version. This powers the `lookup_core_api` MCP tool. Run once per version, in order.

**Versions to index:**
- v9.0 (legacy era1 — requires `~/git/odoo9` present)
- v10.0, v11.0, …, v17.0 (each requires `~/git/odoo<N>` checkout)
- v19.0 (latest; requires `~/git/odoo19` checkout)

**Note:** v18 is deferred (OBS-1), v20 not yet released by Odoo.

**Commands:**
```bash
for V in 9 10 11 12 13 14 15 16 17 19; do
    echo "=== Indexing Odoo v${V}.0 ===" >&2
    ~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-core \
        --source "$HOME/git/odoo${V}" \
        --version "${V}.0" \
        --log-level INFO || {
            echo "ERROR: index-core v${V}.0 failed. Abort and investigate." >&2
            exit 1
        }
    sleep 2  # brief pause between versions
done
echo "✓ All versions indexed successfully"
```

**Expected:** For each version, ~50–150 CoreSymbol nodes created (varies by version). Progress bars show scanner + writer stages.

**Verification (after completion):**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    -d odoo-semantic \
    "MATCH (c:CoreSymbol)
     RETURN c.odoo_version AS version, count(c) AS symbols
     ORDER BY toFloat(version) DESC;"
```

Expected output: 9 rows (v9.0 through v19.0) with symbol counts > 0.

**Result:** [ ] _____ all versions have symbol counts > 0

---

## 3. Seed Production Pattern Catalogue (5 min)

M7.5-P2-SEED: Load curated pattern catalogue into Neo4j for the `suggest_pattern` MCP tool. Patterns are versioned; embeddings are reused via `_SeedMeta` sentinel (ADR-0007).

**Command:**
```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer seed-patterns
```

**Expected:** 80+ PatternExample nodes created (per ADR-0009 minimum). Idempotent — re-running detects unchanged patterns via sentinel hash.

**Verification:**
```bash
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    -d odoo-semantic \
    "MATCH (p:PatternExample) RETURN count(p) AS pattern_count;"
```

Expected: `pattern_count >= 80`.

**Result:** [ ] _____ pattern count ≥ 80

---

## 4. Bootstrap Admin User (if not exists) (2 min)

M9 schema added `is_admin DEFAULT FALSE` to webui_users. Existing web UI users created pre-M9 do **not** have admin rights. Bootstrap or repair.

**Option A: Create a new admin user (interactive — prompts for password) — requires W-CP merge:**
```bash
# (only available after W-CP is merged; W-CP adds --admin flag to manager CLI)
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-webui-user admin --admin
```

**Option B: Create user then grant admin via SQL (fallback — works immediately):**
```bash
# Create user (will prompt for password interactively)
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-webui-user admin

# Then grant admin rights via SQL
psql -d odoo_semantic -c "UPDATE webui_users SET is_admin=TRUE WHERE username='admin';"
```

**Option C: Grant admin to existing user (if admin user already exists):**
```bash
psql -d odoo_semantic -c "UPDATE webui_users SET is_admin=TRUE WHERE username='admin';"
```

**Verification:**
```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.manager list-webui-users
```

Expected output: at least one row with `is_admin = True`.

**Result:** [ ] _____ admin user exists with is_admin=True

---

## 5. Verify Audit Log Captures Events (1 min)

Audit log (`admin_audit_log` table, added in M9) records non-repudiation of admin actions. After this operation, check that login events are being captured.

**Command (after 5+ minutes of post-merge activity):**
```bash
psql -d odoo_semantic -c "
    SELECT actor, action, count(*) AS count
    FROM admin_audit_log
    WHERE created_at > NOW() - INTERVAL '1 hour'
    GROUP BY actor, action
    ORDER BY count DESC;"
```

**Expected:** Rows for login events, profile changes, API key creation, etc. If empty, audit log decorator is not wired — investigate before proceeding.

**Result:** [ ] _____ audit log has ≥ 3 events in last hour

---

## 6. Optional: Cron Setup for Cleanup Jobs (2 min)

M9 added three TTL-tracked tables (`login_attempts`, `email_verifications`, `active_sessions`). Add periodic cleanup to avoid unbounded growth.

**Create daily cleanup cron job:**
```bash
sudo tee /etc/cron.daily/odoo-semantic-cleanup > /dev/null <<'CRON_EOF'
#!/bin/bash
set -e
export PG_DSN="postgresql://tuan@localhost:5432/odoo_semantic"
# Purge login attempts older than 30 days
psql -d odoo_semantic -c \
    "DELETE FROM login_attempts WHERE attempted_at < NOW() - INTERVAL '30 days';"
# Purge expired email verification tokens
psql -d odoo_semantic -c \
    "DELETE FROM email_verifications WHERE expires_at < NOW() - INTERVAL '7 days';"
# Purge expired sessions
psql -d odoo_semantic -c \
    "DELETE FROM active_sessions WHERE expires_at < NOW();"
CRON_EOF

sudo chmod +x /etc/cron.daily/odoo-semantic-cleanup
```

**Verify installation:**
```bash
ls -la /etc/cron.daily/odoo-semantic-cleanup
```

**Result:** [ ] _____ cron job installed (optional, for cleanup)

---

## 7. V8 Era1 CLI Parser Coverage — Backlog Note (1 min)

`parser_cli.py` currently produces 0 CLIFlag entries for Odoo v8 (legacy era1). This is **not a bug** — v8 CLI infrastructure differs significantly from modern versions.

**Action:** No fixes required in M9. Defer enhancement to M10 backlog:
- [ ] File GitHub issue: "Stream E — CLI parser v8 era1 enhancement"
- [ ] Update `src/indexer/parser_cli.py` docstring: document expected 0 CLIFlag count for v8.

**Result:** [ ] _____ noted in TASKS.md backlog

---

## Post-Merge Sign-Off

| Item | Status | Checked by |
|------|--------|-----------|
| Pre-flight checks pass | [ ] | ____________ |
| 99.0 cleanup verified | [ ] | ____________ |
| index-core v9–v19 complete | [ ] | ____________ |
| Pattern catalogue ≥ 80 rows | [ ] | ____________ |
| Admin user bootstrap/repair | [ ] | ____________ |
| Audit log populated after 1h | [ ] | ____________ |
| (Optional) Cron cleanup job installed | [ ] | ____________ |
| Astro UI responsive (spot-check) | [ ] | ____________ |
| MCP tools return valid responses (smoke test) | [ ] | ____________ |

---

## Rollback Procedure (if needed)

If any step fails or data corruption detected:

1. **Stop all services:**
   ```bash
   sudo systemctl stop odoo-semantic-mcp odoo-semantic-api odoo-semantic-astro
   ```

2. **Restore from pre-M9 backup:**
   ```bash
   ~/.venv/odoo-semantic-mcp/bin/python -m src.cli restore ~/backup/pre-m9-ops-*.sql
   ```

3. **Restore Neo4j from backup** (if applicable):
   ```bash
   neo4j-admin database load --from-path=/backup neo4j odoo-semantic
   ```

4. **Restart services:**
   ```bash
   sudo systemctl start odoo-semantic-mcp odoo-semantic-api odoo-semantic-astro
   ```

5. **Notify team** + file incident report.

---

## Notes for Future Milestones

- **M10:** CLI parser v8 era1 enhancement (backlog).
- **M10:** Implement `--full` reindex monthly to clean stale Module nodes from renames/moves (ADR-0007 §D5).
- **M10+:** FERNET rotation UI (currently placeholder in `/admin/operations`).

---

**Date completed:** ___________  
**Completion time:** ___________  
**Issues encountered:** ___________  
**Sign-off:** ___________ (Operator) ___________ (Lead)
