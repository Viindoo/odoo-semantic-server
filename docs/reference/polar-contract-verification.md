# Polar Contract Verification

**Scope:** `src/billing/polar.py` + `src/billing/polar_api.py` (the Polar-specific
adapter + outbound REST client).
**Method:** Verification of every Polar-specific constant and field path against
the live Polar webhook/API documentation and the Standard Webhooks specification
that Polar implements.
**Purpose:** Confirm the webhook signature contract, event spellings, payload
field paths, and the cancel endpoint before enabling paid checkout
(`billing.paid_checkout_enabled`).

This document records the technical findings only. Operational values (secrets,
product IDs, dashboard configuration) are intentionally excluded.

---

## Findings Table

| Item | Code assumption | Finding | Verdict |
|------|-----------------|---------|---------|
| **Webhook headers** | `webhook-id`, `webhook-timestamp`, `webhook-signature` (Standard Webhooks canonical names) | Polar's webhook delivery uses these exact lowercase header names. No `x-polar-*` variants. | **MATCH** |
| **Secret format + HMAC** | `whsec_` prefix + base64 body; `HMAC-SHA256(secret_bytes, "{msg_id}.{timestamp}.{body}")`; signature token `"v1,<base64digest>"`, space-delimited in the header | Matches the Standard Webhooks spec that Polar implements: `whsec_` symmetric-secret prefix, HMAC over `id + "." + ts + "." + body`, `"v1,<b64>"` token. | **MATCH** |
| `subscription.created` | mapped → `"grant"` | Spelling confirmed in the Polar event catalogue. | **MATCH** |
| `subscription.active` | mapped → `"grant"` | Spelling confirmed. | **MATCH** |
| `subscription.updated` | mapped → `"update"` | Spelling confirmed. | **MATCH** |
| `subscription.canceled` (US, one `l`) | mapped → `"revoke"` | US spelling `canceled` confirmed in Polar's own server schema (`WebhookSubscriptionCanceledPayload`). British `cancelled` does NOT appear. | **MATCH** |
| `subscription.revoked` | mapped → `"revoke"` | Spelling confirmed. | **MATCH** |
| `order.paid` | mapped → `"grant"` | Confirmed: fired when the order is fully processed and payment received (distinct from `order.created`, which may be pending). | **MATCH** |
| `order.refunded` | mapped → `"revoke"` | Spelling confirmed. | **MATCH** |
| `subscription.uncanceled` | **ADDED** → `"update"` | Event exists (fired when a cancel-at-period-end schedule is reversed). Originally unmapped → the local subscription stayed `cancelled` while Polar had un-cancelled it. Now routed through `"update"` so the status snapshot is re-read AND the local `cancel_at_period_end` flag is cleared. | **ADDED** |
| `subscription.past_due` | **ADDED** → `"update"` | Event exists (payment failed, dunning retry window). Originally unmapped → the local subscription stayed `active` while actually in dunning (over-serve on payment failure). Now mapped to a terminal status so the key is downgraded. | **ADDED** |
| `payload["type"]` | top-level `type` string | All Polar webhook payloads carry a top-level `type`. | **MATCH** |
| `payload["data"]["id"]` as `external_ref` | subscription/order object id | `data.id` holds the Polar subscription or order id. | **MATCH** |
| `payload["data"]["product_id"]` for plan resolution | flat UUID at `data.product_id` | **Subscription events:** flat `product_id` confirmed (the `Subscription` schema carries `product_id` as a flat field). **Order events (`order.paid`):** the `Order` schema serializes product as a NESTED object (`data.product.id`). Whether a flat `data.product_id` is ALSO present alongside the nested object on order webhooks is unconfirmable from docs alone. | **PARTIAL** for `order.paid` (tolerant-shape fix below) |
| `payload["data"]["customer"]["email"]` | nested customer object with `email` | The full `Subscription` schema includes a nested `customer` object; `email` is present but nullable in recent SDK releases. Minimal third-party payload examples show only flat `customer_id`. Which is sent over the wire is unconfirmable from docs alone. | **UNCONFIRMABLE** — needs a real test event |
| **cancel-at-period-end** | `PATCH /v1/subscriptions/{id}` body `{"cancel_at_period_end": true}` | Confirmed: this is Polar's Update Subscription API; method and field name are correct. | **MATCH** |
| **immediate cancel (revoke)** | `PATCH /v1/subscriptions/{id}` body `{"revoke": true}` | **MISMATCH.** Polar's dedicated Revoke Subscription endpoint is `DELETE /v1/subscriptions/{id}` with NO body. A `{"revoke": true}` variant exists as an anyOf type on the PATCH Update schema, but the canonical revoke is DELETE. | **MISMATCH** (corrected below) |
| **Cancel token type** | `Authorization: Bearer <token>` (Organization Access Token, `subscriptions:write` scope) | Polar management APIs use Organization Access Tokens via `Authorization: Bearer`. | **MATCH** |
| **API base URL** | production default `https://api.polar.sh` | Correct for production. Polar provides a separate sandbox base for staging; it is not auto-detected, so staging must point the configured base URL at the sandbox host explicitly. | **MATCH** (for prod) |

