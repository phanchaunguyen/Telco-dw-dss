-- ===========================================================================
--  04_materialized_views.sql   (ELT — pre-aggregated OLAP performance layer)
--
--  Why materialized views here?
--    The marts are plain views: every Metabase refresh re-runs the full
--    aggregation over 112,552 fact rows. The two heaviest dashboards
--    (Revenue & Billing, Churn Risk) read pre-computed snapshots instead.
--
--  REFRESH ... CONCURRENTLY requires a UNIQUE index that covers all rows and
--  uses only plain column names (no expressions, no WHERE clause). Both MVs
--  below satisfy that, so `make refresh-mv` can run without locking readers.
--
--  Note: CREATE MATERIALIZED VIEW IF NOT EXISTS does NOT repopulate an
--  existing MV. The explicit REFRESH statements at the end of this file make
--  `make marts` idempotent.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- mv_billing_monthly
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.mv_billing_monthly AS
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
    COUNT(*) FILTER (WHERE ps.payment_status IN ('Unpaid', 'Partial'))
                                                            AS overdue_cycles,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE ps.payment_status IN ('Unpaid', 'Partial'))
        / NULLIF(COUNT(*), 0), 2
    )                                                       AS overdue_rate,
    COUNT(*) FILTER (WHERE f.support_interaction_count = 0)
                                                            AS no_support_customers,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.churn_flag) / NULLIF(COUNT(*), 0), 2)
                                                            AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_date           d  ON d.date_key            = f.billing_start_date_key
JOIN gold.dim_payment_status ps ON ps.payment_status_key = f.payment_status_key
WHERE d.is_unknown = FALSE
GROUP BY d.year, d.quarter, d.month;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_billing_monthly
    ON gold.mv_billing_monthly (year, quarter, month);

COMMENT ON MATERIALIZED VIEW gold.mv_billing_monthly IS
  'Pre-aggregated monthly revenue/ARPU/overdue. Refreshed by `make refresh-mv`.';

-- ---------------------------------------------------------------------------
-- mv_churn_by_issue_channel
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.mv_churn_by_issue_channel AS
SELECT
    it.issue_type,
    ch.support_channel,
    COUNT(*)                                                AS billing_cycles,
    COUNT(DISTINCT f.customer_key)                          AS customers,
    COUNT(*) FILTER (WHERE f.churn_flag)                    AS churned_cycles,
    ROUND(100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END), 2)
                                                            AS churn_rate,
    ROUND(AVG(f.total_bill_amount), 2)                      AS avg_bill,
    ROUND(AVG(f.satisfaction_score), 2)                     AS avg_csat,
    ROUND(AVG(f.resolution_time_minutes), 2)                AS avg_resolution
FROM gold.fact_billing_support f
JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
GROUP BY it.issue_type, ch.support_channel;

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_churn_by_issue_channel
    ON gold.mv_churn_by_issue_channel (issue_type, support_channel);

COMMENT ON MATERIALIZED VIEW gold.mv_churn_by_issue_channel IS
  'Pre-aggregated churn by issue_type x channel. Refreshed by `make refresh-mv`.';

-- ---------------------------------------------------------------------------
-- Repopulate on every run.
-- Non-concurrent on purpose: this runs inside `make marts` where a brief lock
-- is acceptable, and CONCURRENTLY cannot run inside a transaction block.
-- ---------------------------------------------------------------------------
REFRESH MATERIALIZED VIEW gold.mv_billing_monthly;
REFRESH MATERIALIZED VIEW gold.mv_churn_by_issue_channel;
