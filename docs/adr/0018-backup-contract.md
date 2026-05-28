# ADR-0018 — Backup Bundle Contract

**Status:** Accepted (updated 2026-05-28 — postgres.sql → postgres.dump + retention; supersedes -F plain decision)
**Date:** 2026-05-15  
**Milestone:** M9 W-BK

---

## Context

The existing `backup` CLI command only produced a plain `postgres.sql` file and
printed a manual reminder for Neo4j. Operators needed a self-contained archive
that captures all persistent state (PostgreSQL + Neo4j) and the FERNET_KEY
required for decryption of SSH private keys stored in `ssh_key_pairs`.

Goals:
1. Single command produces a complete, restorable snapshot.
2. FERNET_KEY never stored in plaintext — must be encrypted with a passphrase.
3. Concurrent backup runs must be prevented to avoid partial archives.
4. Web UI can trigger a backup job and stream its progress.
5. Neo4j backup must work **online** (no database shutdown, Community edition).

### Problem with the original `neo4j.dump` approach

The initial design used `neo4j-admin database dump` to produce `neo4j.dump`.
This approach failed in practice:

- **Neo4j 5.x Community requires the database to be OFFLINE** before
  `neo4j-admin database dump` can run. Attempting the dump against a serving
  instance exits 1 with "database in use".
- The workaround (`docker compose stop neo4j` → dump → `docker compose start
  neo4j`) caused ~30 s of downtime per backup run and made the backup command
  fragile against compose-project-name mismatches.
- `neo4j-admin` is only available inside the Neo4j container in typical
  Docker Compose deployments, adding another layer of subprocess complexity.

**Decision (2026-05-26 update):** Replace `neo4j.dump` with `neo4j.cypher`
— a plain-text Cypher export produced entirely over the Bolt protocol using the
`neo4j` Python driver. No APOC plugin, no Enterprise licence, no database
shutdown are required.

---

## Decision

### Bundle format: `<name>.tar.gz`

| File in archive | Description | Required |
|---|---|---|
| `postgres.dump` | `pg_dump -F custom -Z 6` output | Always |
| `neo4j.cypher` | Online Cypher export via Bolt driver (`_export_neo4j_online`) | Optional — skipped if `NEO4J_PASSWORD` absent or Neo4j unreachable |
| `fernet.enc` | FERNET_KEY encrypted with passphrase-derived key | Only if `--bundle-passphrase-env` provided |
| `manifest.json` | `created_at`, `schema_version`, `components[]` with sha256 per file | Always |

### Backward compatibility

Bundles produced before 2026-05-28 contain `postgres.sql` (plain-text SQL) instead of
`postgres.dump` (custom binary format). The `restore` command auto-detects which restore
tool to use based on the file extension:

- `postgres.dump` → `pg_restore` (pg_dump custom format)
- `postgres.sql` → `psql` (legacy plain-text dump)

Detection happens in `src/cli.py:_restore_bundle` via the renamed helper
`_restore_sql_or_dump`, which inspects `pg_dump.suffix.lower()` before dispatching.

Legacy `.sql` support is maintained for at least one minor version (through 0.14.x).
After that, it may be dropped via a separate ADR amendment. Operators with pre-2026-05-28
bundles should treat them as readable but should not rely on continued `.sql` support
beyond the 0.14.x series.

**Note on legacy `neo4j.dump`:** Bundles created before 2026-05-26 may contain
`neo4j.dump` instead of `neo4j.cypher`. The `restore` command detects the file
name and prints a manual-restore note for legacy `.dump` files; `.cypher` files
are restored automatically via the Bolt driver.

### Retention

After writing the new bundle, the backup command prunes old bundles in the same
`BACKUP_DIR` to bound storage growth.

- **Default `keep_n = 14`** — approximately two weeks of nightly bundles. At ~3 GB/bundle
  (production figure), this caps storage at ~42 GB. Production currently runs ~6 bundles,
  so the default is conservative by design.
