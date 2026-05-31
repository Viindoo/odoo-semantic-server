# ADR-0039 — Commercialization Platform: Control Plane / Data Plane

**Status:** Proposed — P0 shipped (PR #200); P1 keystone implemented on `feat/m10b-p1-billing` (pending PR/merge); P2 multi-IdP / buyer-user-split / ERP-VAS still pending.
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
> node X"). The Activation API and its tables MUST NOT reuse the access-control wording; name the
> tables `subscriptions` / `plans` (plural, see schema below) and reserve `subscription` / `plan`
> (singular) for conceptual/API param usage to avoid the collision.

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

## P0 — Shipped (PR #200, v0.13.0)

`plans` table (4 tiers) + `api_keys.plan_id` FK + `usage_counter` table (migrations m13_006 +
m13_007). Plan-aware MCP middleware (`X-RateLimit-*` / `X-Quota-*` headers; 429 differentiation).
`GET /api/account/usage` + `/account/usage` Astro dashboard.

**P0 extended in `feat/m10b-p0-rbac-quota-ui`** — admin tooling wave (plan/override/reactivate) +
tenant assign UI + key reactivate endpoint. Decisions recorded in
[ADR-0041](0041-unlimited-plan-and-key-overrides.md): unlimited plan slug (D4), per-key override
columns (D1), cascade helper (D3), override semantics (D5). Migration m13_009. See ADR-0041 for
full context.

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

---

## Amendment — 2026-05-29

**Free-plan consolidation (fix/auth-ux-oauth-cache-plans):** The `free-grandfathered` plan (seeded
during the free→pro→team transition in v0.13.0) is now deprecated. Migration
`m13_013_consolidate_free_plans.sql` repoints all 6 existing `free-grandfathered` keys (internal/admin/CLI)
to the `unlimited` plan (ADR-0041 D5 SSOT: no quota/rpm limit), then deletes the plan row. New
signups continue to land on the public `free` plan (100 calls/month, 30 rpm). Rationale: operational
simplicity — one `free` tier for customers, unlimited for admins; no schema artifact.

**Auto-onboarding:** New signups (password + OAuth flows) now auto-assign the `free` plan and
auto-mint one API key (name: `auto_{user_id}_{timestamp}`), eliminating the prior manual
"generate key" step. Post-login landing directs users immediately to `/account/api-keys` to see
their key and copy it. Closes the customer-onboarding friction gap from PR #213 auth unification.

---

## Amendment — M10B P1 billing (2026-05-30)

**Branch:** `feat/m10b-p1-billing`. Implements the D3 Entitlement Activation API keystone, Polar.sh
MoR adapter, and claim-on-login provisioning. Migration `m13_014_billing_p1.sql`.

**Schema deviations from the original D3 sketch:**

- **`EntitlementGrant` frozen dataclass** (`src/billing/activation.py`) is the single Activation
  contract. Uses **integer `plan_id` (NOT a text `plan` slug)** — the slug is resolved to `plans.id`
  before the dataclass is built, enforcing relational integrity end-to-end. The `subscriptions` table
  uses integer FKs throughout (`plan_id→plans`, `claimed_user_id→webui_users`,
  `api_key_id→api_keys`, `tenant_id→tenants`).

- **`subscriptions.limits` JSONB column REJECTED** — limits live ONLY in `plans`, resolved via
  `plan_id` at runtime. Admin plan edits therefore propagate to every subscription immediately
  without a backfill. `subscriptions` stores only commercial/lifecycle data (status, seats, source,
  external_ref, money snapshot, timeline, buyer_email).

- **`product_id` column DEFERRED to P2** — sole product today; adding it when a second product
  arrives is the extract-gradually posture from D2 (zero-cost naming now, physical column only when
  needed).

- **NEW `billing_webhook_events` idempotency ledger** (`(vendor, event_id)` UNIQUE) — money-safety
  addition not in the original D3 sketch. Provides replay protection, signature-validity audit trail,
  and processing-error capture. Every webhook attempt is recorded (signature_valid=FALSE for bad
  signatures) before any provisioning side-effect.

- **NEW `plans` commercial columns** (`price_cents` **BIGINT**, `currency` with ISO-3-letter
  `CHECK ~ '^[A-Z]{3}$'`, `billing_interval`, `trial_days`, `is_archived`) — data-driven pricing
  page (`GET /api/plans`) and webhook amount context. `price_cents` is BIGINT (INTEGER→BIGINT upgrade
  idempotent in the same migration; rationale: VND whole-units can exceed INT4 2.1B max).
  Pricing seed (in-migration, idempotent, USD-only): Free $0/200-calls, Pro $19/seat/month,
  Team $39/seat/month. The `subscriptions` table carries matching `amount_cents` **BIGINT** and
  `currency` with the same ISO-3-letter CHECK. Additionally: `UNIQUE(source, external_ref)` composite
  key replaces any former global `external_ref UNIQUE` (composite ensures the same Polar order ID
  can appear across future vendors without false collision); `last_event_at TIMESTAMPTZ` column
  acts as a monotonic guard — out-of-order webhook events are dropped when their timestamp is
  older than the stored value, preventing late arrivals from reverting an upgrade.

**Provisioning model — claim-on-login:**

A purchase keys a `subscriptions` row to the buyer email (`buyer_email` snapshot) with
`claimed_user_id=NULL`. On the buyer's next VERIFIED login (email-verify / OAuth / password with
`email_verified=TRUE`), `claim_subscription_for_user(user_id, email)` runs best-effort, upgrades
the existing free API key in-place via `set_api_key_plan_and_overrides`, and flushes the middleware
plan cache via `_cache_invalidate_by_key_id`. Anti-spoof: claim only fires on verified-email paths.
**Buyer ≠ user split DEFERRED to P2** (handles B2B billing contact vs seat holder).

