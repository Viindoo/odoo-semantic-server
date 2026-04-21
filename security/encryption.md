---
status: placeholder
scope: security/encryption
date: 2026-04-21
reads-with:
  - ../architecture/deployment.md
  - threat-model.md
---

# Encryption

**Status**: placeholder.

## At rest

- Volume-level LUKS on every Hetzner machine hosting customer data
- Key stored in cloud KMS; loaded at boot via attested process
- Postgres TDE is not used (LUKS covers it) — revisit if regulatory driver appears

## In transit

- TLS 1.3 only on all external endpoints
- Internal service-to-service within one machine: Unix sockets where possible, loopback otherwise
- Internal service-to-service across machines: Tailscale/WireGuard; fall back to mTLS if Tailscale unavailable

## Key management

- One KMS-managed master key per environment (dev / staging / prod)
- Per-customer data keys derived via HKDF from master; enables per-customer cryptographic erasure
- Key rotation schedule: master annually, data keys on customer offboarding

## Secrets

- No secrets in the repo
- Local dev: `.env` excluded from git; onboarding doc in `README.md` lists required vars
- Production: secret manager injects at runtime; no secrets in Docker images

## Embedding provider edge

- If Voyage API is default (per ADR-0002), queries + chunks are sent over TLS to Voyage endpoint
- Hashes + metadata never sent; only chunk text + embedding
- Customer can disable API mode and use self-hosted embedder, which keeps all data on their infra

## Open questions

- HSM vs cloud KMS? Leaning: cloud KMS for velocity, migrate to HSM if enterprise customer requires
- Do we log hashed chunk IDs (to correlate with vector results) in the audit log? Leaning: yes, hashed
