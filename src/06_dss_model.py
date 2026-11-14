#!/usr/bin/env python3
"""
Phase H — DSS model base: churn hazard model + rule scorecard + backtest.

Reads gold.dss_cycle_features (sql/06_dss_features.sql), then:

  1. splits by TIME, not at random: cycles starting before SPLIT_DATE train
     the model, later cycles test it. A random split would let the model see
     February while being graded on January.
  2. fits a logistic regression (discrete-time hazard model) and the
     hand-written 2-tier rule scorecard from docs/story, and grades both,
     plus a single-flag baseline ("contacted support"), on the test period.
  3. scores every cycle and writes the results back into gold so that SQL,
     Metabase and the retention planner (sql/07_dss_decision.sql) can use them.
  4. replays the targeting policy month by month on the test period to show
     what each strategy would have caught for a given budget.

Tables written (all replaced on every run):
    gold.dss_churn_score      1 row per cycle: p_churn, rule_score, risk_band
    gold.dss_model_coef       1 row per feature: standardized coef, odds ratio
    gold.dss_model_eval       1 row per model: AUC, Brier, lift@10%, recall@10%
    gold.dss_policy_backtest  1 row per (strategy, budget share)
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

# Jan-Mar 2020 train (64,380 cycles), Apr 2020 onwards test (48,172 cycles).
SPLIT_DATE = date(2020, 4, 1)
RANDOM_STATE = 42

NUMERIC = [
    "bill", "late_fee", "discount",
    "call_minutes", "data_gb", "sms", "sessions",
    "contacts", "csat_centered", "resolution_min",
    "tenure_cycle", "prior_unpaid_cycles", "prior_escalations", "prior_contacts",
]
FLAGS = ["is_unpaid", "is_partial", "contacted", "csat_low", "escalated"]
# One-hot with an explicit reference level ('No Support' / 'None'), so the
# coefficients read as "compared with a cycle without any support contact".
ISSUES = ["Billing", "Technical", "Service Quality"]
CHANNELS = ["Email", "Phone", "Chat"]

# Risk bands on the predicted hazard. The base rate is ~24.7%, so 'Low' is
# "safer than average" and 'High' is "about 1.6x average or worse".
BANDS = [(0.40, "High"), (0.25, "Medium"), (0.0, "Low")]


def load_features(engine) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM gold.dss_cycle_features"), conn)
    df["cycle_start"] = pd.to_datetime(df["cycle_start"])
    return df


def design_matrix(df: pd.DataFrame) -> pd.DataFrame:
    X = df[NUMERIC].astype(float).copy()
    for col in FLAGS:
        X[col] = df[col].astype(float)
    for v in ISSUES:
        X[f"issue_{v.lower().replace(' ', '_')}"] = (df["issue_type"] == v).astype(float)
    for v in CHANNELS:
        X[f"channel_{v.lower()}"] = (df["support_channel"] == v).astype(float)
    return X


def rule_score(df: pd.DataFrame) -> pd.Series:
    """
    The 2-tier rule from docs/story/presentation.md, slide 16:
        3 x escalated + 3 x unpaid + 2 x (CSAT <= 2) + 1 x (contacts >= 3)
    Tier 1 (contacted) is folded in by giving silent cycles a score of 0.
    """
    score = (
        3 * df["escalated"].astype(int)
        + 3 * df["is_unpaid"].astype(int)
        + 2 * df["csat_low"].astype(int)
        + 1 * (df["contacts"] >= 3).astype(int)
    )
    return score.where(df["contacted"], 0)


def lift_at(y: np.ndarray, score: np.ndarray, share: float) -> tuple[float, float]:
    """(lift, recall) when targeting the top `share` of rows by score."""
    n = max(1, int(round(len(y) * share)))
    # Stable tie-break by a fixed random key, so coarse integer scores (the
    # rule scorecard) are not graded on the input order.
    rng = np.random.default_rng(RANDOM_STATE)
    order = np.lexsort((rng.random(len(y)), -score))
    top = y[order[:n]]
    lift = top.mean() / y.mean()
    recall = top.sum() / y.sum()
    return float(lift), float(recall)


def evaluate(name: str, y: np.ndarray, score: np.ndarray, proba: bool) -> dict:
    lift10, recall10 = lift_at(y, score, 0.10)
    lift20, recall20 = lift_at(y, score, 0.20)
    return {
        "model": name,
        "auc": round(roc_auc_score(y, score), 4),
        "brier": round(brier_score_loss(y, score), 4) if proba else None,
        "lift_top10": round(lift10, 3),
        "recall_top10": round(recall10, 4),
        "lift_top20": round(lift20, 3),
        "recall_top20": round(recall20, 4),
        "test_rows": int(len(y)),
        "test_churn_rate": round(float(y.mean()), 4),
    }


def backtest(test: pd.DataFrame) -> pd.DataFrame:
    """
    For each month in the test period and each budget share, pick the top
    k% of active customers by each strategy and count the churners caught.
    Random targeting is the expected value (k% of churners), not a draw.
    """
    rows = []
    for share in (0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50):
        caught = {"model": 0, "rule": 0, "random": 0.0}
        targeted = 0
        churners = 0
        for _, month in test.groupby(test["cycle_start"].dt.to_period("M")):
            y = month["churn_flag"].to_numpy(dtype=int)
            k = int(round(len(month) * share))
            if k == 0:
                continue
            for strat, col in (("model", "p_churn"), ("rule", "rule_score")):
                rng = np.random.default_rng(RANDOM_STATE)
                order = np.lexsort((rng.random(len(y)), -month[col].to_numpy()))
                caught[strat] += int(y[order[:k]].sum())
            caught["random"] += y.sum() * k / len(y)
            targeted += k
            churners += int(y.sum())
        for strat, c in caught.items():
            rows.append(
                {
                    "strategy": strat,
                    "budget_share": share,
                    "targeted": targeted,
                    "churners_total": churners,
                    "churners_caught": round(float(c), 1),
                    "recall": round(float(c) / churners, 4),
                    "precision": round(float(c) / targeted, 4),
                }
            )
    return pd.DataFrame(rows)


def main() -> int:
    engine = create_engine(config.DATABASE_URL)
    df = load_features(engine)
    print(f"[read] gold.dss_cycle_features: {len(df):,} rows")

    X = design_matrix(df)
    y = df["churn_flag"].astype(int).to_numpy()
    is_train = (df["cycle_start"] < pd.Timestamp(SPLIT_DATE)).to_numpy()
    print(f"[split] train {is_train.sum():,} rows (< {SPLIT_DATE}), "
          f"test {(~is_train).sum():,} rows")

    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=2000, C=1.0, random_state=RANDOM_STATE),
    )
    model.fit(X[is_train], y[is_train])

    df["p_churn"] = model.predict_proba(X)[:, 1]
    df["rule_score"] = rule_score(df)
    df["split"] = np.where(is_train, "train", "test")
    df["risk_band"] = "Low"
    for cut, band in reversed(BANDS):
        df.loc[df["p_churn"] >= cut, "risk_band"] = band

    # ---- evaluation (test period only) -----------------------------------
    test = df[~is_train]
    yt = test["churn_flag"].astype(int).to_numpy()
    evals = pd.DataFrame(
        [
            evaluate("logistic_hazard", yt, test["p_churn"].to_numpy(), proba=True),
            evaluate("rule_scorecard", yt, test["rule_score"].to_numpy(float), proba=False),
            evaluate("contacted_only", yt, test["contacted"].to_numpy(float), proba=False),
        ]
    )
    evals["trained_at"] = datetime.now()
    evals["split_date"] = SPLIT_DATE

    lr = model.named_steps["logisticregression"]
    coef = pd.DataFrame(
        {
            "feature": X.columns,
            "coef_std": lr.coef_[0].round(4),
            "odds_ratio_per_sd": np.exp(lr.coef_[0]).round(4),
        }
    ).sort_values("coef_std", key=np.abs, ascending=False)

    bt = backtest(test)

    # ---- write back to gold ----------------------------------------------
    scores = df[
        ["fact_key", "customer_key", "billing_start_date_key", "cycle_start",
         "p_churn", "rule_score", "risk_band", "split", "churn_flag"]
    ].copy()
    scores["p_churn"] = scores["p_churn"].round(5)
    scores["cycle_start"] = scores["cycle_start"].dt.date

    with engine.begin() as conn:
        for name, frame in (
            ("dss_churn_score", scores),
            ("dss_model_coef", coef),
            ("dss_model_eval", evals),
            ("dss_policy_backtest", bt),
        ):
            conn.execute(text(f"DROP TABLE IF EXISTS gold.{name} CASCADE"))
            frame.to_sql(name, conn, schema="gold", index=False,
                         method="multi", chunksize=5000)
        conn.execute(text("ALTER TABLE gold.dss_churn_score ADD PRIMARY KEY (fact_key)"))
        conn.execute(text(
            "CREATE INDEX ix_dss_score_cycle ON gold.dss_churn_score (cycle_start)"))

    # ---- report ----------------------------------------------------------
    pd.set_option("display.width", 140)
    print("\n" + "=" * 70)
    print("MODEL EVALUATION (test period)")
    print("=" * 70)
    print(evals.drop(columns=["trained_at", "split_date"]).to_string(index=False))
    print("\nTOP COEFFICIENTS (standardized)")
    print(coef.head(12).to_string(index=False))
    print("\nRISK BANDS (test period)")
    print(
        test.groupby("risk_band")
        .agg(cycles=("churn_flag", "size"), churn_rate=("churn_flag", "mean"))
        .round(4)
        .to_string()
    )
    print("\n[ok] wrote gold.dss_churn_score, dss_model_coef, dss_model_eval, "
          "dss_policy_backtest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
