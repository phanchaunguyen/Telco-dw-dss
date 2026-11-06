#!/usr/bin/env python3
"""
Phase C — Bronze load: Extract + Load only.

Rules enforced here (and nowhere else):
  * Bronze is APPEND-ONLY. `if_exists="append"`, never "replace".
  * Every column is TEXT. No casting, no cleaning, no renaming beyond
    CamelCase -> snake_case (needed so downstream SQL can reference
    `customer_id` instead of `customerid`).
  * Three audit columns are added: ingest_batch_id, ingest_ts, source_file.

All transformation happens later, in SQL, inside PostgreSQL.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402


def load_bronze() -> int:
    if not config.DATA_PATH.exists():
        raise FileNotFoundError(
            f"{config.DATA_PATH} not found. Run `make download` first."
        )

    print(f"[read] {config.DATA_PATH}")
    df = pd.read_csv(config.DATA_PATH, na_values=config.NA_VALUES)
    print(f"[read] {len(df):,} rows x {len(df.columns)} columns")

    # CamelCase -> snake_case so downstream SQL can use customer_id, etc.
    df.columns = config.normalize_columns(df.columns)

    # ---------------------------------------------------------------------
    # Cast EVERY column to text.
    #
    # `astype(str)` would turn NaN into the literal string 'nan', which then
    # survives NULLIF(TRIM(col),'') in the Silver transform and creates a
    # bogus 'nan' dimension member. `astype("string")` is pandas' nullable
    # StringDtype: real missing values stay <NA> and are written as SQL NULL.
    # ---------------------------------------------------------------------
    df = df.astype("string")

    # Audit columns
    df["ingest_batch_id"] = config.BATCH_ID
    df["ingest_ts"] = datetime.now()
    df["source_file"] = config.DATA_PATH.name

    engine = create_engine(config.DATABASE_URL)

    print(f"[load] -> {config.BRONZE_SCHEMA}.{config.BRONZE_TABLE} (append)")
    print(f"[load] batch_id = {config.BATCH_ID}")

    # method="multi" batches rows into multi-row INSERTs; without it pandas
    # issues one round-trip per row (~112k round-trips for this dataset).
    df.to_sql(
        name=config.BRONZE_TABLE,
        con=engine,
        schema=config.BRONZE_SCHEMA,
        if_exists="append",
        index=False,
        method="multi",
        chunksize=5000,
    )

    with engine.connect() as conn:
        total = conn.execute(
            text(f"SELECT COUNT(*) FROM {config.BRONZE_SCHEMA}.{config.BRONZE_TABLE}")
        ).scalar_one()
        in_batch = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {config.BRONZE_SCHEMA}.{config.BRONZE_TABLE} "
                "WHERE ingest_batch_id = :b"
            ),
            {"b": config.BATCH_ID},
        ).scalar_one()

    print(f"[ok] rows in this batch : {in_batch:,}")
    print(f"[ok] rows in bronze     : {total:,} (append-only, all batches kept)")
    return in_batch


def main() -> int:
    try:
        load_bronze()
    except Exception as exc:  # noqa: BLE001
        print(f"[error] bronze load failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
