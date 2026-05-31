-- migrations/m13_017_withdrawal_consent.sql
-- CRD-compliant checkout consent (M10B P1, ADR-0039, ETHOS D2).
--
-- Context:
--   OSM paid plans = digital SERVICE delivered immediately upon checkout.
--   Under EU Consumer Rights Directive (CRD) Art.16(a), a consumer who
--   explicitly requests immediate delivery of a digital service and
--   acknowledges their right of withdrawal is extinguished thereby loses
--   the 14-day right of withdrawal.  This is the mandatory "withdrawal
--   waiver" for B2C paid checkout.
--
--   B2B traders have no CRD withdrawal right, so buyer_type='business'
--   simply records the classification; no waiver is required or collected.
--
-- Adds two columns on subscriptions:
--
--   buyer_type TEXT NULL CHECK ('business' | 'consumer')
--     Recorded at checkout pre-redirect.  NULL = pre-consent legacy row
--     (grandfathered; the waiver flow was not present when the sub was created).
--
--   withdrawal_waiver_accepted_at TIMESTAMPTZ NULL
--     Non-NULL iff the buyer is a consumer AND ticked the CRD Art.22
--     non-pre-ticked waiver checkbox before being redirected to Polar.
--     NULL for business buyers (no waiver required) and legacy rows.
--
-- Idempotent -- safe to re-run.

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS buyer_type TEXT
        CONSTRAINT subscriptions_buyer_type_check
        CHECK (buyer_type IN ('business', 'consumer'));

-- Idempotent guard for the CHECK constraint (in case the column existed
-- without the constraint on an earlier partial deploy).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname    = 'subscriptions_buyer_type_check'
           AND conrelid   = 'subscriptions'::regclass
    ) THEN
        ALTER TABLE subscriptions
            ADD CONSTRAINT subscriptions_buyer_type_check
            CHECK (buyer_type IN ('business', 'consumer'));
    END IF;
END $$;

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS withdrawal_waiver_accepted_at TIMESTAMPTZ;

-- Partial index: fast lookup of consumer-waiver rows for compliance reporting.
CREATE INDEX IF NOT EXISTS idx_subscriptions_waiver_consumer
    ON subscriptions(withdrawal_waiver_accepted_at)
    WHERE buyer_type = 'consumer' AND withdrawal_waiver_accepted_at IS NOT NULL;

-- osm_reader SELECT grant (table-level; covers all current + future columns).
-- The CREATE TABLE grant in m13_014 already covers osm_reader on subscriptions;
-- new columns inherit automatically on Postgres. This is a belt-and-suspenders
-- explicit grant that is idempotent if it was already granted.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'osm_reader') THEN
        GRANT SELECT ON subscriptions TO osm_reader;
    END IF;
END $$;
