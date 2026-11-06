-- ===========================================================================
--  05_olap_demos.sql
--
--  Five OLAP operations demonstrated with runnable SQL, plus a CUBE bonus.
--  Every literal below comes from the real dataset (see data/profile.txt):
--      payment_status     : 'Paid' | 'Unpaid' | 'Partial'
--      primary_issue_type : 'Billing' | 'Technical' | 'Service Quality' | 'No Support'
--      support_channel    : 'Email' | 'Phone' | 'Chat' | 'None'
--
--  Run with:  make olap-demo
-- ===========================================================================


-- ===========================================================================
-- [OLAP-1] ROLL-UP: revenue by year -> quarter -> month
--
--  ROLLUP produces the hierarchy subtotals in one pass:
--    (year, quarter, month) -> detail rows
--    (year, quarter)        -> quarter subtotal
--    (year)                 -> year subtotal
--    ()                     -> grand total
--  GROUPING() tells a real NULL apart from a subtotal NULL.
-- ===========================================================================
SELECT
    d.year,
    d.quarter,
    d.month,
    GROUPING(d.year, d.quarter, d.month)          AS grouping_level,
    CASE GROUPING(d.year, d.quarter, d.month)
        WHEN 0 THEN 'month'
        WHEN 1 THEN 'quarter subtotal'
        WHEN 3 THEN 'year subtotal'
        WHEN 7 THEN 'GRAND TOTAL'
    END                                           AS level_label,
    COUNT(*)                                      AS billing_cycles,
    SUM(f.total_bill_amount)                      AS revenue,
    ROUND(AVG(f.total_bill_amount), 2)            AS avg_bill
FROM gold.fact_billing_support f
JOIN gold.dim_date d ON d.date_key = f.billing_start_date_key
WHERE d.is_unknown = FALSE
GROUP BY ROLLUP (d.year, d.quarter, d.month)
ORDER BY d.year NULLS LAST, d.quarter NULLS LAST, d.month NULLS LAST;


-- ===========================================================================
-- [OLAP-2] DRILL-DOWN: year level -> month level
--
--  Same cube, two zoom levels side by side. GROUPING() marks which level each
--  row belongs to, so a single result set shows both the yearly summary and
--  the monthly detail underneath it.
-- ===========================================================================
SELECT
    CASE GROUPING(d.year, d.month)
        WHEN 0 THEN '2. month detail'
        WHEN 1 THEN '1. year total'
    END                                           AS drill_level,
    d.year,
    d.month,
    COUNT(*)                                      AS billing_cycles,
    SUM(f.total_bill_amount)                      AS revenue,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.churn_flag) / COUNT(*), 2)
                                                  AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_date d ON d.date_key = f.billing_start_date_key
WHERE d.is_unknown = FALSE
GROUP BY GROUPING SETS ((d.year), (d.year, d.month))
ORDER BY d.year, GROUPING(d.year, d.month), d.month;


-- ===========================================================================
-- [OLAP-3] SLICE: fix ONE dimension, aggregate the rest
--
--  Slice on payment_status = 'Paid': what does the fully-paid population look
--  like across issue types and channels?
-- ===========================================================================
SELECT
    it.issue_type,
    ch.support_channel,
    COUNT(*)                                      AS billing_cycles,
    SUM(f.total_bill_amount)                      AS revenue,
    ROUND(AVG(f.total_bill_amount), 2)            AS avg_bill,
    ROUND(AVG(f.satisfaction_score), 2)           AS avg_csat
FROM gold.fact_billing_support f
JOIN gold.dim_payment_status  ps ON ps.payment_status_key  = f.payment_status_key
JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
WHERE ps.payment_status = 'Paid'          -- <-- the slice
GROUP BY it.issue_type, ch.support_channel
ORDER BY revenue DESC;


