#!/usr/bin/env python3
"""
Profile the raw CSV BEFORE any transform SQL is written.

This is the mandatory first step of the project: the column names, date
formats, boolean value sets and null patterns in the real file decide how
01_bronze_to_silver.sql must be written. Nothing here transforms data — it
only reports.

Output: data/profile.txt (and the same content on stdout).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

# Columns that are expected to hold dates / booleans / numerics. Used only to
# organise the report; the actual detection is data-driven.
DATE_HINTS = ("date", "cycle_start", "cycle_end")
BOOL_HINTS = ("flag", "churned")


def _section(title: str) -> str:
    return f"\n{'=' * 78}\n{title}\n{'=' * 78}\n"


def build_report(df: pd.DataFrame, source: Path) -> str:
    out: list[str] = []
    out.append(_section("SOURCE"))
    out.append(f"file      : {source}\n")
    out.append(f"size      : {source.stat().st_size / 1024 / 1024:.2f} MB\n")
    out.append(f"shape     : {df.shape[0]:,} rows x {df.shape[1]} columns\n")

    out.append(_section("COLUMNS (lowercased, as they will land in bronze)"))
    out.append("\n".join(f"  {i:2d}. {c}" for i, c in enumerate(df.columns)) + "\n")

    out.append(_section("DTYPES (pandas inference)"))
    out.append(df.dtypes.to_string() + "\n")

    out.append(_section("NULL COUNTS"))
    nulls = df.isna().sum()
    null_pct = (nulls / len(df) * 100).round(2)
    null_report = pd.DataFrame({"nulls": nulls, "pct": null_pct})
    out.append(null_report.to_string() + "\n")

    out.append(_section("CARDINALITY (nunique, nulls excluded)"))
    out.append(df.nunique().to_string() + "\n")

    out.append(_section("CATEGORICAL VALUE SETS (object columns, first 20 values)"))
    for col in df.select_dtypes(include="object").columns:
        values = df[col].dropna().unique()
        preview = ", ".join(repr(v) for v in values[:20])
        out.append(f"\n{col}  (n_unique={len(values)})\n  {preview}\n")

    out.append(_section("BOOLEAN-LIKE COLUMNS (raw value sets)"))
    for col in df.columns:
        if any(h in col for h in BOOL_HINTS):
            counts = df[col].value_counts(dropna=False).to_dict()
            out.append(f"\n{col}\n  {counts}\n")

    out.append(_section("DATE-LIKE COLUMNS (min / max / nulls)"))
    for col in df.columns:
        if any(h in col for h in DATE_HINTS):
            parsed = pd.to_datetime(df[col], errors="coerce")
            bad = int(parsed.isna().sum() - df[col].isna().sum())
            out.append(
                f"\n{col}\n"
                f"  min   : {parsed.min()}\n"
                f"  max   : {parsed.max()}\n"
                f"  nulls : {int(df[col].isna().sum())}\n"
                f"  unparseable (non-null but invalid): {bad}\n"
            )

    out.append(_section("NUMERIC RANGES"))
    numeric = df.select_dtypes(include="number")
    if not numeric.empty:
        out.append(numeric.describe().T.to_string() + "\n")

    out.append(_section("GRAIN CHECK: duplicates on (customer_id, billing_cycle_start)"))
    key = ["customer_id", "billing_cycle_start"]
    if all(k in df.columns for k in key):
        dup_mask = df.duplicated(subset=key, keep=False)
        n_dup_rows = int(dup_mask.sum())
        n_dup_keys = int(df.loc[dup_mask, key].drop_duplicates().shape[0])
        out.append(f"  duplicate rows      : {n_dup_rows:,}\n")
        out.append(f"  duplicate key combos: {n_dup_keys:,}\n")
        out.append(f"  unique key combos   : {df[key].drop_duplicates().shape[0]:,}\n")
        if n_dup_rows:
            out.append("\n  sample duplicates:\n")
            out.append(df.loc[dup_mask, key].head(10).to_string() + "\n")
    else:
        out.append(f"  SKIPPED - missing one of {key}\n")

    out.append(_section("CROSS-CHECK: support interaction vs issue type"))
    if {"support_interaction_count", "primary_issue_type"} <= set(df.columns):
        no_interaction = int((df["support_interaction_count"] == 0).sum())
        no_issue = int(df["primary_issue_type"].isna().sum())
        out.append(f"  rows with support_interaction_count == 0 : {no_interaction:,}\n")
        out.append(f"  rows with primary_issue_type IS NULL     : {no_issue:,}\n")
        out.append(f"  consistent (equal)                       : {no_interaction == no_issue}\n")

    out.append(_section("HEAD (3 rows)"))
    out.append(df.head(3).to_string() + "\n")

    return "".join(out)


def main() -> int:
    if not config.DATA_PATH.exists():
        print(
            f"[error] {config.DATA_PATH} not found. Run `make download` first.",
            file=sys.stderr,
        )
        return 1

    df = pd.read_csv(config.DATA_PATH, na_values=config.NA_VALUES)
    df.columns = config.normalize_columns(df.columns)

    report = build_report(df, config.DATA_PATH)
    print(report)

    config.PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PROFILE_PATH.write_text(report, encoding="utf-8")
    print(f"\n[ok] report written to {config.PROFILE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
