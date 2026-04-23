---
status: accepted
scope: decisions/0005
date: 2026-04-22
deciders: [project-lead]
---

# ADR-0005: Tailscale tenant — personal vs Viindoo tailnet

## Context

The dev topology for `odoo-semantic-mcp` Hosted BYOC tier assumes a
Tailscale mesh between the customer's Hetzner box and the operator's
workstation for out-of-band admin (indexer CLI, debugging, tenant
provisioning). `docker-compose.yml` carries a commented-out `tailscale`
sidecar block that needs a `TS_AUTHKEY` to come alive.

The open question is **whose tailnet the auth key belongs to**:

- **Personal tailnet** owned by the operator — fast, no ACL friction, but
  the key and device trust live in one individual's account.
- **Viindoo corporate tailnet** — centrally governed, shared ACLs, but
  requires Viindoo to provision the tailnet, invite users, and own device
  lifecycle. As of 2026-04-22 Viindoo does not have one.

The decision blocks nothing in P1–P2 because the server binds on
`127.0.0.1` during local dev. It becomes pressure-relevant the moment a
**second developer** joins the project or the **first Hosted BYOC pilot**
goes live — whichever comes first.

## Drivers

- **Time-to-unblock**: Hosted BYOC pilot onboarding must not wait on
  corporate IT provisioning a new tailnet
- **Auditability**: customer-touching access needs traceability beyond
  one person's personal account — eventually
- **Cost**: Tailscale Starter is free for up to 3 users; Team tier kicks
  in at 4+
- **Non-goal**: we are not solving long-term customer-facing auth here —
  that's the Hosted-tier authentication story and belongs to P5

## Considered options

### Option A — Personal tailnet (operator-owned)

- **Pros**: zero provisioning delay; full control; free at current
  scale; familiar tooling
- **Cons**: tied to one individual's Tailscale account; no corporate
  audit trail; if the operator leaves Viindoo the tailnet goes with them

### Option B — Viindoo corporate tailnet

- **Pros**: centralised ACLs; device lifecycle tied to employee
  lifecycle; survives individual departure
- **Cons**: does not exist today; needs IT time to stand up; blocks
  every subsequent change behind an IT ticket

### Option C — No Tailscale in P1

- **Pros**: fewest moving parts; no decision to make
- **Cons**: every admin operation needs SSH port exposure or a
  cloud-provider bastion — both higher-friction and higher blast-radius
  than a mesh VPN

## Decision

**Option A — personal tailnet**, explicitly scoped to P1–P4.

Rationale:

- The team is one operator today; Option A's governance downside
  (device lifecycle tied to one person) does not yet bite.
- Option B's delay (standing up a corporate tailnet) would block the
  Hosted BYOC pilot for which we do not yet have a first customer.
- Option C's friction (SSH/bastion per admin operation) is higher than
  the governance tax of Option A at the current scale.

The sidecar block stays **commented out** in `docker-compose.yml`. The
operator enables it at the moment the first Hosted customer lands, by
dropping a `TS_AUTHKEY` into `.env` and uncommenting the sidecar. No
code change required.

## Consequences

- **Positive**: unblocks Hosted pilot onboarding with zero IT
  dependency; zero cost at current scale.
- **Negative**: if the operator leaves the project, the tailnet and the
  customer admin path go with them. Mitigation: document the path
  publicly so handover is cheap (see "Follow-ups" below).
- **Follow-ups**: revisit when (a) team grows to ≥3 developers, (b)
  Viindoo IT stands up a corporate tailnet, or (c) a compliance-bound
  customer requires an audited VPN path. Review on the first trigger;
  hard review no later than P4 end.

## Kill criteria

Move off Option A if any of:

- A customer's contract requires a corporate-owned VPN path (compliance
  trigger).
- A second operator is added to the admin rotation (governance trigger).
- Tailscale changes Starter-tier pricing in a way that makes the
  personal plan more expensive than the Team plan at our user count.

Review the decision at every one of these triggers. Hard deadline for a
re-review: **end of P4** (roughly week 12 from now).

## References

- ADRs: ADR-0004 (multi-tenant model) — the overlay schema that makes
  per-customer admin operations a frequent-path workflow.
- Docs: `architecture/deployment.md` (dev topology), `docker-compose.yml`
  (commented `tailscale` sidecar block).
- Specs affected: none yet; the Hosted tier's auth story (P5) will
  supersede this ADR as customer-facing auth, not admin-path VPN.
