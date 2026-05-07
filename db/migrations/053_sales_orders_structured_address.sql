-- ============================================================
-- Migration 053: structured sales_orders billing/shipping address (v1.8.0 #288)
-- ============================================================
-- v1.7 mig 046 added sales_orders.billing_address + shipping_address
-- as TEXT placeholders. v1.8 replaces them with 16 structured columns
-- so operators (and inbound mapping docs) can address each component
-- independently:
--
--   billing_address_name     -- recipient
--   billing_address_line1    -- street, line 1
--   billing_address_line2    -- street, line 2 (apt, suite)
--   billing_address_city
--   billing_address_state    -- state / province / region (free text)
--   billing_address_postal_code
--   billing_address_country  -- country name OR ISO-3166 code; operator
--                               convention (no validation)
--   billing_address_phone
--
--   shipping_address_*       -- mirror
--
-- The 2 v1.7 TEXT columns DROP. No production deployments exist so
-- the breaking shape is safe; in-flight mapping docs that still
-- reference billing_address / shipping_address fail boot loud via the
-- #267 canonical-column validator with the offending file path so
-- operators see a clear error and migrate to per-component fields.
--
-- v1.8.0 migration discipline: SET lock_timeout / statement_timeout
-- at the top so a bad migration fails fast. BEGIN/COMMIT-wrapped
-- per V-213.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    DROP COLUMN IF EXISTS billing_address,
    DROP COLUMN IF EXISTS shipping_address;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS billing_address_name        VARCHAR(200),
    ADD COLUMN IF NOT EXISTS billing_address_line1       VARCHAR(200),
    ADD COLUMN IF NOT EXISTS billing_address_line2       VARCHAR(200),
    ADD COLUMN IF NOT EXISTS billing_address_city        VARCHAR(100),
    ADD COLUMN IF NOT EXISTS billing_address_state       VARCHAR(100),
    ADD COLUMN IF NOT EXISTS billing_address_postal_code VARCHAR(32),
    ADD COLUMN IF NOT EXISTS billing_address_country     VARCHAR(64),
    ADD COLUMN IF NOT EXISTS billing_address_phone       VARCHAR(64),
    ADD COLUMN IF NOT EXISTS shipping_address_name        VARCHAR(200),
    ADD COLUMN IF NOT EXISTS shipping_address_line1       VARCHAR(200),
    ADD COLUMN IF NOT EXISTS shipping_address_line2       VARCHAR(200),
    ADD COLUMN IF NOT EXISTS shipping_address_city        VARCHAR(100),
    ADD COLUMN IF NOT EXISTS shipping_address_state       VARCHAR(100),
    ADD COLUMN IF NOT EXISTS shipping_address_postal_code VARCHAR(32),
    ADD COLUMN IF NOT EXISTS shipping_address_country     VARCHAR(64),
    ADD COLUMN IF NOT EXISTS shipping_address_phone       VARCHAR(64);

COMMIT;
