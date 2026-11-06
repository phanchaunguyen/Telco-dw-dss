#!/usr/bin/env python3
"""
Download the telco churn dataset from Hugging Face into data/raw/.

Idempotent: if the destination file already exists and is non-empty, the
download is skipped. This keeps `make all` fast on repeated runs and means
`make reset` (which only removes Docker volumes) never forces a re-download.

Usage:
    python scripts/download_dataset.py [--force]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Make `src` importable when this script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from huggingface_hub import hf_hub_download  # noqa: E402

from src import config  # noqa: E402


def download(force: bool = False) -> Path:
    """Fetch the CSV into data/raw/telco_churn.csv and return its path."""
    config.ensure_dirs()
    dest = config.DATA_PATH

    if dest.exists() and dest.stat().st_size > 0 and not force:
        size_mb = dest.stat().st_size / 1024 / 1024
        print(f"[skip] {dest} already exists ({size_mb:.1f} MB). Use --force to re-download.")
        return dest

    print(f"[hf] repo_id  : {config.HF_REPO_ID}")
    print(f"[hf] filename : {config.HF_FILENAME}")

    cached_path = hf_hub_download(
        repo_id=config.HF_REPO_ID,
        filename=config.HF_FILENAME,
        repo_type="dataset",
    )
    print(f"[hf] cached at: {cached_path}")

    shutil.copyfile(cached_path, dest)
    size_mb = dest.stat().st_size / 1024 / 1024
    print(f"[ok] copied to {dest} ({size_mb:.1f} MB)")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if data/raw/telco_churn.csv already exists.",
    )
    args = parser.parse_args()

    try:
        download(force=args.force)
    except Exception as exc:  # noqa: BLE001 - surface a clean message
        print(f"[error] download failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
