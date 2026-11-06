-- ===========================================================================
--  03_marts.sql   (ELT — business-facing views on top of the star schema)
--
--  Three marts, each answering one DSS question:
--    mart_billing_monthly  -> revenue / ARPU / overdue trend
--    mart_support_monthly  -> support operations performance
--    mart_churn_summary    -> churn risk by segment
--
--  KPI definitions are documented in README.md.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- mart_billing_monthly
--
--  Grain: 1 row per (year, month).
--  Uses ALL 112,552 fact rows: revenue must never be filtered by support
--  activity, otherwise ~16.6% of the money disappears.
--
--  month_start is a real DATE column on purpose: Metabase can only build a
--  drill-down hierarchy on a DATE/TIMESTAMP column, not on separate
--  year/quarter/month integers.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.mart_billing_monthly AS
SELECT
    d.year,
    d.quarter,
    d.month,
    MIN(d.full_date)                                        AS month_start,
    COUNT(*)                                                AS billing_cycles,
    COUNT(DISTINCT f.customer_key)                          AS active_customers,
    SUM(f.total_bill_amount)                                AS total_revenue,
    ROUND(SUM(f.total_bill_amount) / NULLIF(COUNT(DISTINCT f.customer_key), 0), 2)
                                                            AS arpu,
    SUM(f.late_fee)                                         AS total_late_fee,
    SUM(f.discounts_applied)                                AS total_discount,
    -- overdue = anything not fully paid
    COUNT(*) FILTER (WHERE ps.payment_status IN ('Unpaid', 'Partial'))
                                                            AS overdue_cycles,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE ps.payment_status IN ('Unpaid', 'Partial'))
        / NULLIF(COUNT(*), 0), 2
    )                                                       AS overdue_rate,
    -- customers who never contacted support this month (D14)
    COUNT(*) FILTER (WHERE f.support_interaction_count = 0)
                                                            AS no_support_customers,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.churn_flag) / NULLIF(COUNT(*), 0), 2)
                                                            AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_date           d  ON d.date_key           = f.billing_start_date_key
JOIN gold.dim_payment_status ps ON ps.payment_status_key = f.payment_status_key
WHERE d.is_unknown = FALSE
GROUP BY d.year, d.quarter, d.month;

COMMENT ON VIEW gold.mart_billing_monthly IS
  'Monthly revenue / ARPU / overdue / churn. Grain: 1 row per year+month. Uses all fact rows.';

-- ---------------------------------------------------------------------------
-- mart_support_monthly
--
--  Grain: 1 row per (year, month, channel, issue_type).
--
--  Only rows WITH a real support interaction are included (D6/D14).
--  The 18,719 no-interaction rows carry sentinel values
--  (satisfaction_score = 5, resolution_time_minutes = 0) that would inflate
--  CSAT by ~11% and deflate average resolution time by ~19%.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.mart_support_monthly AS
SELECT
    d.year,
    d.quarter,
    d.month,
    MIN(d.full_date)                                        AS month_start,
    ch.support_channel,
    it.issue_type,
    COUNT(*)                                                AS tickets,
    ROUND(AVG(f.resolution_time_minutes), 2)                AS avg_resolution_min,
    ROUND(AVG(f.satisfaction_score), 2)                     AS csat,
    COUNT(*) FILTER (WHERE f.escalation_flag)               AS escalations,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.escalation_flag) / NULLIF(COUNT(*), 0), 2
    )                                                       AS escalation_rate,
    ROUND(AVG(f.support_interaction_count), 2)              AS avg_interactions
FROM gold.fact_billing_support f
JOIN gold.dim_date            d  ON d.date_key            = f.billing_start_date_key
JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
WHERE d.is_unknown = FALSE
  AND f.support_interaction_count > 0
GROUP BY d.year, d.quarter, d.month, ch.support_channel, it.issue_type;

COMMENT ON VIEW gold.mart_support_monthly IS
  'Monthly support performance. Grain: 1 row per year+month+channel+issue_type. Excludes no-interaction rows.';

-- ---------------------------------------------------------------------------
-- mart_churn_summary
--
--  Grain: 1 row per (churn_flag, payment_status, issue_type, channel).
--  Uses ALL fact rows, including the 'No Support' / 'None' members, so the
--  silent-churn segment stays visible (D7).
--
--  churn_rate is a ROW-level rate (share of billing cycles flagged churned),
--  which is what the spec asks for. churned_customers is added so the
--  customer-level view can be cross-checked: one customer can churn in one
--  cycle and not in another, so the two numbers legitimately differ.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.mart_churn_summary AS
SELECT
    f.churn_flag,
    ps.payment_status,
    it.issue_type,
    ch.support_channel,
    COUNT(*)                                                AS billing_cycles,
    COUNT(DISTINCT f.customer_key)                          AS customers,
    COUNT(DISTINCT f.customer_key) FILTER (WHERE f.churn_flag)
                                                            AS churned_customers,
    ROUND(AVG(f.total_bill_amount), 2)                      AS avg_bill,
    ROUND(AVG(f.satisfaction_score), 2)                     AS avg_csat,
    ROUND(AVG(f.support_interaction_count), 2)              AS avg_interactions,
    ROUND(AVG(f.resolution_time_minutes), 2)                AS avg_resolution,
    ROUND(100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END), 2)
                                                            AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_payment_status  ps ON ps.payment_status_key  = f.payment_status_key
JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
GROUP BY f.churn_flag, ps.payment_status, it.issue_type, ch.support_channel;

COMMENT ON VIEW gold.mart_churn_summary IS
  'Churn by segment. Grain: 1 row per churn_flag+payment_status+issue_type+channel. Uses all fact rows.';
