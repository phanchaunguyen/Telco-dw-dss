#!/usr/bin/env python3
"""
Phase F — Build data marts and materialized views.

Runs sql/03_marts.sql then sql/04_materialized_views.sql. No transformation
logic lives here; the SQL files own everything.

Note: REFRESH MATERIALIZED VIEW CONCURRENTLY cannot run inside a transaction
block, so the SQL files use plain REFRESH. The CONCURRENTLY variant is only
used by `make refresh-mv`, which runs through psql in autocommit mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

SQL_FILES = [
    "03_marts.sql",
    "04_materialized_views.sql",
]


def run_file(engine, filename: str) -> None:
    path = config.SQL_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"SQL file not found: {path}")

    sql = path.read_text(encoding="utf-8")
    print(f"\n[run] {filename}")

    # Use the raw DBAPI connection: psycopg2 only performs %-interpolation when
    # arguments are supplied, so literal '%' characters inside SQL comments or
    # expressions are passed through untouched. SQLAlchemy's exec_driver_sql
    # would try to interpret them as bind parameters.
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(sql)
        raw.commit()
        cur.close()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    print(f"[ok ] {filename}")


def report(engine) -> None:
    objects = [
        ("view", "gold.mart_billing_monthly"),
        ("view", "gold.mart_support_monthly"),
        ("view", "gold.mart_churn_summary"),
        ("matview", "gold.mv_billing_monthly"),
        ("matview", "gold.mv_churn_by_issue_channel"),
    ]
    print("\n" + "=" * 60)
    print("MARTS")
    print("=" * 60)
    with engine.connect() as conn:
        for kind, name in objects:
            try:
                rows = conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one()
                print(f"  {kind:8s} {name:38s} {rows:>6,} rows")
            except Exception as exc:  # noqa: BLE001
                print(f"  {kind:8s} {name:38s} ERROR {type(exc).__name__}")


def main() -> int:
    engine = create_engine(config.DATABASE_URL)
    try:
        for filename in SQL_FILES:
            run_file(engine, filename)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[error] {exc}", file=sys.stderr)
        return 1

    report(engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
