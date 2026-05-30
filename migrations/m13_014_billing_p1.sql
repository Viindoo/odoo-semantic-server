-- migrations/m13_014_billing_p1.sql
-- M10B P1 "Billing" — Entitlement Activation API + Polar.sh adapter DDL +
-- cancel-at-period-end flag + per-currency prices JSONB + signup consent +
-- waitlist CHECK drop (gộp từ m13_015, m13_016, m13_017).
-- Diverges from ADR-0039 D3 per product-owner rules: integer FK plan_id (no text slug),
-- NO per-row limits (limits live in plans, resolved via plan_id at runtime), + webhook ledger.
-- Idempotent — safe to re-run.

-- ===== 1. plans commercial pricing columns =====
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS price_cents      BIGINT  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS currency         TEXT    NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS billing_interval TEXT    NOT NULL DEFAULT 'free',
    ADD COLUMN IF NOT EXISTS trial_days       INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS is_archived      BOOLEAN NOT NULL DEFAULT FALSE;

-- #3 BIGINT: upgrade price_cents INTEGER→BIGINT (VND whole-units can exceed INT4 2.1B).
-- Idempotent: ALTER TYPE on an already-BIGINT column is a no-op.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'plans'
           AND column_name = 'price_cents'
           AND data_type = 'integer'
    ) THEN
        ALTER TABLE plans ALTER COLUMN price_cents TYPE BIGINT;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'plans_billing_interval_check'
           AND conrelid = 'plans'::regclass
    ) THEN
        ALTER TABLE plans ADD CONSTRAINT plans_billing_interval_check
            CHECK (billing_interval IN ('free', 'monthly', 'annual', 'one_time'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'plans_price_cents_nonneg'
           AND conrelid = 'plans'::regclass
    ) THEN
        ALTER TABLE plans ADD CONSTRAINT plans_price_cents_nonneg
            CHECK (price_cents >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'plans_trial_days_nonneg'
           AND conrelid = 'plans'::regclass
    ) THEN
        ALTER TABLE plans ADD CONSTRAINT plans_trial_days_nonneg
            CHECK (trial_days >= 0);
    END IF;
    -- #9 currency CHECK on plans.currency
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'plans_currency_iso4217'
           AND conrelid = 'plans'::regclass
    ) THEN
        ALTER TABLE plans ADD CONSTRAINT plans_currency_iso4217
            CHECK (currency ~ '^[A-Z]{3}$');
    END IF;
END $$;

-- ===== 2. FREE/UNLIMITED SEED — zero-price plans =====
-- #12 seed desync fix: the PAID-plan seed (pro/team) is intentionally NOT here.
-- It is deferred to section 6.3 as a SINGLE combined UPDATE that guards BOTH
-- price_cents AND prices in one sentinel, so an admin who edits either field
-- (e.g. price_cents=0 for a promo) can never have the re-run silently revert it.
-- Seeding price_cents here with only a price_cents=0 guard caused exactly that
-- desync: a re-run reverted price_cents→1900 even when prices was already custom.
--
-- The two zero-price plans are safe to seed here: both are $0 by design, so there
-- is no second representation (prices) to drift out of sync with price_cents.
--
-- Free quota bump 100 -> 200 (report 03 §6).
UPDATE plans SET quota_calls_per_month = 200
    WHERE slug = 'free' AND quota_calls_per_month = 100;
-- Free/unlimited: currency + interval.
-- Guard: AND currency = 'USD' — sentinel against admin who changed currency (e.g. 'EUR').
-- billing_interval defaults to 'free' for the column so it is also included as a sentinel.
-- On first-run both columns are at their DEFAULT values (USD / free) → seed applies.
-- On re-run after admin edit (e.g. free.currency='EUR') → guard fails → no revert.
-- This mirrors the dual-sentinel principle used for the paid-plan seed in §6.3.
UPDATE plans SET currency = 'USD', billing_interval = 'free'
    WHERE slug IN ('free', 'unlimited')
      AND price_cents = 0
      AND currency = 'USD'
      AND billing_interval = 'free';
-- Enterprise = unlimited slug + per-key overrides + manual invoice (no public price row).

