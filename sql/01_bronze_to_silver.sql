-- ===========================================================================
--  01_bronze_to_silver.sql   (ELT — all transformation happens here, in SQL)
--
--  Bronze holds raw TEXT. This script types, cleans and deduplicates it.
--  Python only substitutes {{batch_id}} and executes the file.
--
--  Idempotency strategy
--  --------------------
--  Bronze is append-only, so the same business row can appear in several
--  batches. We process ONLY the batch passed in {{batch_id}} and rely on the
--  UNIQUE (customer_id, billing_cycle_start) constraint plus
--  ON CONFLICT DO NOTHING to make re-runs harmless.
--
--  Verified against the real file (see data/profile.txt):
--    * dates      : ISO 'YYYY-MM-DD', 0 unparseable
--    * booleans   : 'True' / 'False'
--    * numerics   : plain decimal text, empty string for missing
--    * 18,719 rows have NULL primary_issue_type / support_channel /
--      last_interaction_date — these are kept as NULL here on purpose.
--      The 'No Support' / 'None' members are assigned in the GOLD layer so
--      that Silver stays faithful to the source.
-- ===========================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Target table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.silver_customer_billing (
    customer_id                TEXT          NOT NULL,
    billing_cycle_start        DATE          NOT NULL,
    billing_cycle_end          DATE          NOT NULL,
    call_minutes               BIGINT,
    data_usage_gb              DOUBLE PRECISION,
    sms_count                  BIGINT,
    internet_sessions          BIGINT,
    total_bill_amount          NUMERIC(12,2),
    payment_status             TEXT,
    payment_due_date           DATE,
    late_fee                   NUMERIC(12,2),
    discounts_applied          NUMERIC(12,2),
    support_interaction_count  BIGINT,
    last_interaction_date      DATE,
    primary_issue_type         TEXT,
    resolution_time_minutes    BIGINT,
    satisfaction_score         BIGINT,
    escalation_flag            BOOLEAN,
    support_channel            TEXT,
    has_churned                BOOLEAN,

    -- lineage
    ingest_batch_id            TEXT          NOT NULL,
    ingest_ts                  TIMESTAMP     NOT NULL,

    -- Business key: one row per customer per billing cycle.
    CONSTRAINT uq_silver_grain UNIQUE (customer_id, billing_cycle_start)
);

COMMENT ON TABLE silver.silver_customer_billing IS
  'Cleaned, typed, deduplicated billing+support rows. Grain: 1 row = 1 billing cycle of 1 customer.';

CREATE INDEX IF NOT EXISTS ix_silver_billing_start
    ON silver.silver_customer_billing (billing_cycle_start);
CREATE INDEX IF NOT EXISTS ix_silver_payment_status
    ON silver.silver_customer_billing (payment_status);
CREATE INDEX IF NOT EXISTS ix_silver_issue_type
    ON silver.silver_customer_billing (primary_issue_type);
CREATE INDEX IF NOT EXISTS ix_silver_channel
    ON silver.silver_customer_billing (support_channel);

-- ---------------------------------------------------------------------------
-- Transform: bronze -> silver
--
-- Helper expressions used below:
--   * NULLIF(TRIM(col), '')  turns empty strings into real NULLs
--   * TO_DATE(col,'YYYY-MM-DD')  explicit format, no DateStyle dependency
--   * CASE ... boolean parsing   tolerant of True/False/t/f/1/0/yes/no
-- ---------------------------------------------------------------------------
INSERT INTO silver.silver_customer_billing (
    customer_id,
    billing_cycle_start,
    billing_cycle_end,
    call_minutes,
    data_usage_gb,
    sms_count,
    internet_sessions,
    total_bill_amount,
    payment_status,
    payment_due_date,
    late_fee,
    discounts_applied,
    support_interaction_count,
    last_interaction_date,
    primary_issue_type,
    resolution_time_minutes,
    satisfaction_score,
    escalation_flag,
    support_channel,
    has_churned,
    ingest_batch_id,
    ingest_ts
)
SELECT
    NULLIF(TRIM(b.customer_id), '')                                   AS customer_id,
    TO_DATE(NULLIF(TRIM(b.billing_cycle_start), ''), 'YYYY-MM-DD')    AS billing_cycle_start,
    TO_DATE(NULLIF(TRIM(b.billing_cycle_end),   ''), 'YYYY-MM-DD')    AS billing_cycle_end,
    NULLIF(TRIM(b.call_minutes), '')::BIGINT                          AS call_minutes,
    NULLIF(TRIM(b.data_usage_gb), '')::DOUBLE PRECISION               AS data_usage_gb,
    NULLIF(TRIM(b.sms_count), '')::BIGINT                             AS sms_count,
    NULLIF(TRIM(b.internet_sessions), '')::BIGINT                     AS internet_sessions,
    NULLIF(TRIM(b.total_bill_amount), '')::NUMERIC(12,2)              AS total_bill_amount,
    NULLIF(TRIM(b.payment_status), '')                                AS payment_status,
    TO_DATE(NULLIF(TRIM(b.payment_due_date), ''), 'YYYY-MM-DD')       AS payment_due_date,
    NULLIF(TRIM(b.late_fee), '')::NUMERIC(12,2)                       AS late_fee,
    NULLIF(TRIM(b.discounts_applied), '')::NUMERIC(12,2)              AS discounts_applied,
    NULLIF(TRIM(b.support_interaction_count), '')::BIGINT             AS support_interaction_count,
    TO_DATE(NULLIF(TRIM(b.last_interaction_date), ''), 'YYYY-MM-DD')  AS last_interaction_date,
    NULLIF(TRIM(b.primary_issue_type), '')                            AS primary_issue_type,
    NULLIF(TRIM(b.resolution_time_minutes), '')::BIGINT               AS resolution_time_minutes,
    NULLIF(TRIM(b.satisfaction_score), '')::BIGINT                    AS satisfaction_score,
    CASE
        WHEN LOWER(TRIM(b.escalation_flag)) IN ('true','t','1','yes')  THEN TRUE
        WHEN LOWER(TRIM(b.escalation_flag)) IN ('false','f','0','no')  THEN FALSE
        ELSE NULL
    END                                                               AS escalation_flag,
    NULLIF(TRIM(b.support_channel), '')                               AS support_channel,
    CASE
        WHEN LOWER(TRIM(b.has_churned)) IN ('true','t','1','yes')      THEN TRUE
        WHEN LOWER(TRIM(b.has_churned)) IN ('false','f','0','no')      THEN FALSE
        ELSE NULL
    END                                                               AS has_churned,
    b.ingest_batch_id,
    b.ingest_ts
FROM bronze.bronze_customer_billing b
WHERE b.ingest_batch_id = '{{batch_id}}'
  -- Guard: rows that cannot form a valid business key are dropped here
  -- rather than failing the whole load. The DQ layer reports the count.
  AND NULLIF(TRIM(b.customer_id), '') IS NOT NULL
  AND NULLIF(TRIM(b.billing_cycle_start), '') IS NOT NULL
ON CONFLICT ON CONSTRAINT uq_silver_grain DO NOTHING;

COMMIT;
