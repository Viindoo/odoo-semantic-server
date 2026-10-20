# Operating the DB tier safely

Short runbook for ops actions that touch the postgres/neo4j containers. Born
out of the 2026-05-19 incident where a half-migrated dev → service install
left the postgres container with a bind-mount pointing at a deleted cwd,
which Docker silently auto-created as an empty directory the next time
`docker compose up -d` ran from the wrong cwd. The container failed to start
(`not a directory: Are you trying to mount a directory onto a file?`), the
MCP service crash-looped 11k+ times in 26h, and no alert fired.

The defaults that make this safer are now in the repo (directory-style
bind-mount, project `name:` pin per ADR-0027, systemd `StartLimitBurst`,
`OnFailure=` alert hook, app-level degraded mode). This file documents the
*operating* discipline that goes with them.

> **Restart policy note (2026-05-21):** postgres now uses
> `restart: unless-stopped` (was `on-failure:5`). The directory-style mount +
> `name:` pin removed the hard-fail mode that `on-failure:5` was capping,
> while `on-failure:N` left the DB down after a clean host reboot (a clean
> stop is not a failure → MCP 503 for ~27 min on 2026-05-21). `unless-stopped`
> restores reboot-survival; degraded mode + `StartLimitBurst` + the alert hook
> still bound any pathological restart loop.

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
#   docker compose down                  # stops + removes BOTH postgres and neo4j
#   docker compose up -d postgres neo4j  # bring the WHOLE DB tier back, not just postgres
#   bash scripts/wait-pg-healthy.sh      # neo4j returns to healthy via its own healthcheck
```

`down` (not just `restart`) is required because container metadata is
recreated only on `down → up`. A bare `up -d` after editing compose
silently keeps the OLD bind-mount path on existing containers.

`down` removes the **whole project** (both postgres and neo4j), so the `up`
must name **both** services — bringing up only `postgres` would leave neo4j
stopped and the MCP server stuck in degraded mode until someone starts it.
Recreating both also rewrites each container's compose `working_dir` label
from the current cwd, healing any stale label left by an earlier wrong-cwd
or dev-tree creation.

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

Every shipped systemd unit (`odoo-semantic-{mcp,webui,astro,backup,reindex,lifecycle-audit}`,
`osm-ttl-cleanup`, the offsite-sync template) has `OnFailure=osm-alert@%n`
(always that spelling). When one fails, systemd starts
`osm-alert@<unit>.service`, which runs `python -m src.ops.alert_notify --unit %i`
as `odoo-semantic` and **mails every OSM admin** (#383).

**Why it exists.** From 2026-08-25 to 2026-09-25 the nightly reindex failed
32 nights out of 32 and nobody noticed: the hook only wrote a journal line. On
2026-10-18/19 a DNS failure made two nightly runs no-ops. Alerts are for
failures that stop OSM from reflecting the code and that the system cannot heal
by itself: a unit crash or non-zero exit, PostgreSQL or Neo4j down, a DNS or
network outage, a repo that cannot be cloned, the embedder down for a whole
run, and exit 3 of the nightly reindex (indexed, but a lifecycle hold needs an
operator, deploy.md s3.6).

**What a mail holds.** Subject `[OSM ALERT] <host> <unit> exit <N>`. Body:
unit, host, result, exit status with its meaning (1 crash or failed repo,
3 lifecycle attention, 4 audit drift, 243 missing credential, ...), the run's
start time, the `Lifecycle needs attention` block when this run wrote one, the
last 60 lines of the unit's output and a runbook pointer. The output is the
unit's `StandardOutput=append:` log file (read from `systemctl cat`) or else
its journal for that invocation. A file-logged unit writes
`osm-run-start <time> invocation=<id>` as its first `ExecStartPre=`, and only
the lines after this invocation's marker count as this run. Passwords, DSN
credentials, bearer tokens, `osm_` API keys, AWS key ids and private keys are
redacted before sending.

**Who gets it.** Every active Web UI admin with an email
(`webui_users.is_admin AND is_active`, `email` not empty), read from PostgreSQL
at send time with the owner `PG_DSN`. Each successful read refreshes
`/var/lib/osm-alert/admins.json`. When the database is down (exactly when
alerts matter) or lists no admin email, the mail goes to that last known list
plus `OSM_ALERT_FALLBACK_TO`. With neither, nothing is sent and the journal
logs `osm-alert: NO RECIPIENT ...`.

**How often.** The same alert (unit, exit status, attention block) is mailed
once per `OSM_ALERT_REPEAT_HOURS` (default 24): a lifecycle hold that persists
every night mails once a day, a new hold or another exit status mails at once.
At most `OSM_ALERT_MAX_PER_HOUR` (default 6) mails per hour over all units
(crash loops). A suppressed alert still logs
`osm-alert: <unit> alert suppressed: ...`. A failed delivery logs
`osm-alert: DELIVERY_FAILED ...` and is not recorded, so the next failure
retries. The hook always exits 0 and has no `OnFailure=`, so it can never
alert about itself.

**Configuration.** No new secret: the unit reads the same env files as the Web
UI (`.env` for `PG_DSN`, `.env` / `webui.env` for the SES `SMTP_*`). Optional
knobs go in `/home/odoo-semantic/etc/alert.env` (0600, odoo-semantic):

| Variable | Default | Meaning |
|---|---|---|
| `OSM_ALERT_FALLBACK_TO` | empty | Comma-separated addresses used when the database cannot give an admin. Set it: it is the only recipient on a fresh box whose database is down. |
| `OSM_ALERT_REPEAT_HOURS` | 24 | Window in which the same alert is mailed once. |
| `OSM_ALERT_MAX_PER_HOUR` | 6 | Cap on mails per hour, all units together. |

State (`state.json`, `admins.json`) lives in `/var/lib/osm-alert`
(`StateDirectory=osm-alert`). The unit body is managed: `make
check-systemd-overrides` audits it like the other units; tune it through
`alert.env` or a drop-in, never by editing `ExecStart=`.

Smoke test (Human, sudo): `dummy` is not a unit, so it is a test alert that
skips the 24h dedupe (not the hourly cap) and **must deliver a real mail**:

```bash
sudo systemctl daemon-reload
sudo systemctl start osm-alert@dummy.service
journalctl -u osm-alert@dummy.service --no-pager | tail
# Expect:
#   osm-alert: unit=dummy state=failed host=<hostname> ...
#   osm-alert: dummy.service alert mailed to N recipient(s) (active Web UI admins (database))
# and a mail "[OSM ALERT] <host> dummy.service test alert" in every admin inbox.
```

Preview what a real unit's alert would say, without sending or recording
(AI or Human, as odoo-semantic):

```bash
sudo -u odoo-semantic -H bash -c 'cd /home/odoo-semantic/odoo-semantic-mcp && set -a && . ./.env && set +a && \
  /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.ops.alert_notify \
  --unit odoo-semantic-reindex --dry-run'
```

`--force` sends even inside the dedupe window or over the cap (a manual resend).

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
