---
status: draft
scope: architecture/deployment
reads-with:
  - overview.md
  - ../security/access-control.md
---

# Deployment

Three topologies, in order of maturity.

## 1. Dev topology (internal)

3 machines in a Tailscale tailnet. No public ports, no port forwarding.

```text
+-------------------+          +-------------------+
|   Laptop (dev)    |          |  Laptop (co-dev)  |
|   tag:dev         |          |  tag:dev          |
|   VS Code + git   |          |  VS Code + git    |
+---------+---------+          +---------+---------+
          |                              |
          | SSH :22                      | SSH :22
          | MCP :8765 (Tailscale only)   | MCP :8765
          |                              |
          +--------------+---------------+
                         |
                         v
            +---------------------------+
            |   Server (WSL)            |
            |   tag:server              |
            |                           |
            |   - FastMCP :8765         |
            |   - Postgres :5432 (LO)   |
            |   - Indexer service       |
            |   - 12GB VRAM (optional   |
            |     self-host embedding)  |
            +---------------------------+
```

**Tailscale ACL (simplified):**

```json
{
  "acls": [
    { "action": "accept", "src": ["tag:dev"], "dst": ["tag:server:*"] }
  ]
}
```

## 2. Self-hosted (customer, Docker Compose)

Single command:

```bash
docker compose up -d
```

Compose file bundles:

- Postgres 16 + pgvector
- Indexer container
- FastMCP container
- Optional: self-hosted embedder container (profile-guarded)

Customer points the indexer at their addon paths (read-only volume). No network egress required if using self-hosted embedder.

## 3. Hosted (Viindoo, per customer)

- Hetzner CPX41 as baseline machine
- One Postgres **schema** per customer — never shared tables
- Reverse proxy terminates TLS, enforces auth, rate-limits
- Customer code lives at rest on encrypted volume (LUKS)
- Daily logical backup → Hetzner Storage Box
- One MCP server replica per machine; scale by adding machines

Security posture for Hosted is detailed in [`../security/`](../security/).

## Version compatibility

Supported Odoo versions: 2 most recent major versions at any time. Older versions are EOL'd with 3 months notice.

## Monitoring (minimum)

- Liveness: FastMCP `/health` endpoint
- Indexer: last successful SHA per project
- Cost: embedding tokens per project per day (alert if > budget)
- Security: audit log tail for anomalous tool-call patterns
