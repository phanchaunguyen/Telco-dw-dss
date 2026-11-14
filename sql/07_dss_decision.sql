-- ===========================================================================
--  07_dss_decision.sql   (DSS — decision layer: who gets a retention offer)
--
--  Requires gold.dss_churn_score, written by src/06_dss_model.py.
--
--  The decision model
--  ------------------
--  For every customer active in the chosen month:
--
--      value_if_retained = bill * margin * expected_future_cycles
--      expected_gain     = p_churn * save_rate * value_if_retained - offer_cost
--
--  expected_future_cycles = 1 / base_hazard. The hazard is flat across tenure
--  (23-25% in every cycle with enough rows), so remaining lifetime is
--  geometric and its mean is 1/h. With h = 24.68% that is ~4.05 cycles.
--
--  Customers are ranked by expected_gain and taken greedily while the
--  cumulative offer cost stays within the budget and the gain stays positive.
--  With one offer type at one fixed cost this greedy order is optimal for
--  the budget-constrained problem (every item has the same weight).
--
--  Parameters the manager controls (what-if):
--      p_month       which month's active base to plan for
--      p_budget      money available for offers this month
--      p_offer_cost  cost of one offer (default 10 ~ mean discounts_applied)
--      p_save_rate   share of would-be churners an offer actually keeps
--      p_margin      gross margin on revenue (1.0 = plan on revenue)
--
--  Because churn_flag is known for the historical months, both functions also
--  return what really happened, which turns the planner into a backtest.
-- ===========================================================================

CREATE OR REPLACE FUNCTION gold.fn_retention_plan(
    p_month       DATE,
    p_budget      NUMERIC,
    p_offer_cost  NUMERIC DEFAULT 10,
    p_save_rate   NUMERIC DEFAULT 0.30,
    p_margin      NUMERIC DEFAULT 1.0
)
RETURNS TABLE (
    priority           BIGINT,
    customer_id        TEXT,
    risk_band          TEXT,
    p_churn            NUMERIC,
    bill               NUMERIC,
    value_if_retained  NUMERIC,
    expected_gain      NUMERIC,
    cumulative_cost    NUMERIC,
    selected           BOOLEAN,
    actually_churned   BOOLEAN
)
LANGUAGE sql STABLE AS
$$
    WITH base AS (
        SELECT 1.0 / AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END) AS future_cycles
        FROM gold.fact_billing_support
    ),
    scored AS (
        SELECT
            c.customer_id,
            s.risk_band,
            s.p_churn::numeric                                         AS p_churn,
            f.total_bill_amount                                        AS bill,
            f.total_bill_amount * p_margin * b.future_cycles           AS value_if_retained,
            s.p_churn::numeric * p_save_rate
                * f.total_bill_amount * p_margin * b.future_cycles
                - p_offer_cost                                         AS expected_gain,
            s.churn_flag                                               AS actually_churned
        FROM gold.dss_churn_score s
        JOIN gold.fact_billing_support f ON f.fact_key     = s.fact_key
        JOIN gold.dim_customer         c ON c.customer_key = s.customer_key
        CROSS JOIN base b
        WHERE date_trunc('month', s.cycle_start) = date_trunc('month', p_month)
    ),
    ranked AS (
        SELECT
            ROW_NUMBER() OVER (ORDER BY expected_gain DESC, customer_id) AS priority,
            sc.*
        FROM scored sc
    )
    SELECT
        r.priority,
        r.customer_id,
        r.risk_band,
        ROUND(r.p_churn, 4),
        r.bill,
        ROUND(r.value_if_retained, 2),
        ROUND(r.expected_gain, 2),
        r.priority * p_offer_cost                                      AS cumulative_cost,
        (r.expected_gain > 0 AND r.priority * p_offer_cost <= p_budget) AS selected,
        r.actually_churned
    FROM ranked r
    ORDER BY r.priority;
$$;

COMMENT ON FUNCTION gold.fn_retention_plan(DATE, NUMERIC, NUMERIC, NUMERIC, NUMERIC) IS
  'DSS planner: ranks the month''s active customers by expected gain of a retention offer under a budget.';

-- ---------------------------------------------------------------------------
-- One-row summary of a plan: what it costs, what it is expected to return,
-- and (backtest) how many of the selected customers really churned.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION gold.fn_retention_plan_summary(
    p_month       DATE,
    p_budget      NUMERIC,
    p_offer_cost  NUMERIC DEFAULT 10,
    p_save_rate   NUMERIC DEFAULT 0.30,
    p_margin      NUMERIC DEFAULT 1.0
)
RETURNS TABLE (
    active_customers        BIGINT,
    selected_customers      BIGINT,
    total_cost              NUMERIC,
    expected_saves          NUMERIC,
    expected_value_saved    NUMERIC,
    expected_net_value      NUMERIC,
    roi_pct                 NUMERIC,
    churners_in_selection   BIGINT,
    precision_pct           NUMERIC,
    recall_pct              NUMERIC
)
LANGUAGE sql STABLE AS
$$
    SELECT
        COUNT(*),
        COUNT(*) FILTER (WHERE selected),
        COUNT(*) FILTER (WHERE selected) * p_offer_cost,
        ROUND(SUM(p_churn * p_save_rate) FILTER (WHERE selected), 1),
        ROUND(SUM(p_churn * p_save_rate * value_if_retained) FILTER (WHERE selected), 2),
        ROUND(SUM(expected_gain) FILTER (WHERE selected), 2),
        ROUND(100.0 * SUM(expected_gain) FILTER (WHERE selected)
              / NULLIF(COUNT(*) FILTER (WHERE selected) * p_offer_cost, 0), 1),
        COUNT(*) FILTER (WHERE selected AND actually_churned),
        ROUND(100.0 * COUNT(*) FILTER (WHERE selected AND actually_churned)
              / NULLIF(COUNT(*) FILTER (WHERE selected), 0), 2),
        ROUND(100.0 * COUNT(*) FILTER (WHERE selected AND actually_churned)
              / NULLIF(COUNT(*) FILTER (WHERE actually_churned), 0), 2)
    FROM gold.fn_retention_plan(p_month, p_budget, p_offer_cost, p_save_rate, p_margin);
$$;

COMMENT ON FUNCTION gold.fn_retention_plan_summary(DATE, NUMERIC, NUMERIC, NUMERIC, NUMERIC) IS
  'DSS planner summary: cost, expected saves, net value, ROI and backtest precision/recall for one plan.';

-- ---------------------------------------------------------------------------
-- Risk band overview per month (dashboard tile, no parameters).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.dss_risk_band_monthly AS
SELECT
    date_trunc('month', s.cycle_start)::date                   AS month_start,
    s.risk_band,
    COUNT(*)                                                    AS customers,
    ROUND(AVG(s.p_churn)::numeric * 100, 2)                     AS avg_predicted_pct,
    ROUND(100.0 * AVG(CASE WHEN s.churn_flag THEN 1.0 ELSE 0.0 END), 2)
                                                                AS actual_churn_pct,
    SUM(f.total_bill_amount)                                    AS revenue_at_stake
FROM gold.dss_churn_score s
JOIN gold.fact_billing_support f ON f.fact_key = s.fact_key
GROUP BY 1, 2;

COMMENT ON VIEW gold.dss_risk_band_monthly IS
  'Monthly count, predicted vs actual churn and revenue per risk band.';
