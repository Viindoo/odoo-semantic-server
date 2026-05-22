# ADR-0035 — Git Access Model: Subprocess CLI + Multi-Tenant Concurrency Policy

**Date:** 2026-05-22
**Status:** Proposed — M13

> Companion to [ADR-0034](0034-multi-tenant-pooled-isolation.md) (multi-tenant) and
> [ADR-0008](0008-ssh-auto-clone.md) (SSH auto-clone). D3 supersedes ADR-0008's
> `accept-new` host-key posture; D5 revisits ADR-0008's full-clone choice.

## Context

OSM accesses git **only** via subprocess to the git CLI today:

- `clone` — `src/git_utils.py` (one-shot, SSH key via `GIT_SSH_COMMAND`).
- read-only `rev-parse HEAD` / `merge-base --is-ancestor` / `diff --name-only` — `src/indexer/incremental.py`.

No `GitPython` / `dulwich` / `pygit2` dependency exists; `pyproject.toml` declares none. There is **no** `fetch`/`pull`/`reset` in the codebase — a cloned repo is never refreshed in-place today.

M13 (ADR-0034) changes the operating profile sharply: many customers × many **private** repos, access granted via **per-tenant deploy keys**, and **concurrent** clone/fetch/reset across tenants for re-indexing. That raised the question: keep the git CLI, or adopt a Python git library?

Three libraries were evaluated:

| Library | Nature | Needs git binary | Native dep | SSH credential handling |
|---|---|---|---|---|
| **GitPython** | Thin wrapper over the git CLI | Yes | No | Borrows git CLI (same as today) |
| **dulwich** | Pure-Python reimplementation | No | No | `SSHVendor`/paramiko, configured in-process |
| **pygit2** | libgit2 (C) binding | No | Yes (version-pinned) | libssh2 callbacks, in-process |

## Decision

### D1 — Keep subprocess git CLI as the access mechanism

The operation set is narrow (`clone`/`fetch`/`reset` + read-only `rev-parse`/`diff`) with no high-volume in-process object walking that would justify pygit2. This is the model proven by GitLab / Gitea / Forgejo (shell out to git).

Decisively for the **deploy-key-per-tenant** model: credential isolation is **per-process** via `env = {**os.environ, "GIT_SSH_COMMAND": ...}` + a per-op tempfile key (`git_utils.py:99-117`). A Python library manages SSH credentials via **in-process** callbacks/agent — far easier to leak or cross-wire keys when many tenants' keys are in flight concurrently. Subprocess isolation wins here.

- **dulwich** is the fallback **only** if a future requirement removes the git binary (e.g. a minimal container with no git).
- **pygit2** is rejected unless a genuine in-process performance need emerges; the libgit2 version-pinning burden in CI/CD is not worth four git commands.

### D2 — Per-repo advisory lock around all mutating git ops

Mutating ops (`clone`/`fetch`/`reset`/`checkout`) on the **same** repo from two processes race on `.git/index.lock`. Current locking is **per-profile** (`pipeline.py _profile_lock_id`) — too coarse.

Add a Postgres advisory lock keyed **per-repo** (`lock_id` derived from `repo_id`) wrapping every mutation. Read-only ops (`rev-parse`, `diff --name-only`) need no lock and run concurrently, but **must not** run concurrently with a `reset`/`checkout` of the same repo. Cross-repo concurrency is naturally safe (separate working tree + `.git`).

### D3 — known_hosts pinning (replaces shared-file + `accept-new`)

Today a **single shared** known_hosts file + `StrictHostKeyChecking=accept-new` (`git_utils.py:112-116`). Under concurrency this has two defects:

1. Concurrent first-clones from multiple hosts write the same file → race/corruption.
2. `accept-new` is trust-on-first-use → an undetected first-time MITM at multi-tenant scale.

Pre-populate known_hosts with **pinned host keys** for the common forges (GitHub / GitLab / Bitbucket), mount it read-only, and switch to `StrictHostKeyChecking=yes`. A per-clone known_hosts tempfile is an acceptable alternative. This is both a concurrency fix and a security hardening.

### D4 — Repo refresh = `fetch` + `reset --hard origin/<branch>` (never `pull`/`merge`)

The clone is a read-only mirror; nobody edits the working tree. `reset --hard` avoids merge conflicts entirely and is the correct refresh primitive. It runs under the D2 per-repo lock. No `fetch`/`pull`/`reset` exists today — this defines the pattern for M13 WI-8.

### D5 — Resource-bounded concurrency + evaluate partial clone

Keep the existing worker cap (`ThreadPoolExecutor(max_workers=...)` in `src/manager/__main__.py`).

ADR-0008 D5 chose full clone (no `--depth=1`) because incremental diff needs history. Full clone of large repos × many tenants × concurrent causes disk/RAM/CPU spikes. **Evaluate** `--filter=blob:none` (partial clone): it drops historical blob storage (the dominant cost) while keeping full commit history, so `diff --name-only` for incremental still works without fetching blobs; current-tree blobs are fetched at checkout (the parser needs them anyway). Caveat: requires connectivity at refresh time — acceptable on a server, but verify air-gapped/offline indexing is not a requirement before committing. Treat as a spike, not a hard switch.

### D6 — Timeout cleanup

`subprocess.run(timeout=...)` SIGKILLs git mid-operation, possibly leaving `.git/*.lock`. Before any retry, remove stale lock files. Processes are reaped by `subprocess.run` (no `Popen` leak in the clone path).

## Consequences

**Positive:**
- No new dependency; the strongest credential isolation under concurrency.
- Aligns with the proven git-host architecture (shell out to git).
- D2 + D3 close the two real multi-tenant hazards (same-repo mutation race; shared-known_hosts race + TOFU MITM).

**Negative:**
- D2 (per-repo lock), D4 (refresh flow), D3 (known_hosts pinning) are net-new work — tracked as M13 WI-8 / WI-9.
- D5 revisiting full clone (ADR-0008 D5) needs a re-index validation that `blob:none` does not break offline indexing.

## References

- `src/git_utils.py` — clone + `GIT_SSH_COMMAND` + per-clone tempfile key + shared known_hosts.
- `src/indexer/incremental.py` — read-only `rev-parse`/`merge-base`/`diff`.
- `src/manager/__main__.py` — clone subprocess worker cap.
- ADR-0008 — SSH auto-clone (host-key posture superseded by D3; full clone revisited by D5).
- ADR-0034 — Multi-tenant pooled isolation + deploy-key credentials.
- `TASKS.md` Milestone 13 — WI-8 (repo refresh), WI-9 (known_hosts pinning).
