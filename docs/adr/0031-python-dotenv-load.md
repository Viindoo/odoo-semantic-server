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

`src/config.py` exposes a callable `init_dotenv()` that wraps
`python-dotenv`'s `load_dotenv(override=False)` with a one-shot
idempotency guard. The call:

- Walks up from CWD to find `.env` (python-dotenv default).
- `override=False` guarantees that env vars already present in
  `os.environ` are **not** clobbered. Systemd's `EnvironmentFile=` and
  the operator's explicit `export FOO=bar` both still win.
- Is idempotent — `_dotenv_initialized` sentinel skips repeated calls.
- Adds one new pinned dependency: `python-dotenv>=1.0`.

**`init_dotenv()` is invoked only from CLI `main()` entry points**, not
at module import time. Each of the 5 known entry points calls it as
its first action:

- `src/db/migrate.py:main()`
- `src/manager/__main__.py:main()`
- `src/cloner/__main__.py:main()`
- `src/cli.py:main()`
- `src/mcp/server.py` (`if __name__ == "__main__":` block)

**Why not module-import time?** PR #143's first iteration placed
`load_dotenv()` at module scope in `src/config.py`. CI immediately
broke on three jobs (`smoke-tests`, `integration-tests`,
`browser-tests-admin` — run id 26103956677): CI workflows do
`cp .env.example .env` before pytest, and `.env.example` shipped
`PG_DSN=postgresql://odoo_semantic:<PASSWORD>@localhost:5432/...`
with a literal `<PASSWORD>` placeholder. At test import time, every
test that touched `src.config` (transitively, almost all of them)
fired `load_dotenv` and injected the placeholder DSN into
`os.environ` — before pytest fixtures could set a real test DSN.
Subprocesses spawned by tests (e.g. `python -m src.manager` in
`test_manager_cli.py`) inherited the bogus DSN and failed
Postgres auth with `FATAL: password authentication failed`.

Moving the call inside `main()` makes the .env load fire **only for
operator/service invocations**, not for `pytest`. Tests already
inject env via fixtures or workflow `env:` blocks, and now see no
interference from whatever `.env` happens to be in CWD.

**Defense-in-depth**: `.env.example` was also sanitized in the same PR
to ship `PG_DSN=` blank (like `NEO4J_PASSWORD` and `PG_PASSWORD`),
preventing any future `.env` consumer from accidentally inheriting
a placeholder DSN.

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
