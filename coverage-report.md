# OBS-1 Coverage Report — Phase 3 Indexer Commands

**Branch:** `wt-coverage`  
**Date:** 2026-05-15  
**Scope:** Expand indexed Odoo version coverage from v8/9/10/11/12/17 → v8/9/10/11/12/13/14/15/16/17/19

---

## Disk Reality Check

Repos confirmed present on disk:

| Version | Path | Status |
|---------|------|--------|
| 13.0 | `~/git/odoo_13.0/` | Available |
| 14.0 | `~/git/odoo_14.0/` | Available |
| 15.0 | `~/git/odoo_15.0/` | Available |
| 16.0 | `~/git/odoo_16.0/` | Available |
| 18.0 | *(not present)* | **DEFERRED** — see below |
| 19.0 | `~/git/odoo_19.0/` | Available |

---

## v18 Deferred

`odoo_18.0` repo is **not present on disk** at `~/git/`. v18 indexing is deferred.

**Action required (separate ticket):** SSH clone `git@github.com:Viindoo/odoo.git` at branch `18.0` via:

```bash
# See docs/adr/0008-ssh-auto-clone.md for SSH key policy
POST /api/repos/<id>/clone   # via web UI or API after registering the repo
```

Or manually:

```bash
git clone --branch 18.0 --single-branch git@github.com:Viindoo/odoo.git \
    ~/git/odoo_18.0/
```

Once cloned, register and index via the same Phase 3 pattern below.

---

## What Was Added (this PR)

| Deliverable | Path |
|-------------|------|
| Profile seed completeness test | `tests/test_profile_seed_completeness.py` |
| This report | `coverage-report.md` |

The Python seeder (`src/db/seed_master_data.py::_PROFILE_DEFS`) already defines
all 26 profiles for v8-v19 (Odoo CE + Standard Viindoo + Viindoo Internal v17/v18).
Prod DBs missing rows for v13/14/15/16/19 simply need a re-run of:

```bash
python -m src.db.migrate            # runs seed_all() idempotently after yoyo
# OR explicitly:
python -m src.manager seed-master-data
```

Both paths use `INSERT … ON CONFLICT (name) DO NOTHING` so they are safe to
re-run on already-seeded DBs.

**Why not a yoyo SQL migration?** An earlier draft of this work added
`migrations/0004_add_missing_version_profiles.sql` to belt-and-suspenders the
profile rows. It was removed because `src/db/migrate.py` (docstring lines
540-542) and `src/db/seed_master_data.py` (lines 8-14) explicitly contract that
`run_migrations()` keeps yoyo migrations schema-only — master data INSERTs live
in the Python seeder so that legacy test fixtures see an empty profiles table
after `run_migrations()`. The migration broke that contract and 16 integration
tests as a result.

Note: `odoo_18` is included in the Python seeder. v18 source repo not on disk
as of 2026-05-15 (deferred per OBS-1) — register via admin webui SSH auto-clone
(ADR-0008).

---

## Phase 3: Run the Indexer (DO NOT RUN IN THIS WT — Phase 3 only)

After this PR is merged and migration applied (`python -m src.db.migrate`),
run the following **in a separate Phase 3 session**.

Per `feedback_long_running_jobs.md` memory note: always detach with `setsid nohup`.

### Step 0: Apply migration + seed

```bash
python -m src.db.migrate
# Expected output: "✓ Seeded master data: N profiles new, ..."
```

### Step 1: Register local repo paths for each version

The Python seeder creates repos with `clone_status='manual'` pointing to
auto-clone paths under `~/.local/share/odoo-semantic-mcp/clones/`. If the repos
are checked out at `~/git/odoo_<N>.0/` instead, update
`local_path` via the web UI (/admin/repos) or direct SQL:

```sql
-- Example for v13 (adjust for each version):
UPDATE repos
SET local_path = '/home/<user>/git/odoo_13.0',
    clone_status = 'manual'
WHERE url = 'git@github.com:Viindoo/odoo.git'
  AND branch = '13.0';
```

### Step 2: Index each new version (detached)

```bash
setsid nohup ~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo \
    --profile odoo_13 > /tmp/osm-idx-v13.log 2>&1 &

setsid nohup ~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo \
    --profile odoo_14 > /tmp/osm-idx-v14.log 2>&1 &

setsid nohup ~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo \
    --profile odoo_15 > /tmp/osm-idx-v15.log 2>&1 &

setsid nohup ~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo \
    --profile odoo_16 > /tmp/osm-idx-v16.log 2>&1 &

setsid nohup ~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo \
    --profile odoo_19 > /tmp/osm-idx-v19.log 2>&1 &
```

Monitor progress:

```bash
tail -f /tmp/osm-idx-v13.log
tail -f /tmp/osm-idx-v14.log
# etc.
```

### Step 3: Verify in Neo4j

```bash
docker compose exec neo4j cypher-shell -u neo4j -p '<NEO4J_PASSWORD>' \
    "MATCH (m:Module) RETURN DISTINCT m.odoo_version ORDER BY toFloat(m.odoo_version)"
```

**Expected output after all versions indexed:**

```
m.odoo_version
"8.0"
"9.0"
"10.0"
"11.0"
"12.0"
"13.0"
"14.0"
"15.0"
"16.0"
"17.0"
"19.0"
```

*(v18.0 absent until separate SSH-clone ticket is resolved)*

---

## Parallel Indexing Option

If indexing all 5 versions in parallel is acceptable (4 vCPU+ recommended):

```bash
setsid nohup ~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-profile \
    --profile-workers 5 --max-workers 2 \
    --profiles odoo_13,odoo_14,odoo_15,odoo_16,odoo_19 \
    > /tmp/osm-idx-obs1-parallel.log 2>&1 &
```

Per `docs/adr/0006`: `progress=False` is forced when `profile_workers > 1`. Each
thread opens its own pg_conn; per-profile advisory lock ensures safety.
