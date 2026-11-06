#!/usr/bin/env python3
"""
Phase D/E — Execute the ELT SQL files in order.

This script contains NO transformation logic. It only:
  1. reads sql/01_bronze_to_silver.sql and sql/02_silver_to_gold.sql,
  2. substitutes the {{batch_id}} placeholder,
  3. executes them sequentially against PostgreSQL.

The SQL files own their own transactions (BEGIN/COMMIT), so a failure in the
middle of the Gold build rolls back that file only.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

SQL_FILES = [
    "01_bronze_to_silver.sql",
    "02_silver_to_gold.sql",
]


def run_file(engine, filename: str, batch_id: str) -> None:
    path = config.SQL_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"SQL file not found: {path}")

    sql = path.read_text(encoding="utf-8").replace("{{batch_id}}", batch_id)

    print(f"\n[run] {filename}")
    # exec_driver_sql sends the whole script as one multi-statement command,
    # which is what psql does. The file manages its own BEGIN/COMMIT.
    with engine.connect() as conn:
        conn.exec_driver_sql(sql)
        conn.commit()
    print(f"[ok ] {filename}")


def report(engine) -> None:
    checks = [
        ("silver.silver_customer_billing", "SELECT COUNT(*) FROM silver.silver_customer_billing"),
        (
            "silver NULL primary_issue_type",
            "SELECT COUNT(*) FROM silver.silver_customer_billing WHERE primary_issue_type IS NULL",
        ),
        ("gold.fact_billing_support", "SELECT COUNT(*) FROM gold.fact_billing_support"),
        ("gold.dim_customer", "SELECT COUNT(*) FROM gold.dim_customer"),
        ("gold.dim_date", "SELECT COUNT(*) FROM gold.dim_date"),
        ("gold.dim_payment_status", "SELECT COUNT(*) FROM gold.dim_payment_status"),
        ("gold.dim_issue_type", "SELECT COUNT(*) FROM gold.dim_issue_type"),
        ("gold.dim_support_channel", "SELECT COUNT(*) FROM gold.dim_support_channel"),
    ]
    print("\n" + "=" * 60)
    print("ROW COUNTS")
    print("=" * 60)
    with engine.connect() as conn:
        for label, sql in checks:
            try:
                value = conn.execute(text(sql)).scalar_one()
                print(f"  {label:38s} {value:>10,}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {label:38s} {'n/a':>10}  ({type(exc).__name__})")


def main() -> int:
    engine = create_engine(config.DATABASE_URL)
    print(f"batch_id = {config.BATCH_ID}")

    try:
        for filename in SQL_FILES:
            run_file(engine, filename, config.BATCH_ID)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[error] {exc}", file=sys.stderr)
        return 1

    report(engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