---

## Corrections Applied

### Immediate cancel is DELETE, not PATCH

The dedicated immediate-revoke endpoint is `DELETE /v1/subscriptions/{id}` with no
request body — not `PATCH {"revoke": true}`. The outbound client constants were
corrected so the immediate-cancel method is `DELETE` and the payload is `None`
(the HTTP client must omit the body for DELETE). The cancel-at-period-end path
(`PATCH` + `{"cancel_at_period_end": true}`) is unchanged — it was already correct.

### Tolerant product-id extraction for `order.paid`

Subscription events expose a flat `data.product_id`; order events expose a nested
`data.product.id`. To grant entitlements on both shapes without depending on an
unconfirmed flat field, plan resolution falls back from `data.product_id` to
`data.product.id` (tolerant-shape read), rather than failing when only the nested
object is present.

### Tolerant customer-email extraction

Because the over-the-wire presence of the nested `customer.email` versus a flat
`customer_id` is unconfirmed, the email extractor reads tolerantly across the
plausible shapes and never crashes when the email is absent — an absent email
leaves the subscription active-but-unclaimed (claimed later on verified login),
which is the correct degraded behaviour.

### Two added events

`subscription.uncanceled` and `subscription.past_due` were added to the event map
(both routed through `"update"`):

- `subscription.uncanceled` → re-reads status (active) and clears the locally
  scheduled `cancel_at_period_end`. The reactivation must also re-point the live
  key back UP to the paid plan even when the snapshot `plan_id` never changed (a
  prior involuntary cancel can have downgraded the key to free while the snapshot
  `plan_id` stayed paid) — see the activation update path.
- `subscription.past_due` → mapped to a terminal status so the key is downgraded
  (the higher-risk gap: previously the subscription stayed `active` locally during
  dunning, over-serving on payment failure).

---

## Confirm With a Real Test Event

The following cannot be authoritatively confirmed from documentation and require a
real Polar test event (Polar dashboard → Webhook endpoint → send test event):

1. **`data.customer.email` in subscription webhooks** — is the nested `customer`
   object (with `email`) delivered, or only a flat `customer_id`?
2. **`data.product_id` in `order.paid` webhooks** — is a flat `product_id`
   present alongside the nested `product` object, or only `product.id`?
3. **Immediate revoke path** — does Polar accept `PATCH {"revoke": true}` (Update
   anyOf), or ONLY `DELETE /v1/subscriptions/{id}`? Both appear in the API
   reference; which is authoritative needs a live call.
4. **`whsec_` prefix display** — does the dashboard show the generated webhook
   secret with the `whsec_` prefix, or as a bare base64 string?
