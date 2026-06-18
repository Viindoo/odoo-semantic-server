# Operator Runbooks

Short, action-oriented playbooks for operator tasks. Read top-to-bottom; run commands in order; verify each step before proceeding to the next.

| Runbook | Purpose |
|---|---|
| [post-pr-ops.md](post-pr-ops.md) | Post-PR deploy steps (migrate → restart → smoke) |
| [rls-cutover.md](rls-cutover.md) | One-time switch from owner-DSN to `osm_reader` reads |
| [fernet-provision.md](fernet-provision.md) | Move FERNET_KEY env-file → systemd `LoadCredential=` |
| [backup-confirm-and-dr-drill.md](backup-confirm-and-dr-drill.md) | Verify nightly bundle + quarterly DR drill steps |
| [prod-smoke-24-tools.md](prod-smoke-24-tools.md) | Authed smoke across MCP tools post-deploy (covers tools #11-31 + resources R1-R9) |
| [nginx-ratelimit-apply.md](nginx-ratelimit-apply.md) | Apply 4 nginx rate-limit zones (mcp/api/waitlist/install) |
| [offsite-backup-bootstrap.md](offsite-backup-bootstrap.md) | rclone + S3-compat + systemd timer for offsite encrypted backup |
| [neo4j-container-recreate.md](neo4j-container-recreate.md) | Recreate Neo4j container from canonical compose path (fix drift, pick up env-var changes) |