**Payment rail — Polar.sh MoR, Standard Webhooks signature:**

`POST /api/webhooks/polar` (public, auth-exempt, HMAC-verified). Signature algorithm: base64
HMAC-SHA256 over `"{webhook-id}.{webhook-timestamp}.{body}"`. Secret may carry a `whsec_` prefix
(stripped + base64-decoded to raw key bytes). Fail-closed: missing `POLAR_WEBHOOK_SECRET` → 503,
never processes an unsigned payload. Replay tolerance and per-IP rate-limit are runtime-configurable
via `billing.*` app_settings (3 new Tier-1 settings → 19 total Tier-1 settings).

On cancel/refund/revoke: `revoke_entitlement` marks subscription `cancelled`, downgrades the linked
API key to the `free` plan, and flushes the middleware plan cache. Key stays `active=TRUE`.

**Admin Activation API:** `POST /api/admin/entitlements` (grant) + `POST /{ref}/revoke` +
`PATCH /{ref}` (update) + `GET` (list). Mutating routes require `require_admin_with_fresh_mfa`
(DB-sourced admin check + MFA step-up per ADR-0026/ADR-0043); read-only `GET` uses plain
`require_admin`. All mutating routes carry `@audit_action` per ADR-0021.

**Pricing decisions (market research, report 03):** Free $0/200 calls, Pro $19/seat, Team $39/seat
(3-seat min enforced at app layer), Enterprise from $149 (= `unlimited` slug + per-key overrides +
manual invoice). Break-even ~120 paying users ≈ $46K ARR.

**FLAG (confirm before production):** Exact Polar webhook header names, `whsec_` encoding,
event-type spellings, and payload field paths for buyer email + product id must be verified against
live Polar docs / a captured sample before merge. Constants are centralized in `src/billing/polar.py`
to make the confirmation + correction a single-file change.

**Ops note on deploy:** Apply migration `m13_014_billing_p1.sql`; re-run
`ops/rls_create_osm_reader.sql` if not relying on the in-migration GRANT block; set
`POLAR_WEBHOOK_SECRET` in `webui.env` / systemd `Environment=` BEFORE the webhook route goes live;
set `billing.polar_product_map` (JSON `{polar_product_id: plan_slug}`) in Admin Settings post-deploy.

