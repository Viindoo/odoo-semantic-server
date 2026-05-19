# Operating the DB tier safely

Short runbook for ops actions that touch the postgres/neo4j containers. Born
out of the 2026-05-19 incident where a half-migrated dev → service install
left the postgres container with a bind-mount pointing at a deleted cwd,
which Docker silently auto-created as an empty directory the next time
`docker compose up -d` ran from the wrong cwd. The container failed to start
(`not a directory: Are you trying to mount a directory onto a file?`), the
MCP service crash-looped 11k+ times in 26h, and no alert fired.

The defaults that make this safer are now in the repo (directory-style
bind-mount, `restart: on-failure:5`, systemd `StartLimitBurst`, `OnFailure=`
alert hook, app-level degraded mode). This file documents the *operating*
discipline that goes with them.

## Golden rule

> **Never run `docker compose` with `sudo` from a cwd that does not contain
> this repo's `docker-compose.yml`.** Docker daemon runs as root and will
> silently auto-create empty directories at any bind-mount source path that
> doesn't exist yet — turning every wrong-cwd invocation into a time bomb
> for the next container start.

This is true even if the *current* `up` succeeds: container metadata stores
the absolute bind-mount source it resolved, and that path is reused on
every subsequent `up`/`restart`/`start` until the container is `rm`'d.

## Quick verification

After any compose change OR migration, verify the active container has the
correct bind-mount sources:

```bash
docker inspect <container> --format '{{json .Mounts}}' | jq
```

For postgres you should see `Source: /opt/odoo-semantic-mcp/docker/initdb.d`
(or your repo path) and `Type: bind`, `Destination:
/docker-entrypoint-initdb.d`.

## Recreate-DB workflow

Use `make recreate-db` after any change to `docker-compose.yml`:

```bash
make recreate-db
# Equivalent to:
#   docker compose down
#   docker compose up -d postgres
#   bash scripts/wait-pg-healthy.sh
```

`down` (not just `restart`) is required because container metadata is
recreated only on `down → up`. A bare `up -d` after editing compose
silently keeps the OLD bind-mount path on existing containers.

## Dev → service install migration

Symptom we want to avoid: dev tree at `/home/<dev>/git/odoo-semantic-mcp/`
got deleted while the postgres container still pointed bind-mount source
at that deleted path. Migration steps that prevent this:

1. **From the OLD cwd:** `docker compose down` — destroys container
   metadata so the old bind-mount paths are gone for good.
2. Move code / git pull into the new cwd (e.g. `/opt/odoo-semantic-mcp/`).
3. **From the NEW cwd:** `make recreate-db` — `up -d postgres` resolves
   bind-mount sources against the new cwd and writes them into fresh
   container metadata.
4. Verify: `docker inspect <postgres-container> --format '{{json .Mounts}}'`
   shows the NEW absolute path. If anything still shows the OLD path,
   `docker compose down` again from the new cwd and repeat.
5. **Only after step 4 passes:** delete the old dev tree.
6. Audit systemd units for stale `User=` fields:
   ```bash
   grep -l '^User=' /etc/systemd/system/osm-*.service \
                    /etc/systemd/system/odoo-semantic-*.service 2>/dev/null \
     | xargs -I{} grep -H '^User=' {}
   ```
   Any unit still showing the dev username must be edited to the canonical
   service user (`odoo-semantic` by default) and `systemctl daemon-reload`
   issued.

## Backup is now skip-gracefully

`python -m src.cli backup` pre-checks the postgres container and exits 0
with a `SKIPPED:` line on stderr if it is not running. Nightly systemd
unit is no longer marked `failed` when the DB tier is down — the failure
shows up in the alert pipeline instead.

To force a hard fail when PG is expected up (e.g. ad-hoc backup), check
container status yourself first:

```bash
docker inspect --format='{{.State.Running}}' \
  "${POSTGRES_CONTAINER:-odoo-semantic-mcp-postgres-1}"
```

## Health check + diagnose

Two new touchpoints for ops:

- **`GET /health` (MCP server, port 8002):** the endpoint is always
  reachable (never crashes the process), but HTTP code reflects severity:

  | Body `status` | HTTP code | Meaning                              |
  |---------------|-----------|--------------------------------------|
  | `ok`          | 200       | both Neo4j + PG reachable            |
  | `degraded`    | 200       | one tier down, the other still works |
  | `error`       | 503       | BOTH tiers unreachable               |

  **Probe wiring:**
  - **Readiness probe** (k8s `readinessProbe`, nginx `health_check`): use
    `/health` and treat HTTP 2xx as ready, 5xx as not ready. This routes
    traffic away from a fully-broken pod without killing it.
  - **Liveness probe** (k8s `livenessProbe`): do **NOT** point at
    `/health` — a transient `error` state would force-kill the pod and
    amplify the outage. Use a plain TCP check on port 8002 instead, or
    a custom endpoint that only checks process responsiveness (e.g. a
    /ping route returning a static 200).
- **`python -m src.cli diagnose [--json]`:** one-shot cross-tier check
  (PG container, Neo4j container, MCP /health, bind-mount type for
  `docker/initdb.d/`). Exits 1 if any check fails; suitable for cron
  monitoring.

## Alert wiring (OnFailure=osm-alert@%n)

Every shipped systemd unit (`odoo-semantic-{mcp,webui,astro,backup}`)
has `OnFailure=osm-alert@%n.service`. The pattern unit
`[email protected]` ships with a journal-only default — to wire real
notifications (email, Slack webhook, PagerDuty), edit its `ExecStart=`
to call your notifier and keep the `osm-alert:` log prefix so scrapers
keep working.

Sanity test (no real failure needed):

```bash
sudo systemctl daemon-reload
sudo systemctl start [email protected]
journalctl -u [email protected] --no-pager | tail
# Expect: a single WARNING line tagged `osm-alert: unit=dummy.service ...`
```

## Degraded mode

When the postgres pool fails to initialise at MCP startup, the server now
stays UP in degraded mode instead of crashing:

- `GET /health` returns **HTTP 200** with `status: degraded` and
  `postgres: error:<cause>` (Neo4j still reachable). It returns **HTTP 503**
  with `status: error` only when BOTH tiers fail — see the probe-wiring
  table above before using `/health` for k8s probes.
- Any authenticated request returns 503 with body
  `{"status":"degraded","pg":"unavailable"}` and a
  `Retry-After: <PG_BG_RETRY_INTERVAL_SECONDS>` header (default 30, single
  source: `src/constants.py`). The body is intentionally minimal — the
  underlying `psycopg2.OperationalError` text (which contains internal
  hostnames / DB usernames) appears in the **server-side journal log
  only**, never on the wire (CWE-209 defence-in-depth).
- A background task retries pool init on the same cadence as `Retry-After`;
  on success it logs `PG pool initialized after background retry —
  degraded mode cleared` and 503 responses stop. The task holds a strong
  reference and is explicitly cancelled on lifespan shutdown so it does
  not leak across hot-reload boundaries.

This is a deliberate trade-off: better to keep `/health` reachable so
ops sees the truth than to crash uvicorn (which would also block `/health`
and look identical to "the host is dead" from outside).
