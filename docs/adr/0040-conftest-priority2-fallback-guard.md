# 0040 — conftest.py Priority 2 fallback guard against prod Neo4j collision

**Status:** Accepted (2026-05-28)
**Related:** [ADR-0006](0006-environment-harness.md) (environment harness), [ADR-0009](0009-pattern-catalogue-community-contribution.md) (pattern catalogue — test-enforced minimum)

---

## Context

On 2026-05-26 a contributor ran `make test-integration` on a machine that
also hosted a live Neo4j at `:7687`.
Testcontainers (Priority 1) failed to spin up — Docker daemon access issue —
and `tests/conftest.py` fell through to the **Priority 2** path: a direct bolt
connect to `bolt://localhost:7687` using the default test credentials
`("neo4j", "password")`.

The 8 parallel `verify_connectivity()` calls auth-failed × 8 against the
live Neo4j (which uses a non-default password). Neo4j's default
`auth_max_failed_attempts=3` rate-limit triggered immediately: connections 4–8
were rejected with `Neo.ClientError.Security.AuthorizationExpired`. The
rate-limit cooldown took approximately two minutes, briefly blocking all MCP
queries on the live instance.

The Priority 2 path is a legitimate dev convenience: `make neo4j-up` starts a
local Docker Neo4j with the default password, and contributors can run
integration tests against it without testcontainers overhead. The problem is
not the path itself but the absence of a guard that prevents it from firing
against a non-test Neo4j with the same address.

---

## Decision

Add a `_priority2_guard_blocks_run()` helper in `tests/conftest.py`.

The function returns `True` — meaning the Priority 2 connect is **blocked** —
when ALL three conditions hold simultaneously:

| Condition | Default value that triggers the block |
|-----------|---------------------------------------|
| (a) `CI` env var is absent or not exactly `"true"` | not set |
| (b) `NEO4J_TEST_URI` is unset or equals `"bolt://localhost:7687"` | `"bolt://localhost:7687"` |
| (c) `NEO4J_TEST_PASSWORD` is unset or equals `"password"` | `"password"` |

When `_priority2_guard_blocks_run()` returns `True`, the Priority 2 branch
calls `pytest.skip(...)` with an explicit reason and override hint, rather than
attempting the connect.

### Override (legitimate local dev use)

Set **either** `NEO4J_TEST_PASSWORD` **or** `NEO4J_TEST_URI` to a non-default
value before running `make test-integration`:

```bash
# Against a local non-default Neo4j (e.g. started with a custom password):
NEO4J_TEST_URI=bolt://localhost:7688 make test-integration
# — or —
NEO4J_TEST_PASSWORD=mydevpassword make test-integration
```

A single non-default value disarms the guard; both overrides together are
equally valid.

### Why `pytest.skip` rather than a hard fail

1. **Contributor experience:** the skip reason message includes the exact
   override hint. No silent failure, no cryptic error — the contributor sees
   immediately what to do.
2. **CI is unaffected:** CI always sets `CI=true`, so condition (a) is never
   satisfied in CI; the guard never fires.
3. **Consistent fallback semantics:** Priority 1 already uses `pytest.skip`
   when testcontainers are unavailable. Priority 2 matching that pattern means
   "both paths unavailable → skip" is the uniform behaviour; a hard fail would
   break CI-less developer workflows where skipping integration tests is
   expected and documented.

---

## Consequences

- `tests/conftest.py` exports `_priority2_guard_blocks_run` as a public
  internal helper (underscore prefix — test-module internal, not part of any
  public API).
- `tests/test_conftest_priority2_guard.py` (W1A-3) verifies the guard logic
  with 10 cases: all-defaults-no-CI, CI=true, non-default password,
  non-default URI, partial override, and 5 non-`"true"` CI values.
- The skip message in `conftest.py` references this ADR by file path
  (`docs/adr/0040-conftest-priority2-fallback-guard.md`) so contributors can
  locate the rationale without searching.
- **`CONTRIBUTING.md` §"Chạy Tests"** has a dedicated paragraph explaining
  the guard and the override env vars; cross-reference this ADR by number.
- No change to the CI workflow; the neo4j `auth_max_failed_attempts` default
  is bumped separately in `docker-compose.yml` as part of this wave's
  hardening (TD-4).

---

## Alternatives Considered

**Hard fail instead of skip:** Rejected. A hard `pytest.fail()` would make
`make test-integration` an error on machines with no Docker and no local Neo4j,
which is already a documented and accepted configuration (skip-only). Fail-loud
would also break the clean skip story for the Priority 1 testcontainers path.

**Guard at the shell level (`make test-integration` Makefile target):** Rejected.
The guard belongs at the Python layer where the actual connect is attempted; a
Makefile guard would be bypassed by any direct `pytest` invocation and would
duplicate logic that is better tested via `test_conftest_priority2_guard.py`.

**Raise `auth_max_failed_attempts` instead of guarding the test:** Complementary
(done in W1A-4), not a substitute. A higher attempt limit reduces blast radius
but does not prevent the conftest from making unnecessary auth calls to a live
production Neo4j.

---

## Amendment (2026-06-05) — remote-target destructive-DB guard (PR #266 follow-up)

### Gap

The original `_priority2_guard_blocks_run` guard only fires when the target uses
**default credentials** (`NEO4J_TEST_PASSWORD == "password"` and the default URI).
A contributor who exports `NEO4J_TEST_URI` / `PG_TEST_DSN` pointing at a **real
store with valid (non-default) credentials** disarms that guard — and the
integration fixtures (`clean_neo4j` runs `DETACH DELETE`; the Postgres fixtures
`TRUNCATE`/`DELETE`) would then wipe a production database. The default-creds
guard cannot catch this because the whole point of a prod DSN is that it carries
non-default creds.

### Decision

Add a second, orthogonal guard `_assert_test_db_target_is_safe(env_var, default)`
in `tests/conftest.py`, wired at the top of the `neo4j_driver` and `pg_conn`
fixtures. It hard-`pytest.skip`s when ALL of:

1. the resolved host (parsed from the bolt URI or libpq DSN, both URL and
   keyword `host=` forms) is **positively a non-loopback host**
   (not `localhost` / `127.0.0.1` / `0.0.0.0` / `::1` / empty), AND
2. `CI` is unset/false, AND
3. `OSM_ALLOW_REMOTE_TEST_DB` is unset/false.

A remote host on a dev box is the only configuration refused. Unparseable /
empty host → treated as loopback (we only block on a *positively identified*
remote host) so the guard never produces false-positive skips on odd-but-local
targets.

### Why this is CI-safe

GitHub Actions sets `CI=true` **and** points both targets at `127.0.0.1`
(loopback) — either condition alone exempts CI, so the guard is doubly inert
there. The local default (`localhost`) is loopback, so the normal
`make test-integration` flow is never blocked. The escape hatch
`OSM_ALLOW_REMOTE_TEST_DB=1` covers the legitimate "remote but disposable test
instance" case without a code change.

### Why skip (not fail), consistent with the original decision

Same rationale as the Priority-2 guard above: a hard fail would turn a
misconfiguration into a red suite on machines that legitimately have no local
DB. A skip surfaces the reason (the message names the offending env var, the
resolved host, and the override) without breaking the no-DB-present story.
