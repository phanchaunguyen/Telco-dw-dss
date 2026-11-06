#!/usr/bin/env python3
"""
Phase G — Data-quality validation.

Runs 12 rules against bronze / silver / gold, writes one row per rule into
gold.dq_results, prints a summary and exits non-zero if any rule FAILs.

Rules are pure SQL: this script only orchestrates and reports.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

# (rule_id, layer, table, column, description, failing_rows_sql)
# Each SQL must return a single integer = number of rows violating the rule.
RULES: list[tuple[str, str, str, str | None, str, str]] = [
    (
        "DQ-01",
        "bronze",
        "bronze.bronze_customer_billing",
        "customer_id",
        "customer_id must never be NULL",
        "SELECT COUNT(*) FROM bronze.bronze_customer_billing WHERE customer_id IS NULL",
    ),
    (
        "DQ-02",
        "silver",
        "silver.silver_customer_billing",
        "customer_id,billing_cycle_start",
        "business key (customer_id, billing_cycle_start) must be unique",
        """
        SELECT COALESCE(SUM(cnt - 1), 0) FROM (
            SELECT COUNT(*) AS cnt
            FROM silver.silver_customer_billing
            GROUP BY customer_id, billing_cycle_start
            HAVING COUNT(*) > 1
        ) t
        """,
    ),
    (
        "DQ-03",
        "silver",
        "silver.silver_customer_billing",
        "satisfaction_score",
        "satisfaction_score must be between 1 and 5, or NULL",
        """
        SELECT COUNT(*) FROM silver.silver_customer_billing
        WHERE satisfaction_score IS NOT NULL
          AND (satisfaction_score < 1 OR satisfaction_score > 5)
        """,
    ),
    (
        "DQ-04",
        "silver",
        "silver.silver_customer_billing",
        "total_bill_amount",
        "total_bill_amount must be >= 0",
        "SELECT COUNT(*) FROM silver.silver_customer_billing WHERE total_bill_amount < 0",
    ),
    (
        "DQ-05",
        "silver",
        "silver.silver_customer_billing",
        "late_fee,discounts_applied",
        "late_fee and discounts_applied must be >= 0",
        """
        SELECT COUNT(*) FROM silver.silver_customer_billing
        WHERE late_fee < 0 OR discounts_applied < 0
        """,
    ),
    (
        "DQ-06",
        "silver",
        "silver.silver_customer_billing",
        "billing_cycle_start,billing_cycle_end",
        "billing_cycle_start must be <= billing_cycle_end",
        """
        SELECT COUNT(*) FROM silver.silver_customer_billing
        WHERE billing_cycle_start > billing_cycle_end
        """,
    ),
    (
        "DQ-07",
        "silver",
        "silver.silver_customer_billing",
        "resolution_time_minutes",
        "resolution_time_minutes must be >= 0, or NULL",
        """
        SELECT COUNT(*) FROM silver.silver_customer_billing
        WHERE resolution_time_minutes IS NOT NULL AND resolution_time_minutes < 0
        """,
    ),
    (
        "DQ-08",
        "gold",
        "gold.fact_billing_support",
        None,
        "fact row count must equal silver row count",
        """
        SELECT ABS(
            (SELECT COUNT(*) FROM gold.fact_billing_support)
          - (SELECT COUNT(*) FROM silver.silver_customer_billing)
        )
        """,
    ),
    (
        "DQ-09",
        "gold",
        "gold.fact_billing_support",
        None,
        "fact must have zero orphan foreign keys (all 8 dimensions)",
        """
        SELECT (
            (SELECT COUNT(*) FROM gold.fact_billing_support f
              WHERE NOT EXISTS (SELECT 1 FROM gold.dim_customer d
                                 WHERE d.customer_key = f.customer_key))
          + (SELECT COUNT(*) FROM gold.fact_billing_support f
              WHERE NOT EXISTS (SELECT 1 FROM gold.dim_date d
                                 WHERE d.date_key = f.billing_start_date_key))
          + (SELECT COUNT(*) FROM gold.fact_billing_support f
              WHERE NOT EXISTS (SELECT 1 FROM gold.dim_date d
                                 WHERE d.date_key = f.billing_end_date_key))
          + (SELECT COUNT(*) FROM gold.fact_billing_support f
              WHERE NOT EXISTS (SELECT 1 FROM gold.dim_date d
                                 WHERE d.date_key = f.payment_due_date_key))
          + (SELECT COUNT(*) FROM gold.fact_billing_support f
              WHERE NOT EXISTS (SELECT 1 FROM gold.dim_date d
                                 WHERE d.date_key = f.last_interaction_date_key))
          + (SELECT COUNT(*) FROM gold.fact_billing_support f
              WHERE NOT EXISTS (SELECT 1 FROM gold.dim_payment_status d
                                 WHERE d.payment_status_key = f.payment_status_key))
          + (SELECT COUNT(*) FROM gold.fact_billing_support f
              WHERE NOT EXISTS (SELECT 1 FROM gold.dim_issue_type d
                                 WHERE d.issue_type_key = f.issue_type_key))
          + (SELECT COUNT(*) FROM gold.fact_billing_support f
              WHERE NOT EXISTS (SELECT 1 FROM gold.dim_support_channel d
                                 WHERE d.support_channel_key = f.channel_key))
        )
        """,
    ),
    (
        "DQ-10",
        "gold",
        "gold.dim_date",
        None,
        "dim_date must contain more than the unknown member (calendar generated)",
        """
        SELECT CASE WHEN COUNT(*) > 1 THEN 0 ELSE 1 END
        FROM gold.dim_date
        """,
    ),
    (
        "DQ-11",
        "gold",
        "gold.fact_billing_support",
        "issue_type_key",
        "no fact row may reference the unknown issue_type (key 0)",
        """
        SELECT COUNT(*) FROM gold.fact_billing_support WHERE issue_type_key = 0
        """,
    ),
    (
        "DQ-12",
        "silver",
        "silver.silver_customer_billing",
        "support_interaction_count,primary_issue_type",
        "no-interaction rows must equal NULL issue_type rows (18,719 expected)",
        """
        SELECT ABS(
            (SELECT COUNT(*) FROM silver.silver_customer_billing
              WHERE support_interaction_count = 0)
          - (SELECT COUNT(*) FROM silver.silver_customer_billing
              WHERE primary_issue_type IS NULL)
        )
        """,
    ),
]


def main() -> int:
    engine = create_engine(config.DATABASE_URL)
    run_ts = datetime.now()

    results: list[tuple[str, str, str, str | None, int, str, str]] = []

    with engine.begin() as conn:
        for rule_id, layer, table, column, description, sql in RULES:
            try:
                failed = int(conn.execute(text(sql)).scalar_one())
                status = "PASS" if failed == 0 else "FAIL"
            except Exception as exc:  # noqa: BLE001
                failed = -1
                status = "FAIL"
                description = f"{description} [ERROR: {type(exc).__name__}: {exc}]"

            results.append((rule_id, layer, table, column, failed, status, description))

        # Replace this run's rows so re-running does not accumulate duplicates
        conn.execute(
            text("DELETE FROM gold.dq_results WHERE run_ts >= :ts"),
            {"ts": run_ts},
        )
        for rule_id, layer, table, column, failed, status, _desc in results:
            conn.execute(
                text(
                    """
                    INSERT INTO gold.dq_results
                        (rule_id, layer, table_name, column_name, failed_rows, status, run_ts)
                    VALUES (:rule_id, :layer, :table_name, :column_name,
                            :failed_rows, :status, :run_ts)
                    """
                ),
                {
                    "rule_id": rule_id,
                    "layer": layer,
                    "table_name": table,
                    "column_name": column,
                    "failed_rows": failed,
                    "status": status,
                    "run_ts": run_ts,
                },
            )

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    print("=" * 96)
    print(f"DATA QUALITY REPORT   run_ts={run_ts:%Y-%m-%d %H:%M:%S}")
    print("=" * 96)
    print(f"{'RULE':7s} {'LAYER':7s} {'FAILED':>8s}  {'STATUS':6s}  DESCRIPTION")
    print("-" * 96)
    for rule_id, layer, _table, _column, failed, status, description in results:
        marker = "OK  " if status == "PASS" else "FAIL"
        print(f"{rule_id:7s} {layer:7s} {failed:>8,}  {marker:6s}  {description}")

    n_pass = sum(1 for r in results if r[5] == "PASS")
    n_total = len(results)
    print("-" * 96)
    print(f"RESULT: {n_pass}/{n_total} rules passed")

    if n_pass != n_total:
        print("\nFAILED RULES:")
        for rule_id, _l, _t, _c, failed, status, description in results:
            if status != "PASS":
                print(f"  {rule_id}: {failed:,} failing rows - {description}")
        return 1

    print("All data-quality rules passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