-- ===== 3. subscriptions (commercial-only, integer FKs, NO limit cols) =====
CREATE TABLE IF NOT EXISTS subscriptions (
    id                   SERIAL PRIMARY KEY,
    plan_id              INTEGER NOT NULL REFERENCES plans(id),
    -- RESTRICT (default): can't drop a plan with active subscriptions.
    claimed_user_id      INTEGER REFERENCES webui_users(id) ON DELETE SET NULL,
    api_key_id           INTEGER REFERENCES api_keys(id)    ON DELETE SET NULL,
    tenant_id            INTEGER REFERENCES tenants(id)     ON DELETE SET NULL,
    -- buyer email snapshot for claim-on-login (NULL once claimed)
    buyer_email          TEXT,
    status               TEXT NOT NULL DEFAULT 'pending'
                             CONSTRAINT subscriptions_status_check
                             CHECK (status IN (
                                 'pending', 'active', 'past_due', 'cancelled',
                                 'expired', 'trialing', 'refunded'
                             )),
    seats                INTEGER NOT NULL DEFAULT 1
                             CONSTRAINT subscriptions_seats_positive
                             CHECK (seats > 0),
    source               TEXT NOT NULL DEFAULT 'polar'
                             CONSTRAINT subscriptions_source_check
                             CHECK (source IN ('polar', 'erp', 'admin', 'promo')),
    external_ref         TEXT,
    -- money snapshot (informational; Polar is accounting SoR)
    -- #3 BIGINT: VND whole-units can exceed INT4 2.1B max.
    amount_cents         BIGINT,
    currency             TEXT,
    billing_interval     TEXT
                             CONSTRAINT subscriptions_billing_interval_check
                             CHECK (billing_interval IS NULL OR billing_interval IN (
                                 'free', 'monthly', 'annual', 'one_time'
                             )),
    -- timeline
    current_period_start TIMESTAMPTZ,
    current_period_end   TIMESTAMPTZ,
    trial_ends_at        TIMESTAMPTZ,
    cancelled_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Active/trialing subscriptions must have at least one claim target
    -- (claimed_user_id, api_key_id, tenant_id, OR buyer_email for unclaimed-paid transient).
    CONSTRAINT subscriptions_no_orphan_active
        CHECK (
            status NOT IN ('active', 'trialing')
            OR claimed_user_id IS NOT NULL
            OR api_key_id IS NOT NULL
            OR tenant_id IS NOT NULL
            OR buyer_email IS NOT NULL
        )
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id
    ON subscriptions(claimed_user_id) WHERE claimed_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_subscriptions_api_key_id
    ON subscriptions(api_key_id) WHERE api_key_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant_id
    ON subscriptions(tenant_id) WHERE tenant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_subscriptions_plan_status
    ON subscriptions(plan_id, status);
-- buyer_email partial index for claim-on-login lookup (unclaimed-paid rows only)
CREATE INDEX IF NOT EXISTS idx_subscriptions_buyer_email
    ON subscriptions(buyer_email)
    WHERE buyer_email IS NOT NULL AND claimed_user_id IS NULL;

-- Post-CREATE ALTER TABLE corrections (applied idempotently via DO blocks).
-- These are safe to run against both a freshly-created and a pre-existing table.

-- #3 BIGINT: upgrade amount_cents INTEGER→BIGINT (same rationale as plans.price_cents).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'subscriptions'
           AND column_name = 'amount_cents'
           AND data_type = 'integer'
    ) THEN
        ALTER TABLE subscriptions ALTER COLUMN amount_cents TYPE BIGINT;
    END IF;
END $$;

-- #8 UNIQUE(source, external_ref): replace the global external_ref UNIQUE with a
-- composite key so the same Polar order ID cannot bleed across vendors, while
-- still allowing NULL external_ref for admin/promo grants.
-- Step 1: drop the old global UNIQUE constraint if it still exists (any name variant).
DO $$
DECLARE
    _conname TEXT;
BEGIN
    SELECT conname INTO _conname
      FROM pg_constraint
     WHERE conrelid = 'subscriptions'::regclass
       AND contype  = 'u'
       AND conkey   = ARRAY(
               SELECT a.attnum FROM pg_attribute a
                WHERE a.attrelid = 'subscriptions'::regclass
                  AND a.attname  = 'external_ref'
               ORDER BY a.attnum
           );
    IF _conname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE subscriptions DROP CONSTRAINT %I', _conname);
    END IF;
