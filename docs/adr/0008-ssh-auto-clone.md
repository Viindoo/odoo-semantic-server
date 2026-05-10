# ADR-0008 — SSH Auto-Clone (M6 Wave 4)

**Status:** Accepted (2026-05-11)

**Context:** M5 shipped FERNET-encrypted `ssh_key_pairs` table + Web UI to generate/list/delete Ed25519 keypairs (ADR-0004). Admins still cannot register a repo without manually cloning and passing `--local-path` to `add-repo`. M6 Wave 2 (ADR-0007) requires full git history for the incremental indexer (`git diff old..new`). M6 Wave 4 closes the loop: detect SSH URL → auto-clone via stored key → set `local_path` automatically.

## Decisions

### D1 — URL detection: regex `^git@|^ssh://`

SSH URLs match either:
- `git@host:path/to/repo.git` (GitHub SSH shorthand)
- `ssh://git@host/path/to/repo.git` (explicit SSH URI)

HTTPS URLs (`https://...`) continue the current manual flow (out of scope: HTTPS basic-auth tokens).

### D2 — Key delivery via `GIT_SSH_COMMAND` env var, NOT `-i` flag

The private key path is passed to `git clone` via environment variable, never as a command-line argument:

```bash
GIT_SSH_COMMAND="ssh -i <tmp_key> -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=<project_known_hosts>" \
  git clone --branch <branch> --single-branch <url> <target>
```

**Rationale:** `/proc/<pid>/cmdline` is world-readable on most Linux systems. Passing key path via `-i` flag leaves a trace. Environment variables are private to the process (readable only via `/proc/<pid>/environ` which requires process ownership or root). Further, `GIT_SSH_COMMAND` is standard Git convention (preferred over `-c core.sshCommand`).

### D3 — Tempfile atomic creation + cleanup invariant

Key decryption stores the plaintext in a secure tempfile:

```python
fd, tmp_key = tempfile.mkstemp(mode=0o600, prefix='osm-ssh-')
try:
    with os.fdopen(fd, 'wb') as f:
        f.write(decrypted_key_bytes)
    # clone via GIT_SSH_COMMAND pointing to tmp_key
finally:
    os.unlink(tmp_key)
```

**Mode 0o600:** atomic (set at creation), no race window between create and chmod.  
**try/finally:** cleanup runs on success, failure, and mid-clone exception (e.g., network error, auth failure).  
**SIGKILL leak:** if orchestrator kills the process mid-finally, tempfile remains. Accepted trade-off (best-effort; system tmpdir cleanup on reboot handles it).

### D4 — Project-local `known_hosts`, NOT system `~/.ssh/known_hosts`

Host key verification writes to:

```
~/.local/share/odoo-semantic-mcp/known_hosts
```

**Rationale:**
- Multi-tenant safe (no shared system state between projects/deployments).
- NEVER touches system `~/.ssh/known_hosts` (non-portable; conflicts with user's personal SSH setup).
- Admin can manually inspect/clear if MITM suspected: `cat ~/.local/share/odoo-semantic-mcp/known_hosts`.

**Policy:** `-o StrictHostKeyChecking=accept-new` persists fingerprints. No interactive prompt; safe for background indexing.

### D5 — Full clone (NOT `--depth=1`)

Clone command: `git clone --branch <branch> --single-branch <url> <target>`

No `--depth=1` shallow clone. **Rationale:** ADR-0007 incremental indexer (M6 Wave 2) requires full git history to compute `git diff old..new` between commits. Shallow clone would force full reindex on every change (defeating incremental benefit). Trade-off: large Odoo repos take 3–10 minutes to clone. Handled via background job pattern (D6).

### D6 — Background clone via subprocess + `clone_status` lifecycle

Mirror M5.5 `indexer_jobs` pattern. Web UI `POST /repos/{id}/clone` spawns async task:

```
User action: POST /repos/{id}/clone
↓
Web UI updates repos.clone_status ← "pending"
↓
Spawn subprocess: python -m src.cloner --repo-id N
↓
Cloner fetches key, spawns git, updates repos.clone_status ← "cloned" or "error"
↓
Web UI polls /repos/repos/{id}/clone-status every 5s
```

Cloner process:
1. Read repo + SSH key from DB.
2. Decrypt key (FERNET).
3. Spawn `git clone` with `GIT_SSH_COMMAND`.
4. On success/failure, update `repos.clone_status` + optional `repos.clone_error_msg`.
5. Exit.

### D7 — Schema delta: two new columns per ADR-0001 M6 policy

```sql
ALTER TABLE IF NOT EXISTS repos ADD COLUMN ssh_key_id INTEGER REFERENCES ssh_key_pairs(id) ON DELETE SET NULL;
ALTER TABLE IF NOT EXISTS repos ADD COLUMN clone_status TEXT NOT NULL DEFAULT 'manual';
```

Idempotent `ALTER TABLE IF NOT EXISTS` per ADR-0001. Existing rows: `clone_status='manual'` (legacy behavior preserved). New rows default to `'manual'` until user explicitly clones via Web UI.

Distinct columns prevent overwrite collision:
- `repos.error_msg` — written exclusively by `update_repo_status` (indexer)
- `repos.clone_error_msg` — written exclusively by `set_clone_status` (cloner)

Keeping them separate ensures a cloner success path (`set_clone_status('cloned', error_msg=None)`) never clears a prior indexer error stored in `error_msg`, and vice versa. Implemented in M6 W4 Opus review fixup.

### D8 — Default clone target dir configurable

Clones land at:

```
~/.local/share/odoo-semantic-mcp/clones/<profile>/<repo_slug>/
```

Configurable via `[clones] base_dir` in `odoo-semantic.conf`. Prevents nested repo clones and centralizes management.

## Consequences

**Positive:**
- Admins paste SSH URL into Web UI → repo auto-cloned without leaving UI.
- Multi-tenant deploys safe (no shared `~/.ssh/`).
- Clone status badge surfaces failures in UI with error message.
- Full git history enables incremental indexing (ADR-0007).

**Negative:**
- Clone time visible in UI (minutes on large repos). Users may perceive slowness; mitigated by progress indicator.
- Extra table columns (D7) + subprocess overhead (D6). Negligible compared to indexing time.

**Risk:**
- Ed25519 key leaked if tempfile not cleaned up on SIGKILL (D3). Accepted: best-effort, system tmpdir cleans on reboot.
- `StrictHostKeyChecking=accept-new` auto-accepts first SSH key (D4). Suitable for CI/automation; admin should verify first fingerprint manually if security-sensitive.

## Out of scope (deferred to M7 or later)

- HTTPS basic-auth tokens in URL (security concerns; unclear use case).
- Per-host key binding (multi-host orgs may need it; complex).
- Host fingerprint UI review-before-accept (currently auto-accept + manual `known_hosts` edit).
- SSH key rotation script (defer to M5.5 pattern).

## Alternatives considered

1. **Pass key via `-i` flag** — leaks key path in `/proc/<pid>/cmdline`. Rejected.
2. **Use system `~/.ssh/known_hosts`** — not multi-tenant safe. Rejected.
3. **Shallow clone (`--depth=1`)** — breaks incremental indexer (ADR-0007). Rejected.
4. **Synchronous clone (block Web UI)** — poor UX for 3–10 minute operations. Rejected (D6 async better).

## References

- ADR-0001: Schema evolution policy (M6 idempotent ALTER)
- ADR-0004: FERNET encryption + ssh_key_pairs table (M5)
- ADR-0007: Incremental indexer (M6 Wave 2; requires full git history)
