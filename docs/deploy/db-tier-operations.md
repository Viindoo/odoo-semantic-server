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
by itself: a unit crash or non-zero exit, PostgreSQL or Neo4j down, a repo that
cannot be cloned, and exit 3 of the nightly reindex (indexed, but OSM no longer
reflects some code and an operator must act, deploy.md s3.6). Exit 3 covers the
network and embedder cases too: the pre-scan fetch failing for **every** repo
of a run (at least two; a DNS / network outage or a credential they share,
`outages: fetch_outage`), the same repo's fetch failing in **two consecutive
runs** (`stale_repos`), and the embedder returning no vector for any chunk the
run sent (`outages: embedder_down`). One failed fetch that heals by the next
run, a partial embedding failure, and kept data (`lifecycle-audit`
`parser_gaps` / `unproven_drops`) are a WARNING or a report line, never a mail.

**What a mail holds.** Subject `[OSM ALERT] <host> <unit> exit <N>`. Body:
unit, host, result, exit status with its meaning (1 crash or failed repo,
3 lifecycle attention, 4 audit drift, 243 missing credential, ...), the run's
start time, the `Lifecycle needs attention` block when this run wrote one, the
last 60 lines of the unit's output and a runbook pointer. The output is the
unit's `StandardOutput=append:` log file (read from `systemctl cat`) or else
its journal for that invocation. A file-logged unit writes
`osm-run-start <time> invocation=<id>` as its first `ExecStartPre=`, and only
the lines after this invocation's marker count as this run. Passwords, DSN
credentials (also in SMTP / any URL), `NEO4J_AUTH=user/password`, bearer
tokens, `osm_` API keys, GitHub tokens (`ghp_`, `gho_`, `github_pat_`, ...),
AWS key ids, bare Fernet keys, long base64 runs (key material) and private
keys - also the body of a key block whose BEGIN line falls outside the 60-line
window - are redacted before sending, on the full line before it is cut to
500 characters. When a unit fails before its main process ran (an
`ExecStartPre=` failed, so systemd sets no exit status), the status comes from
`systemctl show -p ExecStartPre`: e.g. `exit 124 (ExecStartPre) - pre-flight
timed out: PostgreSQL not ready within 60s`.

**Who gets it.** Every active Web UI admin with an email
(`webui_users.is_admin AND is_active`, `email` not empty), read from PostgreSQL
at send time with the owner `PG_DSN`. Each successful read refreshes
`/var/lib/osm-alert/admins.json`, and so does the nightly reindex
(`ExecStartPre=-... alert_notify --refresh-admins`, best effort, sends
nothing). When the database is down (exactly when alerts matter) or lists no
admin email, the mail goes to that last known list - unless it is older than
`OSM_ALERT_ADMIN_CACHE_MAX_DAYS` (7), since it may name an admin demoted
since - plus `OSM_ALERT_FALLBACK_TO`. With neither, nothing is sent and the journal
logs `osm-alert: NO RECIPIENT ...`. One mail goes out with every recipient in
`Bcc` and `To: undisclosed-recipients:;`, so no admin sees another admin's
address (the `Bcc` header is not transmitted).

**How often.** The same alert is mailed once per `OSM_ALERT_REPEAT_HOURS`
(default 20, deliberately under the 24h period of the nightly timer: a run
that ends earlier than the night last mailed is still a new night, so a
lifecycle hold that persists every night mails every night). "The same alert"
means the same unit, exit status (and failed `ExecStartPre=`, if any) and
cause: the `Lifecycle needs attention` block when the run wrote one, else the
last error-looking line of the output (exception, `ERROR`, `failed`) with
timestamps, numbers, hex ids and paths masked. A new hold, another exit
status or a new cause of the same exit status mails at once. At most
`OSM_ALERT_MAX_PER_HOUR` (default 6) mails per hour over all units (crash
loops); an alert the cap holds is recorded in `state.json` and listed
("Also failed since the last mail, held by the hourly cap") in the next mail
that goes out. A suppressed alert still logs
`osm-alert: <unit> alert suppressed: ...`. A failed delivery logs
`osm-alert: DELIVERY_FAILED ...` and is not recorded, so the next failure
retries. The state lock is held only to decide and record, never while
mailing, and is waited for at most 20s (then the alert is sent without
dedupe rather than lost); the whole SMTP send has a 45s deadline; the worst
case stays under the unit's `TimeoutStartSec=180`. The hook always exits 0
and has no `OnFailure=`, so it can never alert about itself.

**Known limit: docker down.** The reindex and lifecycle-audit units have
`Requires=docker.service`. If docker.service fails to start, their start job
ends with result `dependency`: the unit never enters `failed`, so its
`OnFailure=` does not fire. A docker that is running with the database
container down is covered (the 60s `pg_isready` pre-flight fails: exit 124).
To be alerted when docker itself fails, give docker.service the same hook
(Human, sudo; it is the distribution's unit, so use a drop-in):

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
printf '[Unit]\nOnFailure=osm-alert@%%n\n' | sudo tee /etc/systemd/system/docker.service.d/osm-alert.conf
sudo systemctl daemon-reload
```

A docker that is merely stopped is started by `Requires=`; a box that is off
sends nothing at all (no freshness watchdog yet).

**Configuration.** No new secret: the unit reads the same env files as the Web
UI (`.env` for `PG_DSN`, `.env` / `webui.env` for the SES `SMTP_*`). Optional
knobs go in `/home/odoo-semantic/etc/alert.env` (0600, odoo-semantic):

| Variable | Default | Meaning |
|---|---|---|
| `OSM_ALERT_FALLBACK_TO` | empty | Comma-separated addresses used when the database cannot give an admin. Set it: it is the only recipient on a fresh box whose database is down. |
| `OSM_ALERT_REPEAT_HOURS` | 20 | Window in which the same alert is mailed once. Keep it under 24 (the nightly timer period). |
| `OSM_ALERT_MAX_PER_HOUR` | 6 | Cap on mails per hour, all units together. Held alerts are listed in the next mail. |
| `OSM_ALERT_ADMIN_CACHE_MAX_DAYS` | 7 | Age past which the last known admin list is ignored when the database is down. |

State (`state.json`, `admins.json`) lives in `/var/lib/osm-alert`
(`StateDirectory=osm-alert`). The unit body is managed: `make
check-systemd-overrides` audits it like the other units; tune it through
`alert.env` or a drop-in, never by editing `ExecStart=`.

Smoke test (Human, sudo): `dummy` is not a unit, so it is a test alert that
skips the dedupe (not the hourly cap) and **must deliver a real mail**:

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
