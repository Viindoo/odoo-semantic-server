# ADR-0031 — Python apps auto-load `.env` via python-dotenv

**Status:** Accepted (2026-05-19)
**Context:** Issue #141 — `python -m src.db.migrate` doesn't auto-load `.env`, manual invocations fail with a cryptic DSN error.

## Context

During the PR #133 production deploy on 2026-05-19, the operator ran
`python -m src.db.migrate` from an interactive shell and got:

```
✗ PostgreSQL DSN missing. Set PG_DSN env var OR `pg_dsn` in [database]
  section of odoo-semantic.conf.
```

The shell hadn't sourced `.env`. Systemd works because units use
`EnvironmentFile=-/opt/odoo-semantic-mcp/.env`, so the `migrate.service`,
`backup.service`, etc. all get env vars injected at process spawn. But
**every manual invocation** (deploy, dev repro, hotfix, smoke test) hit
the same trap, and the workaround `set -a; . .env; set +a` lives only
in folklore.

ADR-0006 doesn't address `.env` loading by Python apps. The closest
prior guidance was a single line in `docs/deploy.md` §2.1 stating
*"Python apps không đọc `.env`"* — a soft convention, not a load-bearing
architectural decision. The convention was useful when the canonical
config was `odoo-semantic.conf` (INI) and `.env` was reserved for
Docker Compose only. After M6+ the system has multiple Python entry
points (migrate, manager, cloner, cli, mcp server) and friction on
this assumption became material.

## Decision

`src/config.py` calls `python-dotenv`'s `load_dotenv(override=False)` at
import time. The call:

- Walks up from CWD to find `.env` (python-dotenv default).
- `override=False` guarantees that env vars already present in
  `os.environ` are **not** clobbered. Systemd's `EnvironmentFile=` and
  the operator's explicit `export FOO=bar` both still win.
- Is idempotent — safe to call multiple times across re-imports.
- Adds one new pinned dependency: `python-dotenv>=1.0`.

Centralizing the call in `src/config.py` (which is imported by every
CLI/server entry point) means all 6 known entry points pick it up
without per-call-site edits:

- `src/db/migrate.py`
- `src/manager/__main__.py`
- `src/cloner/__main__.py`
- `src/mcp/server.py`
- `src/cli.py`
- (future entry points that already import `src.config`)

In addition we introduce `config.dsn_missing_hint()` — a helper that
returns a multi-line error message surfacing the three fix paths
(shell source, explicit `export`, `odoo-semantic.conf`). Every CLI
entry point that fails when `PG_DSN` is missing now uses this helper
instead of inlining a one-liner, so the workaround is reachable from
the failure itself even if `.env` auto-load did not pick up the file
(e.g., running from a different CWD than the project root).

## Consequences

### Positive
- Manual `python -m src.db.migrate` works in a fresh shell with no
  prerequisite ritual.
- Operators no longer need to know the `set -a; . .env; set +a`
  incantation.
- Error message is self-documenting when DSN resolution still fails
  (wrong CWD, malformed `.env`, etc.).
- Resolution precedence remains: shell env vars > `.env` >
  `odoo-semantic.conf` > caller fallback. No production behavior
  change because env vars set by systemd `EnvironmentFile=` are
  already in `os.environ` before `_load()` runs.

### Negative
- A stale `.env` in CWD could be silently picked up by an operator
  who didn't realize one existed. Mitigation: `override=False` means
  the operator's explicit `export` wins; the production path is to
  rely on systemd-injected env or `odoo-semantic.conf`, not on `.env`
  being present.
- Importing `python-dotenv` adds ~1ms at startup (negligible).
- One new transitive dependency to track (`python-dotenv` is a small,
  zero-deps library, low maintenance burden).

### Neutral
- ADR-0006 §3 (Python 3.12 enforcement) and §2 (runtime version
  checks) are unaffected — this ADR only changes when `.env` is
  loaded, not how config values are resolved.

## Supersedes

The single line *"Python apps **không** đọc `.env`. Secrets cần khai
báo ở **cả hai** file."* in `docs/deploy.md` §2.1 (pre-2026-05-19).
The updated guidance reads: Python apps **also** auto-load `.env`
with `override=False`; production still prefers explicit `EnvironmentFile=`
or `odoo-semantic.conf` so `.env` is a dev convenience, not a
production requirement.

## Out of Scope

- Loading multiple `.env*` profile files (`.env.local`, `.env.production`)
  — not needed for current scope; can be added later if it becomes a
  real ask.
- Encrypted secrets at rest — already handled per-secret-class
  (`FERNET_KEY` via webui.env, ADR-0020).