- **Override precedence:** `--keep-bundles N` (CLI flag) > `OSM_BACKUP_KEEP` (env var)
  > built-in default `14`. A value `< 1` is clamped to `1`.
- **Safety:** `_prune_old_bundles` (`src/cli.py`) NEVER deletes the bundle being written
  in the current run (`current_bundle` parameter). Bundles are sorted by `mtime` descending
  (newest first); the oldest beyond `keep_n` are removed.
- **Idempotent:** Re-running backup with the same `keep_n` against a directory that already
  satisfies the retention limit does not double-prune — files already within `keep_n`
  are untouched.

### Output path validation

`--output` must end with `.tar.gz` **and** resolve (via `Path.resolve()` — follows
symlinks) to a path under `BACKUP_DIR` env var (default: `~/backup`). This prevents
path-traversal and accidental writes outside the designated backup directory.

### FERNET_KEY encryption

`fernet.enc` uses PBKDF2-HMAC-SHA256 (100 000 iterations) to derive a 32-byte key
from the passphrase + a random 16-byte salt. Output layout:

```
[ 16-byte salt ] [ Fernet token (variable) ]
```

The passphrase is read from the environment variable named by `--bundle-passphrase-env`
and is **never logged**. Storing the FERNET_KEY in plaintext inside the bundle is
explicitly forbidden.

### Advisory lock

`pg_try_advisory_lock(0xBA17C9)` is acquired before any backup work and released
on exit (success or failure). A second concurrent backup returns an error immediately
rather than producing a partial or inconsistent archive.

Lock ID `0xBA17C9` ("BAKCUP" in hex — mnemonic) is shared between:
- `src/cli.py` `_backup_advisory_lock()` (CLI path)
- `src/web_ui/routes/operations.py` backup job (spawns the CLI, so advisory lock is
  held by the subprocess)

### Web UI integration

`POST /api/operations/backup` creates an in-memory job record and spawns the CLI as
a detached subprocess. Output is captured to `/tmp/osm-backup-<uuid>.log`.

`GET /api/operations/backup/{job_id}/stream` returns a `text/event-stream` SSE
response that tails the log file and forwards lines with ANSI escapes stripped.
A `: heartbeat` comment is emitted every 15 seconds to prevent nginx timeouts.

`GET /api/operations/backup/{job_id}/status` is a simple JSON poll endpoint for
clients that prefer not to use SSE.

---

### Neo4j Cypher export format (`neo4j.cypher`)

The file produced by `_export_neo4j_online()` contains:

1. A comment header with export timestamp.
2. `CREATE` statements for every node, with all labels and properties encoded as
   inline Cypher literals.  A temporary `__eid__` property (the Neo4j
   `elementId()`) is injected on every node so relationships can be wired up by
   this surrogate key.
3. `MATCH … CREATE` statements for every relationship (type + properties).
4. A cleanup `MATCH … REMOVE n.__eid__` statement to remove the temporary property.

The file is suitable for replay via `cypher-shell < neo4j.cypher` (manual) or
via `python -m src.cli restore <bundle.tar.gz>` (automatic, Bolt driver).

### Required Neo4j configuration

No extra configuration is needed beyond the standard `NEO4J_URI`,
`NEO4J_USER`, and `NEO4J_PASSWORD` environment variables (or equivalent INI
settings). In particular:

- **No APOC plugin** required.
- **No Enterprise licence** required.
- **No `apoc.export.file.enabled`** required.
- **Database stays online** during the entire backup operation.

---

## Restore prerequisites

To restore from a bundle:

1. **PostgreSQL:** `psql` the `postgres.sql` into a fresh database.
2. **Neo4j (new bundles):** The `restore` command automatically loads
   `neo4j.cypher` via the Bolt driver when `NEO4J_PASSWORD` is set.
   **Restore is a REPLACE, not a merge:** the Cypher file replays `CREATE`
   statements (not `MERGE`), so the restore first runs `MATCH (n) DETACH DELETE n`
   to wipe the existing graph before replaying — otherwise restoring onto a
   non-empty graph would duplicate every node and relationship. This preserves
   the destructive semantics of the old offline `neo4j-admin database load`
   path. A `Wiping existing Neo4j graph before restore...` line is printed to
   stdout before the wipe.
   To replay manually, wipe first then load:
   `cypher-shell -u neo4j -p <pass> "MATCH (n) DETACH DELETE n"` then
   `cypher-shell -u neo4j -p <pass> < neo4j.cypher`.

   **DR safety guards (PR #189) around the destructive wipe:**
   - **Validate before wipe.** `_restore_neo4j_cypher` parses the file and
     refuses to run `DETACH DELETE` unless it contains ≥1 executable statement
     AND the export completeness trailer (`REMOVE n.__eid__`, always written last
     by `_export_neo4j_online`). An empty/truncated/corrupt dump returns an error
     and the live graph is left untouched — a wiped graph with nothing valid to
     restore is unrecoverable.
   - **Pre-restore safety snapshot.** Before the wipe, `restore` snapshots the
     current live graph to `BACKUP_DIR/pre-restore-<ts>-neo4j.cypher` (via
     `_export_neo4j_online`) — parity with the Postgres pre-restore safety dump.
     If the graph is reachable but the snapshot fails, the restore aborts before
     wiping. If Neo4j is unreachable/unconfigured, the snapshot is skipped (the
     restore would itself fail to connect and never reaches `DETACH DELETE`).
   - **Failure propagates.** A failed/partial Neo4j restore makes the `restore`
     command exit non-zero (Postgres success is still reported on stdout), so DR
     automation never mistakes a half-restored graph for success.
3. **Neo4j (legacy bundles pre-2026-05-26):** `neo4j.dump` is present instead.
   Load via `neo4j-admin database load --from-path=/path neo4j` (requires DB offline).
   See `docs/deploy.md §Backup` for the offline load procedure.
4. **FERNET_KEY:** Decrypt `fernet.enc` using the passphrase:
   - Read first 16 bytes as `salt`.
   - Derive key: `PBKDF2-HMAC-SHA256(passphrase, salt, 100_000, dklen=32)`, then
     `base64.urlsafe_b64encode(key_bytes)`.
   - `Fernet(derived_key).decrypt(rest_of_file)` → plaintext FERNET_KEY.
   - Set `FERNET_KEY=<plaintext>` before starting the application.
5. **Verify checksums** from `manifest.json` against restored files.

---

## Consequences

- Backups are larger (tar.gz vs plain SQL) but complete.
- `neo4j.cypher` **backup** export is non-fatal: if `NEO4J_PASSWORD` is absent or
  Neo4j is unreachable, the backup step is skipped with a `WARNING` log line;
  `postgres.sql` is still captured. The omission is visible in `manifest.json`
  `components`. (This non-fatal posture applies to the *backup* path only — the
  *restore* path treats a Neo4j failure as fatal to the exit code; see Restore
  step 2 DR safety guards above.)
- `_export_neo4j_online` streams statements directly to the output file handle
  inside a single read transaction — it does not buffer the whole graph in
  memory, so peak RSS stays flat regardless of graph size (the ~1–2 M-node graph
  would otherwise cost hundreds of MB of in-memory Cypher).
- **Zero downtime**: unlike the old `neo4j-admin dump` approach, the database
  never needs to be stopped during backup.
- **Community edition compatible**: no APOC plugin or Enterprise licence required.
- Export time scales with graph size (nodes × properties + relationships).
  For the typical OSM production graph (~1–2 M nodes) expect 30–120 s.
- The Web UI backup job list is in-memory only (process restart clears it).
  For persistent job history, a future ADR should integrate with `indexer_jobs`.
- Passphrase management is the operator's responsibility; there is no key escrow.