-- ===========================================================================
-- [OLAP-4] DICE: filter on THREE dimensions at once
--
--  year = 2022  AND  payment_status IN ('Paid','Unpaid')  AND  issue_type = 'Billing'
-- ===========================================================================
SELECT
    d.year,
    d.quarter,
    ps.payment_status,
    it.issue_type,
    COUNT(*)                                      AS billing_cycles,
    SUM(f.total_bill_amount)                      AS revenue,
    SUM(f.late_fee)                               AS late_fees,
    ROUND(AVG(f.total_bill_amount), 2)            AS avg_bill
FROM gold.fact_billing_support f
JOIN gold.dim_date           d  ON d.date_key            = f.billing_start_date_key
JOIN gold.dim_payment_status ps ON ps.payment_status_key = f.payment_status_key
JOIN gold.dim_issue_type     it ON it.issue_type_key     = f.issue_type_key
WHERE d.year = 2022
  AND ps.payment_status IN ('Paid', 'Unpaid')
  AND it.issue_type = 'Billing'
GROUP BY d.year, d.quarter, ps.payment_status, it.issue_type
ORDER BY d.quarter, ps.payment_status;


-- ===========================================================================
-- [OLAP-5] PIVOT: churn rate, issue_type (rows) x channel (columns)
--
--  Conditional aggregation instead of a crosstab extension.
--  The 3x3 matrix covers only the real support interactions; the 'No Support'
--  segment is analysed separately in [OLAP-5b] because it has no channel.
-- ===========================================================================
SELECT
    it.issue_type,
    ROUND(100.0 * AVG(CASE WHEN ch.support_channel = 'Email'
                           THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END), 2)
                                                  AS churn_email,
    ROUND(100.0 * AVG(CASE WHEN ch.support_channel = 'Phone'
                           THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END), 2)
                                                  AS churn_phone,
    ROUND(100.0 * AVG(CASE WHEN ch.support_channel = 'Chat'
                           THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END), 2)
                                                  AS churn_chat,
    COUNT(*)                                      AS billing_cycles
FROM gold.fact_billing_support f
JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
WHERE it.issue_type <> 'No Support'
GROUP BY it.issue_type
ORDER BY it.issue_type;


-- ===========================================================================
-- [OLAP-5b] SILENT CHURN: customers who never contacted support
--
--  The 'No Support' segment is a real business state, not missing data.
--  Comparing its churn rate with the contacted population is the whole point
--  of keeping those 18,719 rows.
-- ===========================================================================
SELECT
    CASE WHEN f.support_interaction_count = 0
         THEN 'No support contact'
         ELSE 'Contacted support'
    END                                           AS segment,
    COUNT(*)                                      AS billing_cycles,
    COUNT(DISTINCT f.customer_key)                AS customers,
    COUNT(*) FILTER (WHERE f.churn_flag)          AS churned_cycles,
    ROUND(100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END), 2)
                                                  AS churn_rate,
    ROUND(AVG(f.total_bill_amount), 2)            AS avg_bill,
    ROUND(AVG(f.satisfaction_score), 2)           AS avg_csat
FROM gold.fact_billing_support f
GROUP BY 1
ORDER BY churn_rate DESC;


-- ===========================================================================
-- [BONUS] CUBE: churn rate over (issue_type x channel x payment_status)
--
--  CUBE produces every combination of subtotals, including the 2-way and
--  1-way margins. GROUPING() marks which dimensions are aggregated away.
-- ===========================================================================
SELECT
    it.issue_type,
    ch.support_channel,
    ps.payment_status,
    GROUPING(it.issue_type, ch.support_channel, ps.payment_status) AS grouping_level,
    COUNT(*)                                      AS billing_cycles,
    ROUND(100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END), 2)
                                                  AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
JOIN gold.dim_payment_status  ps ON ps.payment_status_key  = f.payment_status_key
GROUP BY CUBE (it.issue_type, ch.support_channel, ps.payment_status)
ORDER BY grouping_level, it.issue_type NULLS LAST, ch.support_channel NULLS LAST,
         ps.payment_status NULLS LAST;
