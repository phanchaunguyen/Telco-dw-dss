#!/usr/bin/env python3
"""
Phase G — Export marts, dimensions and a denormalized fact to CSV + Parquet.

The exports are what you feed into Looker Studio / Google Sheets, so the
denormalized fact joins every foreign key back to its readable name.

Output: exports/<name>.csv and exports/<name>.parquet
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

# (export_name, SQL)
EXPORTS: list[tuple[str, str]] = [
    # --- data marts -------------------------------------------------------
    ("mart_billing_monthly", "SELECT * FROM gold.mart_billing_monthly ORDER BY year, month"),
    (
        "mart_support_monthly",
        "SELECT * FROM gold.mart_support_monthly ORDER BY year, month, support_channel, issue_type",
    ),
    (
        "mart_churn_summary",
        "SELECT * FROM gold.mart_churn_summary ORDER BY payment_status, issue_type, support_channel",
    ),
    # --- materialized views ----------------------------------------------
    ("mv_billing_monthly", "SELECT * FROM gold.mv_billing_monthly ORDER BY year, quarter, month"),
    (
        "mv_churn_by_issue_channel",
        "SELECT * FROM gold.mv_churn_by_issue_channel ORDER BY issue_type, support_channel",
    ),
    # --- dimensions -------------------------------------------------------
    ("dim_customer", "SELECT * FROM gold.dim_customer ORDER BY customer_key"),
    ("dim_date", "SELECT * FROM gold.dim_date ORDER BY date_key"),
    ("dim_payment_status", "SELECT * FROM gold.dim_payment_status ORDER BY payment_status_key"),
    ("dim_issue_type", "SELECT * FROM gold.dim_issue_type ORDER BY issue_type_key"),
    ("dim_support_channel", "SELECT * FROM gold.dim_support_channel ORDER BY support_channel_key"),
    # --- denormalized fact ------------------------------------------------
    (
        "fact_billing_support_denormalized",
        """
        SELECT
            f.fact_key,
            c.customer_id,
            ds.full_date            AS billing_cycle_start,
            de.full_date            AS billing_cycle_end,
            dp.full_date            AS payment_due_date,
            dl.full_date            AS last_interaction_date,
            dq.year,
            dq.quarter,
            dq.month,
            ps.payment_status,
            it.issue_type,
            ch.support_channel,
            f.escalation_flag,
            f.churn_flag,
            f.call_minutes,
            f.data_usage_gb,
            f.sms_count,
            f.internet_sessions,
            f.total_bill_amount,
            f.late_fee,
            f.discounts_applied,
            f.support_interaction_count,
            f.resolution_time_minutes,
            f.satisfaction_score
        FROM gold.fact_billing_support f
        JOIN gold.dim_customer        c  ON c.customer_key        = f.customer_key
        JOIN gold.dim_date            ds ON ds.date_key           = f.billing_start_date_key
        JOIN gold.dim_date            de ON de.date_key           = f.billing_end_date_key
        JOIN gold.dim_date            dp ON dp.date_key           = f.payment_due_date_key
        JOIN gold.dim_date            dl ON dl.date_key           = f.last_interaction_date_key
        JOIN gold.dim_date            dq ON dq.date_key           = f.billing_start_date_key
        JOIN gold.dim_payment_status  ps ON ps.payment_status_key = f.payment_status_key
        JOIN gold.dim_issue_type      it ON it.issue_type_key     = f.issue_type_key
        JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
        ORDER BY ds.full_date, c.customer_id
        """,
    ),
]


def main() -> int:
    config.ensure_dirs()
    engine = create_engine(config.DATABASE_URL)

    print("=" * 70)
    print(f"EXPORTING TO {config.EXPORT_DIR}")
    print("=" * 70)

    total_rows = 0
    for name, sql in EXPORTS:
        try:
            with engine.connect() as conn:
                df = pd.read_sql(text(sql), conn)
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] {name}: {exc}", file=sys.stderr)
            return 1

        csv_path = config.EXPORT_DIR / f"{name}.csv"
        parquet_path = config.EXPORT_DIR / f"{name}.parquet"

        df.to_csv(csv_path, index=False)
        df.to_parquet(parquet_path, index=False, compression="snappy")

        total_rows += len(df)
        print(
            f"  {name:38s} {len(df):>8,} rows x {len(df.columns):>2} cols"
            f"  -> .csv + .parquet"
        )

    print("-" * 70)
    print(f"  {len(EXPORTS)} datasets, {total_rows:,} rows total")
    print(f"  files written to {config.EXPORT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
