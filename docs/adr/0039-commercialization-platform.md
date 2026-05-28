# ADR-0039 — Commercialization Platform: Control Plane / Data Plane

**Status:** Proposed
**Date:** 2026-05-28
**Milestone:** M10B (Commercialization Wow — supersedes the Stripe-based "Billing Wow" plan)

---

> Relates to [ADR-0011](0011-webui-session-auth.md) (session auth), [ADR-0017](0017-oauth-arctic-oslo.md)
> (OAuth / multi-IdP — amended alongside this ADR), [ADR-0029](0029-implicit-session-context.md)
> (per-key session context), [ADR-0034](0034-multi-tenant-pooled-isolation.md) (tenant isolation —
> re-scoped to *data plane* by this ADR), and [ADR-0038](0038-tenant-rbac-web-ui-write-side.md)
> (tenant RBAC web-UI write-side).

## Context

OSM is going commercial: customers buy access by plan/tier. Two buyer segments must be served and
they are not interchangeable:

| | International self-serve (developers) | Regional / ecosystem (sales-led) |
|---|---|---|
| Channel | Marketplace / Apps Store, plugin install | Sales quotation via the company ERP |
| Payment | International cards | Bank transfer / domestic rails |
| Invoice | Receipt from the payment provider | Legally-mandated domestic e-invoice (VAS) |
| Login | Google / GitHub (no company account) | One company login across products |

Two problems block the previously-planned billing milestone:

1. **The prior M10B plan is coupled to Stripe** (`stripe>=10`, `stripe_subscription_id`,
   `stripe_customer_id`). **Stripe does not onboard entities incorporated in Vietnam** — the
   operating entity's jurisdiction. A Stripe-coupled build would be discarded once that constraint
   surfaces.
2. **There is no plan/tier gating.** Every API key today reaches all MCP tools without limit
   (a uniform 120 rpm rate-limiter, `src/mcp/middleware.py`, is the only throttle). No tier can be
   sold until usage is metered and gated.

The same control surfaces — identity, billing orchestration, entitlement — will be reused by future
Viindoo products. Designing them **product-aware now costs ~0**; retrofitting later is expensive.

## Decision

### D1 — Control Plane / Data Plane separation

A thin shared **control plane** (Identity, Billing orchestration, Entitlement) sits in front of
isolated per-product **data planes** (each product keeps its own core, data, tenant isolation,
provisioning, and quota enforcement). OSM is the first data plane. The control plane holds only
metadata (who-is-who, what-plan); it never centralizes product data.

### D2 — Posture: extract-gradually (not a separate service yet)

Plant platform-aware **schema + naming inside OSM now** (a "Viindoo Account" identity boundary, an
entitlement record carrying `product_id`, an Activation API endpoint). Extract a standalone
control-plane service **only when a second product needs it**. Rationale: no speculative platform
build while OSM is the sole revenue product; the cost of being platform-aware up front is naming
and schema shape, not extra services.

### D3 — Entitlement Activation API is the keystone contract

Every commerce front-end calls **one contract**:

```
grant / revoke / update  →  { email, product_id, plan, seats, status, limits }
```

It is the bridge between "payment happened somewhere" and "access is activated". Built once; reused
by every product. The data plane provisions (mints API key, creates tenant) and enforces quota off
the entitlement record.

> **Term disambiguation (required).** "Entitlement" in this ADR means a **subscription grant** —
> which plan/product/seats a buyer holds. It is distinct from the existing access-control sense in
> the codebase (`src/mcp/server.py` ~L2996, `ADR-0034` ~L403: "the caller is *entitled to* read
> node X"). The Activation API and its tables MUST NOT reuse the access-control wording; name them
> `subscription` / `plan` to avoid the collision.

### D4 — Payment rail: Merchant-of-Record (not a PSP)

Use a **Merchant-of-Record (MoR)**: Polar.sh primary, Paddle fallback. The MoR is the legal seller
of record to the end customer — it handles cross-border VAT/GST in 130+ countries and remits a
single NET B2B payout. This collapses thousands of cross-border invoices into one revenue line.
Stripe (a PSP, not a MoR) is rejected: it does not onboard the operating entity's jurisdiction and
does not produce domestic e-invoices.

The **regional/domestic segment** routes through the company's existing ERP order + e-invoice (VAS)
flow — a thin **activation webhook**, not a two-way data sync. The ERP remains the accounting system
of record for that segment.

### D5 — Dual commerce adapters, one Activation API

```
   Marketplace / plugin  ──> Polar.sh (MoR) ──────────┐
                                                       ├──> Entitlement Activation API ──> data plane
   Sales quotation       ──> ERP sale.order webhook ──┘     (provision, mint API key, gate quota)
```

Polar.sh issues a **native license key** on purchase → webhook → Activation API → OSM mints a real
API key + provisions the tenant + sets the plan. The license-key model maps cleanly onto OSM's
unit-of-access. **GTM sequencing: international self-serve (Polar) ships first**; the ERP/VAS adapter
follows.

### D6 — Quota gating + usage metering in the data plane (architecture-neutral)

Gating + metering live in the product, behind a **generic `limits` interface** (not seat-only — a
future event-cost-cap must fit the same shape). Build on what already exists: the per-key
sliding-window limiter (`src/mcp/middleware.py`) becomes plan-aware, and the existing `usage_log`
table (`migrations/0001_initial.sql`) is queried to enforce daily/period quotas. This is the
**blocker that is independent of every other choice here** and ships first.

### D7 — Identity: multi-IdP "Viindoo Account"

A single account boundary acts as a Service Provider accepting multiple Identity Providers
(Google / GitHub for developers; a company OIDC provider for the ecosystem segment). Detail and the
extensible-IdP-registry decision live in the [ADR-0017](0017-oauth-arctic-oslo.md) amendment. The
existing account-linking-by-verified-email logic is the reuse asset.

## Schema shape (product-aware from the start)

- `plans` — tier definitions (`limits` JSONB, not hard-coded tool allow-lists or seat-only).
- `subscriptions` — `product_id`, `plan`, `seats`, `status`, **`external_ref`** (vendor-agnostic;
  NOT `stripe_subscription_id`).
- `api_keys.plan_id` — links an existing key (already tenant-scoped, `m13_002`) to its plan.
- Entitlement payload is the D3 contract; the Activation API is the only writer.

## Consequences

- **M10B is rewritten** (`TASKS.md`): Stripe tasks dropped; phased P0 (gating) → P1 (Activation API
  + Polar adapter) → P2 (multi-IdP + regional/VAS, buyer≠user split) → P3 (support tiers, dunning,
  cross-product bundle groundwork).
- **ADR-0034 is re-scoped** to *data-plane tenant isolation*; this ADR owns the *control-plane*
  commercialization architecture.
- **ADR-0017 is amended** with an extensible IdP registry.
- New billing migrations are product-aware (`product_id`, `external_ref`) — no vendor coupling.
- The prior `TASKS.md` reference to "ADR-0027 — Billing domain model" was a number collision
  (0027 is the accepted system-user deployment ADR); the billing/commercialization ADR is **this
  one (0039)**.

## Open questions (human / legal — outside engineering)

- MoR KYB onboarding for the operating entity (entity type, document set, payout currency).
- Accounting treatment of the NET MoR payout; foreign-exchange compliance.
- Whether the company website already accepts international payment through an existing rail that
  could be reused instead of a new MoR onboarding.