END $$;
-- Step 2: add composite UNIQUE(source, external_ref) — idempotent.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname    = 'subscriptions_source_external_ref_key'
           AND conrelid   = 'subscriptions'::regclass
    ) THEN
        ALTER TABLE subscriptions
            ADD CONSTRAINT subscriptions_source_external_ref_key
            UNIQUE (source, external_ref);
    END IF;
END $$;

-- #9 currency CHECK on subscriptions.currency
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname  = 'subscriptions_currency_iso4217'
           AND conrelid = 'subscriptions'::regclass
    ) THEN
        ALTER TABLE subscriptions
            ADD CONSTRAINT subscriptions_currency_iso4217
            CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$');
    END IF;
END $$;

-- #5 last_event_at: monotonic guard column for out-of-order webhook events.
-- WI-2/WI-4 write this column; DDL lives here as the SSOT.
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ;

-- ===== 4. billing_webhook_events (idempotency ledger) =====
CREATE TABLE IF NOT EXISTS billing_webhook_events (
    id               BIGSERIAL PRIMARY KEY,
    vendor           TEXT NOT NULL
                         CONSTRAINT billing_webhook_events_vendor_check
                         CHECK (vendor IN ('polar', 'erp', 'test')),
    event_id         TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    signature_valid  BOOLEAN NOT NULL DEFAULT FALSE,
    payload          JSONB NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at     TIMESTAMPTZ,
    processing_error TEXT,
    subscription_id  INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    CONSTRAINT billing_webhook_events_vendor_event_unique UNIQUE (vendor, event_id)
);
CREATE INDEX IF NOT EXISTS idx_bwe_vendor_received
    ON billing_webhook_events(vendor, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_bwe_unprocessed
    ON billing_webhook_events(received_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_bwe_subscription
    ON billing_webhook_events(subscription_id) WHERE subscription_id IS NOT NULL;

-- ===== 5. osm_reader GRANTs (pg_roles-guarded, in-migration per house convention) =====
-- subscriptions: SELECT for /account + /tenant portal read.
-- billing_webhook_events: SELECT for admin viewer read.
-- NO INSERT/sequence grants: Activation API + webhook handler write as DB owner.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'osm_reader') THEN
        GRANT SELECT ON TABLE subscriptions          TO osm_reader;
        GRANT SELECT ON TABLE billing_webhook_events TO osm_reader;
    END IF;
END $$;

-- ===== 6. cancel_at_period_end + per-currency prices (gộp từ m13_015) =====
-- M10B P1 Billing — cancel-at-period-end flag + per-currency prices JSONB.
-- Builds on sections 1-5 above (subscriptions + plans tables).

-- 6.1 cancel_at_period_end on subscriptions
-- Voluntary cancel scheduled: access continues until current_period_end;
-- status stays 'active'. A separate 'pending_cancellation' status was
-- considered and rejected (B1 ADR-0039): the boolean flag is the minimal
-- correct model. Polar fires subscription.canceled at actual period end →
-- existing webhook then calls revoke_entitlement(voluntary=False) →
-- immediate downgrade. The in-app flag is a UI/state signal only.
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE;

-- 6.2 prices JSONB on plans (per-currency map)
-- Additive alongside existing scalar price_cents/currency (default-display currency).
-- Current seed is USD-only: {"USD": 1900}  (multi-currency display deferred to P2).
-- FUTURE NOTE (when VND is added): VND is zero-decimal — prices['VND'] is whole VND,
--   NOT cents. price_cents semantics = USD cents (or display-currency minor unit).
--   prices["VND"] semantics = whole Vietnamese Dong.
--   NEVER multiply prices["VND"] by 100. Document in ADR-0039 C3 when implemented.
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS prices JSONB NOT NULL DEFAULT '{}'::jsonb;

-- 6.3 PAID-PLAN SEED — single combined block, DUAL-SENTINEL guarded (idempotent)
-- #12 seed desync fix: each PAID plan (pro/team) is seeded HERE in ONE UPDATE
-- that writes BOTH the scalar columns (price_cents/currency/billing_interval) AND
-- the per-currency prices JSONB, guarded by a DUAL sentinel:
--     WHERE slug = '<slug>' AND price_cents = 0 AND prices = '{}'::jsonb
-- Both sentinels must hold SIMULTANEOUSLY for the seed to (re-)apply.  The moment
-- an admin edits EITHER field — price_cents away from 0 (e.g. a promo to 0 is
-- already non-applying once prices is set) OR prices away from '{}' — the WHERE
-- no longer matches, so a migration re-run is a true no-op and NEVER reverts the
-- admin's value.  This eliminates the previous split-seed desync where section 2
-- (price_cents, guard price_cents=0) and section 6.3 (prices, guard prices='{}')
-- could revert one representation while the other stayed custom.
-- New vendor: ALTER TABLE ... DROP CONSTRAINT subscriptions_source_check;
--             ADD CONSTRAINT ... CHECK (source IN ('polar','erp','admin','promo','paddle'));

-- Pro: $19/seat/mo USD. Multi-currency display deferred; VND key removed from prices.
UPDATE plans
   SET price_cents      = 1900,
       currency         = 'USD',
       billing_interval = 'monthly',
       prices           = '{"USD": 1900}'::jsonb
 WHERE slug = 'pro'
   AND price_cents = 0
   AND prices = '{}'::jsonb;

-- Team: $39/seat/mo USD. Multi-currency display deferred; VND key removed from prices.
UPDATE plans
   SET price_cents      = 3900,
       currency         = 'USD',
       billing_interval = 'monthly',
       prices           = '{"USD": 3900}'::jsonb
 WHERE slug = 'team'
   AND price_cents = 0
   AND prices = '{}'::jsonb;

-- Free + unlimited: $0 (zero-decimal currencies also 0). Single sentinel on prices
-- is fine — these are zero-price by design so price_cents has nothing to drift to.
UPDATE plans
   SET prices = '{"USD": 0}'::jsonb
 WHERE slug IN ('free', 'unlimited')
   AND prices = '{}'::jsonb;

-- 6.4 osm_reader GRANTs
-- subscriptions and plans already had GRANT SELECT TO osm_reader via sections above
-- and m13_006 respectively.  New columns on those tables are automatically
-- covered by existing table-level SELECT grants — no new GRANT needed.
-- (PostgreSQL column-level privileges: table SELECT covers all current+future
-- columns; a new column never breaks a pre-existing SELECT grant.)
-- No-op sentinel so yoyo does not see a comment-only empty statement:
SELECT 1 WHERE FALSE;

-- ===== 7. signup consent audit trail (gộp từ m13_016) =====
-- M10B P1 Billing — signup consent audit trail.
-- Adds terms_accepted_at to webui_users for auditable proof-of-consent
-- (ToS + Privacy Policy accepted at signup). Required by PDPL 91/2025 +
-- EU/international card-network consent requirements before taking payments.
--
-- NULL = pre-consent legacy user (grandfathered, no paid features blocked).
-- Non-NULL = timestamp user checked "I agree to ToS and Privacy Policy".
-- The value is recorded at signup (password) and OAuth account-creation.
-- See Area D4 of the billing solution plan and ADR-0039.
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMPTZ;

-- ===== 8. drop waitlist_emails.plan CHECK constraint (gộp từ m13_017) =====
-- C4 (ADR-0039): remove the hard-coded CHECK constraint on waitlist_emails.plan.
--
-- The original m13_008 migration added:
--   CHECK (plan IS NULL OR plan IN ('free', 'pro', 'team'))
-- This constraint encoded the allowed-plan list at the schema level, which
-- means adding a new public plan required BOTH a code change AND a DB migration.
--
-- C4 replaces the application-level frozenset with a DB-derived query
-- (_public_plan_slugs in waitlist.py).  The DB CHECK is now redundant, and its
-- hardcoded list contradicts the goal of admin-editable plans.  The application
-- layer is the sole gate: it validates plan against plans WHERE is_public=TRUE
-- AND is_archived=FALSE before the INSERT.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS is safe to re-run.

ALTER TABLE waitlist_emails
    DROP CONSTRAINT IF EXISTS waitlist_emails_plan_check;

-- No new constraint added: application layer (_public_plan_slugs) is the gate.
-- This is intentional — a DB CHECK would have to enumerate plan slugs, defeating
-- the purpose of the DB-driven allow-list.
-- No-op sentinel so yoyo does not see a comment-only empty statement:
SELECT 1 WHERE FALSE;
