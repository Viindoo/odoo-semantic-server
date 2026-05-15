# ADR-0018 — Backup Bundle Contract

**Status:** Accepted  
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

---

## Decision

### Bundle format: `<name>.tar.gz`

| File in archive | Description | Required |
|---|---|---|
| `postgres.sql` | `pg_dump -F plain` output | Always |
| `neo4j.dump` | `neo4j-admin database dump neo4j` | Optional (skipped if binary absent or Neo4j not running) |
| `fernet.enc` | FERNET_KEY encrypted with passphrase-derived key | Only if `--bundle-passphrase-env` provided |
| `manifest.json` | `created_at`, `schema_version`, `components[]` with sha256 per file | Always |

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

## Restore prerequisites

To restore from a bundle:

1. **PostgreSQL:** `psql` the `postgres.sql` into a fresh database.
2. **Neo4j:** `neo4j-admin database load` the `neo4j.dump` (if present).
3. **FERNET_KEY:** Decrypt `fernet.enc` using the passphrase:
   - Read first 16 bytes as `salt`.
   - Derive key: `PBKDF2-HMAC-SHA256(passphrase, salt, 100_000, dklen=32)`, then
     `base64.urlsafe_b64encode(key_bytes)`.
   - `Fernet(derived_key).decrypt(rest_of_file)` → plaintext FERNET_KEY.
   - Set `FERNET_KEY=<plaintext>` before starting the application.
4. **Verify checksums** from `manifest.json` against restored files.

---

## Consequences

- Backups are larger (tar.gz vs plain SQL) but complete.
- `neo4j-admin` absence is non-fatal — logged as a warning; only `postgres.sql`
  is included. Operators should note the omission in the `manifest.json` `components`
  list.
- The Web UI backup job list is in-memory only (process restart clears it).
  For persistent job history, a future ADR should integrate with `indexer_jobs`.
- Passphrase management is the operator's responsibility; there is no key escrow.
