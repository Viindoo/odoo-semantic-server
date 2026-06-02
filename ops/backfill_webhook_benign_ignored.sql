-- ops/backfill_webhook_benign_ignored.sql — clear benign-ignore noise from the
-- billing_webhook_events ledger. ADR-0039 (webhook-pipeline outcome distinction).
-- Idempotent + safe to re-run.
--
-- Before the watched_event_prefixes fix, the pipeline recorded EVERY unmapped
-- event_type (e.g. Polar's checkout.created/updated/expired, fired on every
-- checkout attempt) into processing_error as "unmapped event_type=...". Those
-- benign-ignore rows read as errors and bury the genuine "forgotten mapping"
-- signal (an unmapped subscription.*/order.* subtype the pipeline now reserves
-- processing_error for).
--
-- This one-off cleanup NULLs processing_error for rows that are now classified
-- benign-ignore: an "unmapped event_type=..." note whose event_type is OUTSIDE
-- the watched entitlement families (subscription.* / order.*). Rows for unmapped
-- subscription.*/order.* subtypes (true forgotten-mapping signals) are left
-- untouched. Re-running is a no-op once the matching rows are already NULL.
--
-- Run (prod is the local docker postgres):
--   docker exec -i odoo-semantic-mcp-postgres-1 \
--     psql -U odoo_semantic -d odoo_semantic -v ON_ERROR_STOP=1 \
--     -f - < ops/backfill_webhook_benign_ignored.sql

UPDATE billing_webhook_events
SET processing_error = NULL
WHERE processing_error LIKE 'unmapped event_type=%'
  AND event_type IS NOT NULL
  AND event_type NOT LIKE 'subscription.%'
  AND event_type NOT LIKE 'order.%';
