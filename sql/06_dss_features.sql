-- ===========================================================================
--  06_dss_features.sql   (DSS — model input layer, all in SQL)
--
--  One row per fact row (= 1 billing cycle of 1 customer), with the features
--  the churn model reads. Built only from gold.fact_billing_support and its
--  dimensions, so the DSS never reaches back into silver/bronze.
--
--  Why the label is a hazard, not "will this customer ever churn"
--  ---------------------------------------------------------------
--  churn_flag is TRUE on exactly the LAST cycle of every churned customer
--  (27,777 rows) and FALSE everywhere else. 27,777 of 27,778 customers churn
--  eventually, so "will they churn" has a trivial answer. The useful question
--  is "will this cycle be their last one", i.e. the discrete-time hazard
--  P(churn at t | still active at t). A logistic regression on cycle rows
--  estimates exactly that.
--
--  Sentinel handling (same rule as mart_support_monthly)
--  -----------------------------------------------------
--  Rows without a support contact carry satisfaction_score = 5 and
--  resolution_time_minutes = 0. Feeding those raw would teach the model that
--  "CSAT 5" means "never called". So CSAT and resolution only count when
--  contacted = TRUE, and are 0 otherwise; `contacted` itself carries the
--  no-contact signal.
--
--  History features only look BACKWARDS (... AND 1 PRECEDING), so a cycle's
--  features never include anything from that cycle's own future.
-- ===========================================================================

CREATE OR REPLACE VIEW gold.dss_cycle_features AS
WITH base AS (
    SELECT
        f.fact_key,
        f.customer_key,
        f.billing_start_date_key,
        d.full_date                                             AS cycle_start,
        ROW_NUMBER() OVER (PARTITION BY f.customer_key
                           ORDER BY f.billing_start_date_key)   AS tenure_cycle,

        -- billing
        f.total_bill_amount::double precision                   AS bill,
        f.late_fee::double precision                            AS late_fee,
        f.discounts_applied::double precision                   AS discount,
        (ps.payment_status = 'Unpaid')                          AS is_unpaid,
        (ps.payment_status = 'Partial')                         AS is_partial,

        -- usage
        f.call_minutes::double precision                        AS call_minutes,
        f.data_usage_gb                                         AS data_gb,
        f.sms_count::double precision                           AS sms,
        f.internet_sessions::double precision                   AS sessions,

        -- support (sentinel-safe)
        f.support_interaction_count::double precision           AS contacts,
        (f.support_interaction_count > 0)                       AS contacted,
        CASE WHEN f.support_interaction_count > 0
             THEN f.satisfaction_score - 3 ELSE 0 END::double precision
                                                                AS csat_centered,
        (f.support_interaction_count > 0 AND f.satisfaction_score <= 2)
                                                                AS csat_low,
        CASE WHEN f.support_interaction_count > 0
             THEN f.resolution_time_minutes ELSE 0 END::double precision
                                                                AS resolution_min,
        COALESCE(f.escalation_flag, FALSE)                      AS escalated,
        it.issue_type,
        ch.support_channel,

        f.churn_flag
    FROM gold.fact_billing_support f
    JOIN gold.dim_date            d  ON d.date_key             = f.billing_start_date_key
    JOIN gold.dim_payment_status  ps ON ps.payment_status_key  = f.payment_status_key
    JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
    JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
)
SELECT
    b.*,
    -- history: everything strictly BEFORE this cycle
    COALESCE(SUM(b.is_unpaid::int) OVER w, 0)                   AS prior_unpaid_cycles,
    COALESCE(SUM(b.escalated::int) OVER w, 0)                   AS prior_escalations,
    COALESCE(SUM(b.contacts)       OVER w, 0)                   AS prior_contacts
FROM base b
WINDOW w AS (PARTITION BY b.customer_key
             ORDER BY b.billing_start_date_key
             ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING);

COMMENT ON VIEW gold.dss_cycle_features IS
  'DSS model input. Grain: 1 row per billing cycle. Sentinel-safe support features, backward-only history.';
