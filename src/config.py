"""
Central configuration for the Medallion DW pipeline.

Everything that varies between runs (ports, credentials, dataset coordinates)
comes from the environment / `.env`. Nothing here is a hardcoded absolute path:
all paths are resolved relative to the repository root, so the project can be
cloned anywhere.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# config.py lives in <root>/src/, so the repository root is one level up.
ROOT_DIR: Path = Path(__file__).resolve().parent.parent

load_dotenv(ROOT_DIR / ".env")

DATA_DIR: Path = ROOT_DIR / "data"
RAW_DIR: Path = DATA_DIR / "raw"
DATA_PATH: Path = RAW_DIR / "telco_churn.csv"
PROFILE_PATH: Path = DATA_DIR / "profile.txt"

EXPORT_DIR: Path = ROOT_DIR / "exports"
SQL_DIR: Path = ROOT_DIR / "sql"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_USER: str = os.getenv("DB_USER", "dw")
DB_PASSWORD: str = os.getenv("DB_PASSWORD", "dw")
DB_NAME: str = os.getenv("DB_NAME", "warehouse")
DB_HOST: str = os.getenv("DB_HOST", "localhost")
DB_PORT: str = os.getenv("DB_PORT", "5432")

DATABASE_URL: str = (
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------
HF_REPO_ID: str = os.getenv("HF_REPO_ID", "Omer1234567/telco-churn-synthetic")
HF_FILENAME: str = os.getenv(
    "HF_FILENAME", "synthetic_customer_data_evenly_distributed.csv"
)

# Values that pandas must treat as NULL. The raw CSV uses empty strings for
# missing support interactions; the rest are defensive.
NA_VALUES: list[str] = ["", "NA", "N/A", "null", "NULL", "nan", "NaN"]

# ---------------------------------------------------------------------------
# Batch identity
# ---------------------------------------------------------------------------
# Microsecond precision: two runs started within the same second must not
# collide, otherwise the "process only the latest batch" filter in
# 01_bronze_to_silver.sql would silently process both.
BATCH_ID: str = datetime.now().strftime("%Y%m%dT%H%M%S%f")

# ---------------------------------------------------------------------------
# Bronze table coordinates
# ---------------------------------------------------------------------------
BRONZE_SCHEMA: str = "bronze"
BRONZE_TABLE: str = "bronze_customer_billing"
SILVER_SCHEMA: str = "silver"
SILVER_TABLE: str = "silver_customer_billing"
GOLD_SCHEMA: str = "gold"


def ensure_dirs() -> None:
    """Create the directories the pipeline writes into."""
    for directory in (RAW_DIR, EXPORT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Column-name normalisation
# ---------------------------------------------------------------------------
# The raw CSV uses CamelCase WITHOUT underscores (CustomerID, BillingCycleStart,
# DataUsageGB, ...). Lowercasing alone would produce `customerid` and
# `billingcyclestart`, which do not match the snake_case names used throughout
# the SQL. So Bronze converts CamelCase -> snake_case once, and every layer
# downstream sees `customer_id`, `billing_cycle_start`, `data_usage_gb`, ...
#
# Acronyms are kept together: `DataUsageGB` -> `data_usage_gb` (not `data_usage_g_b`).
_ACRONYM_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")


def to_snake_case(name: str) -> str:
    """Convert a CamelCase / PascalCase column name to snake_case."""
    s = name.strip()
    s = _ACRONYM_RE.sub(r"\1_\2", s)
    s = _CAMEL_RE.sub(r"\1_\2", s)
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_").lower()


def normalize_columns(columns) -> list[str]:
    """Apply `to_snake_case` to every column name."""
    return [to_snake_case(c) for c in columns]


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    print(f"ROOT_DIR     : {ROOT_DIR}")
    print(f"DATA_PATH    : {DATA_PATH}")
    print(f"EXPORT_DIR   : {EXPORT_DIR}")
    print(f"SQL_DIR      : {SQL_DIR}")
    print(f"DATABASE_URL : {DATABASE_URL}")
    print(f"HF_REPO_ID   : {HF_REPO_ID}")
    print(f"HF_FILENAME  : {HF_FILENAME}")
    print(f"BATCH_ID     : {BATCH_ID}")