**Human follow-up (non-engineering, open questions):** Polar.sh KYB onboarding for the operating
entity (entity documents, payout currency); register the webhook endpoint URL + product→plan map in
the Polar dashboard; accounting treatment of the NET MoR payout and foreign-exchange compliance.

---

## Amendment — M10B P1 completion (2026-05-30)

**Branch:** `feat/m10b-p1-billing` — W1-W6 completion waves (schema hardening, extensibility
refactor, self-service cancel, admin configurability, legal/consent, billing dashboard).

### A — Schema additions (gộp vào m13_014_billing_p1.sql, sections 6-8)

All W1 schema additions are gộp vào `m13_014_billing_p1.sql` (single migration for the entire
billing schema — sections 1-5 are the original P1 DDL; sections 6-8 extend it). The previously
separate m13_015/m13_016/m13_017 draft files no longer exist as separate files for this PR
(note: m13_017 file number later reused by PR #224 — see Amendment 2026-05-31).

**Section 6 — cancel_at_period_end + per-currency prices** (formerly m13_015):

- `subscriptions.cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE` — UI/state signal for
  voluntary cancel-at-period-end. The column is a flag only; the actual period-end downgrade
  is driven by the Polar `subscription.canceled` webhook calling `revoke_entitlement(voluntary=False)`.
- `plans.prices JSONB NOT NULL DEFAULT '{}'` — per-currency price map alongside the existing
  scalar `price_cents/currency` (default-display currency). Example: `{"USD": 1900}`.
  Seed guard: dual sentinel `WHERE price_cents = 0 AND prices = '{}'::jsonb` so re-runs never
  clobber admin edits. Seeded values (USD-only; **multi-currency display deferred to P2**):
  Pro `{"USD":1900}`, Team `{"USD":3900}`, Free/Unlimited `{"USD":0}`. The `prices` JSONB
  column is designed to hold future currency keys (e.g. VND) — add them when a regional pricing
  tier is decided. *Note for future VND support:* VND is zero-decimal; values should be whole
  Vietnamese Dong, NOT cents — never multiply by 100.

**Section 7 — terms_accepted_at** (formerly m13_016): `webui_users.terms_accepted_at TIMESTAMPTZ`
— auditable proof-of-consent for ToS + Privacy Policy. `NULL` = legacy user (grandfathered).
Non-NULL = timestamp the user checked the consent checkbox at signup (password) or completed OAuth
account-creation. Required by PDPL 91/2025 + card-network consent requirements before taking payments.

**Section 8 — drop waitlist CHECK** (formerly m13_017; note: m13_017 file number later reused by PR #224 — see Amendment 2026-05-31): drops the hard-coded
`CHECK (plan IS NULL OR plan IN ('free','pro','team'))` from `waitlist_emails` (added in m13_008).
The constraint encoded the allowed-plan list at the schema level; the application layer
(`_public_plan_slugs` query in `waitlist.py`) is now the sole gate, DB-derived from
`plans WHERE is_public=TRUE AND is_archived=FALSE`. No replacement constraint.

### B — Vendor-generic webhook pipeline + slug helper (C1, C4 ADR-0039)

**`src/billing/_db.py`** — `slug_to_plan_id(slug, conn)`: vendor-neutral, fully parameterised
slug→`plans.id` resolver. Both the Polar adapter and any future Paddle/ERP adapter call this;
no caller needs to import a vendor-named module just to resolve a plan.

**`src/billing/webhook_pipeline.py`** — `WebhookAdapter` frozen dataclass + `run_webhook_pipeline`
function. The 13-step webhook-processing pipeline (rate-limit, fail-closed secret check, signature
verify, ledger record, dedup, event-action map, plan resolution, grant/update/revoke dispatch,
mark-processed) is vendor-agnostic. Only four concerns differ per vendor: `verify_fn`,
`parse_event_fn`, `event_action_fn`, `resolve_plan_fn`, plus header names and field extractors —
all captured in `WebhookAdapter`. A second vendor (Paddle/ERP) is ~25 lines of adapter glue +
a route that builds an adapter and calls `run_webhook_pipeline`. The Polar handler is the first
adapter.

**`src/billing/__init__.py`** re-exports only the vendor-neutral surface
(`EntitlementGrant`, `grant/revoke/update_entitlement`, `claim_subscription_for_user`,
`provision_or_upgrade`). Vendor adapters are imported namespaced to avoid top-level collision.

**`subscriptions.product_id` remains DEFERRED to P2** (D2 posture: extract-gradually; sole
product today; column added when a second product arrives).

**Polar adapter behavioral decisions:**

- **`recurring_interval` dual-path extraction:** `_extract_interval(data)` reads
  `data["recurring_interval"]` first; falls back to `data["price"]["recurring_interval"]` for
  older Polar payload shapes that nest the field in a `price` sub-object. The raw token is passed
  to `polar.normalize_billing_interval`: `month→monthly`, `year→annual`, `day`/`week`→`monthly`
  (safe fallback — no day/week product sold today; owner-flag to add enum values if that changes),
  `null`→`one_time`.
- **Status normalisation:** `map_subscription_status` maps Polar `unpaid`→`expired` (definitive
  payment failure, not a retry), `ended`/`incomplete_expired`→`expired`. `trialing` and clearly
  active tokens map to their OSM equivalents.
- **Transient-vs-permanent error routing (money-safety invariant):** errors from
  `grant/revoke/update_entitlement` are classified at the pipeline boundary.
  *Permanent* (`IntegrityError`, `CheckViolation`, `ValueError` — bad data that will never succeed
  on retry) → mark event processed + return **200** so Polar stops hammering a poison event.
  *Transient* (`OperationalError`, DB pool timeout, any other exception) → do NOT mark processed +
  return **5xx** so Polar retries later and the grant is not lost. All errors are written to
  `billing_webhook_events.processing_error` for ops investigation.
- **Self-heal / reprocess guard:** a webhook event that was NOT previously marked processed (e.g.
  crash mid-flight) is re-dispatched on the next Polar delivery attempt; already-processed events
  are deduped immediately (return 200 without re-dispatch). The `last_event_at` monotonic guard
  on `subscriptions` prevents out-of-order events from reverting a newer state.
- **Immediate-cancel path via PATCH:** the Polar cancel API call uses
  `PATCH .../v1/subscriptions/{id}` with `{"cancel_at_period_end": true}` (FLAG: confirm against
  live Polar docs before go-live; constant in `polar_api.py`).

### C — Self-service cancel (no refund, cancel-at-period-end)

**`src/billing/polar_api.py`** — outbound Polar REST client (`httpx.AsyncClient`). `POLAR_API_KEY`
sourced from `src/web_ui/config.py`; `billing.polar_api_base` is admin-configurable (default
`https://api.polar.sh`). Fail-closed: absent `POLAR_API_KEY` → `PolarApiNotConfigured` (caller
→ 503). Transport / 4xx / 5xx → `PolarApiError` (caller → 502). Cancel path:
`PATCH {base}/v1/subscriptions/{id}` with body `{"cancel_at_period_end": true}`.
**FLAG: confirm endpoint + method + payload against live Polar docs before go-live.**
Constants centralized in `_CANCEL_PATH_TEMPLATE` / `_CANCEL_AT_PERIOD_END_METHOD` /
`_CANCEL_AT_PERIOD_END_PAYLOAD` for single-location correction.

**`activation.revoke_entitlement(voluntary=)` contract extended:**

- `voluntary=True` (user-initiated in-app cancel): calls `subs.schedule_cancellation(sub_id)` →
  sets `cancel_at_period_end=TRUE` + `cancelled_at=now()`, leaves `status='active'`, does NOT
  downgrade the key. Access continues to `current_period_end`.
- `voluntary=False` (default — payment failure / refund / period-end webhook): immediate
  downgrade to `free` + flush plan cache (unchanged from original P1 behaviour).

**`GET /api/account/subscription`** — returns the user's active subscriptions with plan metadata
(`plan_slug`, `plan_name`, `cancel_at_period_end`, renewal date) plus `manage_url` pointing to the
Polar customer portal (`billing.polar_portal_url` setting, default `https://polar.sh/`).

**`POST /api/account/subscription/cancel`** (`@audit_action("account.subscription.cancel")`):

1. Auth-gate: requires valid session.
2. Find the user's active, non-cancelled subscription.
3. `await polar_api.cancel_subscription(external_ref, at_period_end=True)`.
4. Only on Polar success: `revoke_entitlement(external_ref, voluntary=True)` → sets local flag.
5. Returns `cancel_at_period_end=True` + renewal date.
6. On `PolarApiNotConfigured`: 503 + `portal_url` so user can cancel directly at the vendor.
7. On `PolarApiError`: 502 + `polar_status` — local flag is NOT set (Polar remains authoritative).

### D — Admin configurability

**`PATCH /api/admin/plans/{slug}` (`PlanPatch`)** now accepts `price_cents`, `currency`,
`billing_interval`, `trial_days`, `prices` (per-currency map), and `is_archived` in addition to
the existing quota/rpm/seat_limit/display_name/is_public/metadata fields.

**8 new `billing.*` settings** added to `src/settings_registry.py` (total billing settings: 11;
total `SETTINGS_CATALOGUE` entries: 27 at time of P1 billing; 28 after PR #223 adds `support.helpdesk_url`):
`billing.free_plan_slug` (default `"free"`),
`billing.unlimited_sentinel_slug` (default `"unlimited"`),
`billing.team_plan_slug` (default `"team"`),
`billing.team_min_seats` (default `3`, **enforced** at `grant_entitlement` — `ValueError` → HTTP 422
on webhook, surface to ops via ledger `processing_error`),
`billing.polar_portal_url` (default `"https://polar.sh/"`),
`billing.polar_api_base` (default `"https://api.polar.sh"`),
`billing.paid_checkout_enabled` (default `False` — gates paid CTA on `/pricing` and legal pages),
`billing.polar_checkout_url_map` (default `{}`).

**Waitlist allow-list is now DB-derived** (`_public_plan_slugs` queries `plans WHERE is_public=TRUE
AND is_archived=FALSE`); the hard-coded frozenset is removed; m13_014 §8 (formerly m13_017; note: m13_017 file number later reused by PR #224 — see Amendment 2026-05-31) drops
the schema-level `CHECK` that encoded the same list (see §A).

**`GET /api/plans`** now exposes the `prices` JSONB field alongside existing pricing columns,
enabling the data-driven USD pricing page without a separate query. Multi-currency display
(additional keys in `prices` JSONB) is deferred to P2.

### E — Legal pages + consent gate

Three Astro pages initially shipped with **DRAFT badge**; **DRAFT removed per CEO sign-off
2026-06-01 (PR #224); paid_checkout_enabled flip remains runtime ops** (external counsel review
recommended post-launch):

- **`/terms`** — Terms of Service, including the no-refund + cancel-at-period-end policy
  (owner decision #1). DRAFT notice removed; real Viindoo entity + effective date 2026-06-01.
- **`/refund`** — Refund Policy page; B2B all-sales-final + EEA/UK consumer 14-day withdrawal
  pro-rata (CRD Art. 9/14(3)/16(a)).
- **`/privacy`** — Privacy Policy, citing PDPL 91/2025 Art. 17(a) consent basis.

Footer links to all three pages added to the shared layout.

**Required signup consent checkbox** (`data-testid="signup-consent-checkbox"`) — disables the
submit button until checked (client-side D4 guard). On submit, `consent: true` is sent to the
backend. Backend records `terms_accepted_at = NOW()` in `webui_users` for both the
password-signup path (`routes/signup.py`) and the OAuth account-creation path (`routes/oauth.py`).

**`paid_checkout_enabled` flag (D4):** the paid-checkout CTA on `/pricing` and the checkout links
on legal pages are gated by `billing.paid_checkout_enabled` (default `False`). Flip to `True`
only after legal sign-off + Polar KYB onboarding complete.

### F — Billing dashboard + real-time usage refresh

**`/account/billing`** Astro page (auth-gated by middleware) renders a `BillingDashboard` React
island. The island calls `GET /api/account/subscription` to display:

- Current plan name, status, seats.
- Renewal / period-end date.
- `cancel_at_period_end` state (shows "Cancels on [date]" when scheduled).
- "Manage subscription" link → Polar portal.
- "Cancel subscription" button → `POST /api/account/subscription/cancel` (voluntary, at-period-end).

**`/pricing`** page is now `prerender=false` (DB-driven). Fetches `GET /api/plans` at SSR time to
render tier cards with live prices (USD; multi-currency display deferred to P2). Checkout CTA is
gated by `billing.paid_checkout_enabled`. Usage counter auto-refreshes every 60s on the usage dashboard.

### G — Owner decisions recorded

1. **No refund + cancel-at-period-end:** in-app cancel schedules `cancel_at_period_end`; access
   runs to `current_period_end`; no refund is issued. The `subscription.canceled` Polar webhook
   at actual period end drives the downgrade.
2. **In-app cancel calls Polar API** (fail-closed, `POLAR_API_KEY`): the local flag is only set
   after Polar confirms. If Polar is unavailable, user sees 503 + portal URL.
3. **Legal DRAFT removed per CEO sign-off 2026-06-01 (PR #224):** external counsel review
   recommended post-launch; paid checkout CTA is gated by `billing.paid_checkout_enabled`
   (default `False`); flip remains runtime ops (requires Polar KYB completion).
4. **`team_min_seats` default 3, enforced** at `grant_entitlement` (not just advisory).

### H — Pending / human follow-up (non-engineering)

- **Confirm Polar webhook + REST API endpoints/fields against live Polar docs:** header names
  (`webhook-id` / `webhook-timestamp` / `webhook-signature`), `whsec_` prefix encoding, event-type
  spellings (`subscription.canceled` US), payload field paths (`data.id`, `data.product_id`,
  `data.customer.email`). Constants centralized in `src/billing/polar.py` + `polar_api.py`.
- **Legal pages CEO-signed (DRAFT removed per PR #224, 2026-06-01).** External counsel review
  recommended post-launch. Enable live paid sales: admin set
  `billing.paid_checkout_enabled = True` in Admin Settings after Polar KYB + counsel review.
- **KYB onboarding** (Polar + accounting treatment of NET MoR payout + foreign-exchange compliance).
- **Register webhook endpoint URL + product→plan map** in the Polar dashboard + set
  `billing.polar_product_map` in Admin Settings post-deploy.

### I — Justified P2 deferrals (not debt)

- `subscriptions.product_id` — extract-gradually (D2); sole product today.
- Live in-window RPM cross-process — bounded by 60s TTL; Redis/PG LISTEN deferred to M14+.
- Fernet-encrypted webhook-secret rotation — deferred; env-var delivery sufficient for Phase 1.
- Per-key usage breakdown — deferred to P3 support/SLA tooling.

**Tool count stays 24.** All W1-W6 changes are schema / web-UI / webhook / Astro layer only.
No new MCP tools added.

**Migration required on deploy:** `m13_014` is the single migration covering all billing schema
(sections 1-8; idempotent, safe to re-run). The previously separate m13_015/m13_016/m13_017 draft files
are gộp vào m13_014 and no longer exist as separate files for this PR (note: m13_017 file number later reused by PR #224 — see Amendment 2026-05-31). Set `POLAR_API_KEY` in `webui.env` / systemd
`Environment=` for the self-service cancel route; set `billing.polar_api_base` if using a
non-default Polar base URL.

---

### Amendment 2026-05-31 (PR #223 — feat/site-pricing-ux)

**Migration number reuse:** The file numbers `m13_015` and `m13_016` were freed when their
draft contents were merged into `m13_014`. PR #223 reuses these numbers for new migrations:
- `migrations/m13_015_pricing_model.sql` — adds `plans.pricing_model TEXT CHECK IN ('flat','per_seat')`,
  seeds `pro` + `team` as `per_seat`.
- `migrations/m13_016_plan_min_seats.sql` — adds `plans.min_seats INTEGER` (display SSOT for
  per-seat minimum copy on the pricing page).

**Per-seat pricing schema decision (two-SSOT pattern):**
- `plans.min_seats` column = **display SSOT** (pricing page copy, admin UI).
- `billing.team_min_seats` setting = **enforcement SSOT** at `grant_entitlement` (ADR-0042).
- The two values are intentionally separate; keep them in sync manually (default both = 3).

**Settings catalogue count:** `support.helpdesk_url` added as the 28th entry. Total = 28
(see `src/settings_registry.py` docstring — SSOT, not this ADR).

**Billing provision advisory lock:** `src/billing/provisioning.py` wraps `provision_or_upgrade`
in a session-level Postgres advisory lock `pg_advisory_lock(ns, subscription_id)` to prevent
the scan-B double-provision race. This is a money-safety mechanism distinct from indexer locks
(ADR-0006) and git locks (ADR-0035).

---

## Amendment 2026-05-31 (PR #224 — feat/launch-prep)

**Branch:** `feat/launch-prep`. Install MCP-first, brand SSOT, SEO/AI-discovery, English-only
legal, legal pages DRAFT removal, CRD-compliant checkout consent.

### m13_017 file number reuse

The `m13_017` file number was freed when the waitlist CHECK-drop content was merged into
`m13_014` §8. **PR #224 reuses this number** for a new migration:

- `migrations/m13_017_withdrawal_consent.sql` — adds `subscriptions.buyer_type TEXT` (values
  `'business'` / `'consumer'`) and `subscriptions.withdrawal_waiver_accepted_at TIMESTAMPTZ`
  for CRD-compliant checkout consent capture.

**Full deploy order (PR #224 and later):** `m13_014` → `m13_015` → `m13_016` → **`m13_017`**.

### DRAFT badge removal (CEO sign-off 2026-06-01)

`/terms`, `/privacy`, `/refund` DRAFT badges removed. Real Viindoo Technology Joint Stock
Company details (business reg-no 0201994665, registered address, hotline), effective date
2026-06-01, and `support@`/`sales@`/`privacy@`/`legal@viindoo.com` are now in the
`site/src/lib/contact.ts` SSOT. Public-page emails render via `ObfuscatedEmail.astro`
(JS-assembled; no plaintext address in static HTML — anti-harvest).

External counsel review is recommended post-launch (no blocking dependency on code changes;
the no-refund-absolute posture for B2C was replaced by the CRD-compliant pro-rata mechanism
below). Enabling live paid sales is a runtime admin step: set
`billing.paid_checkout_enabled = True` + configure `billing.polar_checkout_url_map` in Admin
Settings after Polar KYB is complete.

### CRD checkout consent mechanism

EU Consumer Rights Directive compliance layer, wired at the `/account/billing` pre-Polar-redirect
step (since Polar checkout is URL-map based):

- **Buyer-type capture:** `business` vs `consumer` radio selection before Polar redirect.
  Business path skips the waiver entirely.
- **Withdrawal-waiver checkbox (consumer path):** non-pre-ticked (CRD Art. 22). Consumer
  without waiver is blocked from proceeding to Polar checkout.
- **Persisted** via `m13_017` (`subscriptions.buyer_type` + `withdrawal_waiver_accepted_at`).
- **Durable-medium confirmation email** (`src/web_ui/email.py`): sent after consent capture,
  satisfying CRD Art. 7(3)/8(8) "durable medium" obligation.
- **Refund policy split** (already reflected in `/refund` page): B2B = all-sales-final;
  EEA/UK consumer = 14-day withdrawal right + **pro-rata** mid-period refund per CRD
  Art. 9/14(3)/16(a) — NOT absolute no-refund for consumers.
- **New endpoints:** `POST /api/account/checkout-consent` (record buyer_type + waiver),
  `GET /api/account/checkout-config` (returns current consent state for the billing island).
- **`_billing-island.tsx` consent modal** — renders buyer-type selector + conditional waiver
  checkbox before emitting the Polar checkout redirect.
- **`list_all` admin endpoint** now exposes `buyer_type` + `withdrawal_waiver_accepted_at`
  columns on subscription rows (admin visibility, no new MCP tool).
- **10 new postgres integration tests** covering the consent flow.

**Tool count stays 24.** All PR #224 changes are web/Astro/billing layer only.
