# ADR-0019 — Restore Upload Security (M9 W-RS)

**Status:** Accepted  
**Date:** 2026-05-15  
**Milestone:** M9 "Auth Wow"

---

## Context

The admin web UI needs a restore-from-backup endpoint that accepts `.tar.gz` bundles
(produced by the backup CLI). This is a high-risk operation: it replaces the entire
PostgreSQL database. Failure modes include:

- **Path traversal** in tar members writing files outside the extract directory.
- **Symlink attacks** pointing at sensitive files (e.g., `/etc/passwd`).
- **Upload bombs** (a 1GB gzip expanding to 10GB of disk).
- **Unauthorized access** — a non-admin or unauthenticated actor triggering restore.
- **Concurrent restores** corrupting the database.
- **No rollback path** if restore fails midway.

---

## Decision

### OWASP 10-Item Checklist

| # | Guard | Implementation |
|---|-------|---------------|
| 1 | Content-Type allowlist | Only `application/gzip`, `application/x-gzip`, `application/x-tar`, `application/octet-stream` accepted |
| 2 | Extension allowlist | Only `.tar.gz` / `.tgz` accepted; `Path(filename).name` strips path traversal in filename |
| 3 | Content-Length pre-check | Quick reject if `Content-Length` header > 500MB before streaming starts |
| 4 | Streaming size guard | Read loop counts bytes; raises 413 if > `MAX_RESTORE_BYTES = 500MB` |
| 5 | Disk space check | `shutil.disk_usage().free >= 2 × upload_size` required; returns 507 if insufficient |
| 6 | SHA-256 audit hash | Computed after streaming, before extract; logged to audit record |
| 7 | Maintenance mode | `asyncio.Event` blocks all non-restore endpoints with 503 + `Retry-After: 60` while restore runs |
| 8 | Admin + MFA freshness | `require_admin_with_fresh_mfa` dependency: valid session + `session["mfa_verified_at"]` within last 5 minutes |
| 9 | Pre-restore safety backup | `pg_dump` to `$BACKUP_DIR/pre-restore-<ts>.sql` **must succeed** before any destructive step |
| 10 | Audit log | Records: event, timestamp, filename, sha256, size, job_id, outcome, error snippet |

### Tarfile Safety — PEP 706 `filter='data'`

Python 3.12 adds `tarfile.extractall(filter='data')` (PEP 706). This filter:

- Blocks absolute paths (e.g., `/etc/passwd`)
- Blocks path traversal (e.g., `../../secret`)
- Blocks symlinks pointing outside the destination directory
- Blocks hardlinks pointing outside the destination directory
- Blocks special device files (char/block devices, FIFOs)

Any violation raises one of: `AbsoluteLinkError`, `OutsideDestinationError`,
`LinkOutsideDestinationError`, `SpecialFileError`. The CLI catches all four and
exits with a clear error.

**This filter is REQUIRED** — `filter='data'` is the minimum safe level per PEP 706.

### MFA Freshness Rationale

Standard session authentication (8h TTL) is insufficient for destructive operations.
A 5-minute MFA freshness window ensures:

1. An admin who walked away from their workstation cannot be exploited via a
   stale session (CSRF + session fixation scenarios).
2. The admin must positively re-confirm identity within a short window before
   the restore executes.

If MFA is not enrolled (`session["mfa_verified_at"]` absent), the operation is
blocked with 403. This prevents degraded-mode bypass.

### Maintenance Mode Tradeoff

**Decision:** Use an `asyncio.Event` (in-process, not distributed).

**Pros:**
- Zero external dependencies (no Redis/Postgres advisory lock needed for this single-writer operation).
- Simple, correct within a single process.

**Cons:**
- Does not work across multiple API workers (multiple uvicorn workers would each have independent events).

**Mitigation:** The restore operation is admin-only, expected to be rare, and the
deployment uses a single uvicorn worker for the admin API (port 8003). If multi-worker
deployment is needed in the future, a Postgres advisory lock (`pg_try_advisory_lock`)
should replace the asyncio.Event.

### Pre-Restore Safety Backup

The safety backup path: `$BACKUP_DIR/pre-restore-<unix_ts>.sql` (default
`~/backup/pre-restore-<ts>.sql`). If `pg_dump` fails, the restore is **aborted**
immediately (503 returned). The admin retains the ability to recover from the
pre-restore state.

---

## Consequences

- The restore endpoint requires fresh MFA. Until M9 W-AC (MFA enrollment) ships,
  tests use the `WEBUI_AUTH_DISABLED` + `PYTEST_CURRENT_TEST` bypass.
- The `filter='data'` requirement means Python 3.12+ is required for the restore
  bundle path. This is already satisfied by the `.python-version` file.
- Maintenance mode is process-local — single-worker deployment assumed for admin API.

---

## Related

- ADR-0008: SSH auto-clone (tempfile safety pattern reused here)
- ADR-0011: Web UI session auth (session_at TTL, bcrypt)
- [PEP 706](https://peps.python.org/pep-0706/) — tarfile filter='data'
