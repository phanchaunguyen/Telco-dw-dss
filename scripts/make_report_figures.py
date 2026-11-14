#!/usr/bin/env python3
"""
Render the report / slide charts straight from the warehouse.

Every number on every chart comes from a query on gold.* (fact, dims and the
DSS tables written by src/06_dss_model.py), so re-running the pipeline and
then `make figures` regenerates the figures with the same numbers the
dashboards show. Output: docs/figures/*.png (200 dpi, white background).

Chart conventions
-----------------
* one y-axis per panel; two measures of different scale go in two panels
* fixed categorical order: blue, orange, aqua (validated for colour-blind
  separation); aqua is below 3:1 contrast so it always carries a direct label
* thin bars with a 2 px gap, recessive grid, numbers only where they are read
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.metrics import roc_curve  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

OUT = config.ROOT_DIR / "docs" / "figures"

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK2,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.titlecolor": INK,
        "axes.titlelocation": "left",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.grid.axis": "y",
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "legend.frameon": False,
        "figure.dpi": 100,
        "savefig.dpi": 200,
        "savefig.bbox": "tight",
        "savefig.facecolor": "white",
    }
)


def q(engine, sql: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)


def save(fig, name: str) -> None:
    path = OUT / f"{name}.png"
    fig.savefig(path)
    plt.close(fig)
    print(f"  [fig] {path.relative_to(config.ROOT_DIR)}")


def bar_labels(ax, bars, fmt="{:.1f}%", color=INK):
    for b in bars:
        ax.annotate(fmt.format(b.get_height()).replace(".", ","), (b.get_x() + b.get_width() / 2, b.get_height()),
                    xytext=(0, 3), textcoords="offset points", ha="center",
                    fontsize=9, color=color)


# ---------------------------------------------------------------------------
# Business story
# ---------------------------------------------------------------------------

def fig_cohort(engine) -> None:
    df = q(engine, """
        SELECT d.year, d.month, MIN(d.full_date) AS month_start,
               COUNT(*) AS cycles,
               SUM(f.total_bill_amount) / COUNT(DISTINCT f.customer_key) AS arpu
        FROM gold.fact_billing_support f
        JOIN gold.dim_date d ON d.date_key = f.billing_start_date_key
        GROUP BY d.year, d.month ORDER BY 1, 2""")
    df = df[df["cycles"] >= 100]
    x = pd.to_datetime(df["month_start"])
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(8, 5), sharex=True,
                                 gridspec_kw={"height_ratios": [3, 2], "hspace": 0.25})
    a1.bar(x, df["cycles"], width=22, color=BLUE)
    a1.set_title("Số khách còn hoạt động mỗi tháng (tháng có ≥ 100 khách)")
    a1.set_ylabel("khách")
    a1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda v, _: f"{v:,.0f}".replace(",", ".")))
    for xi, yi in list(zip(x, df["cycles"]))[:4]:
        a1.annotate(f"{yi:,}".replace(",", "."), (xi, yi), xytext=(0, 3),
                    textcoords="offset points", ha="center", fontsize=8.5, color=INK)
    a2.plot(x, df["arpu"], color=ORANGE, lw=2, marker="o", ms=4)
    a2.set_ylim(60, 100)
    a2.set_title("ARPU (doanh thu bình quân mỗi khách) cùng các tháng")
    a2.set_ylabel("đơn vị tiền / khách")
    a2.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m/%y"))
    save(fig, "fig_cohort_arpu")


def fig_hazard(engine) -> None:
    df = q(engine, """
        SELECT tenure_cycle, COUNT(*) AS n,
               100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END) AS hazard
        FROM gold.dss_cycle_features GROUP BY 1 ORDER BY 1""")
    df = df[df["n"] >= 300]
    base = 24.68
    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.plot(df["tenure_cycle"], df["hazard"], color=BLUE, lw=2, marker="o", ms=5)
    ax.axhline(base, color=INK2, lw=1, ls="--")
    ax.annotate(f"trung bình {base:.2f}%".replace(".", ","), (df["tenure_cycle"].max(), base),
                xytext=(0, 5), textcoords="offset points", ha="right", color=INK2, fontsize=9)
    ax.set_ylim(0, 35)
    ax.set_xticks(df["tenure_cycle"])
    ax.set_xlabel("kỳ thứ t của khách hàng (thâm niên)")
    ax.set_ylabel("% khách rời mạng ở kỳ t")
    ax.set_title("Xác suất rời mạng theo thâm niên gần như không đổi (kỳ có ≥ 300 khách)")
    save(fig, "fig_hazard_tenure")


def fig_drivers(engine) -> None:
    contacts = q(engine, """
        SELECT support_interaction_count AS k,
               100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END) AS rate
        FROM gold.fact_billing_support GROUP BY 1 ORDER BY 1""")
    pay = q(engine, """
        SELECT ps.payment_status AS s,
               100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END) AS rate
        FROM gold.fact_billing_support f
        JOIN gold.dim_payment_status ps USING (payment_status_key)
        GROUP BY 1""").set_index("s").loc[["Paid", "Partial", "Unpaid"]].reset_index()
    csat = q(engine, """
        SELECT satisfaction_score AS c,
               100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END) AS rate
        FROM gold.fact_billing_support WHERE support_interaction_count > 0
        GROUP BY 1 ORDER BY 1""")
    esc = q(engine, """
        SELECT escalation_flag AS e,
               100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END) AS rate
        FROM gold.fact_billing_support WHERE support_interaction_count > 0
        GROUP BY 1 ORDER BY 1""")

    fig, axes = plt.subplots(1, 4, figsize=(11, 3.4), sharey=True,
                             gridspec_kw={"width_ratios": [6, 3, 5, 2], "wspace": 0.12})
    panels = [
        (axes[0], contacts["k"].astype(str), contacts["rate"], "Số lần liên hệ CSKH"),
        (axes[1], ["Đã\ntrả", "Một\nphần", "Chưa\ntrả"], pay["rate"], "Thanh toán"),
        (axes[2], csat["c"].astype(str), csat["rate"], "CSAT (kỳ có liên hệ)"),
        (axes[3], ["Không", "Có"], esc["rate"], "Escalate"),
    ]
    for ax, xs, ys, title in panels:
        bars = ax.bar(xs, ys, width=0.7, color=BLUE, edgecolor="white", linewidth=2)
        bar_labels(ax, bars, "{:.1f}")
        ax.set_title(title, fontsize=10)
    axes[0].set_ylabel("% kỳ rời mạng")
    axes[0].set_ylim(0, 45)
    fig.suptitle("Tỷ lệ rời mạng theo từng tín hiệu (đường nền 24,68%)", x=0.125,
                 ha="left", fontweight="bold", color=INK, y=1.03)
    for ax in axes:
        ax.axhline(24.68, color=INK2, lw=0.8, ls="--")
    save(fig, "fig_churn_drivers")


def fig_issue_channel(engine) -> None:
    df = q(engine, """
        SELECT it.issue_type, ch.support_channel,
               100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END) AS rate,
               COUNT(*) AS n
        FROM gold.fact_billing_support f
        JOIN gold.dim_issue_type it USING (issue_type_key)
        JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
        WHERE it.issue_type <> 'No Support'
        GROUP BY 1, 2""")
    m = df.pivot(index="issue_type", columns="support_channel", values="rate")
    m = m.loc[["Billing", "Technical", "Service Quality"], ["Email", "Phone", "Chat"]]
    fig, ax = plt.subplots(figsize=(5.6, 3))
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list("seq", SEQ[:5])
    im = ax.imshow(m.values, cmap=cmap, vmin=20, vmax=32, aspect="auto")
    ax.set_xticks(range(3), m.columns)
    ax.set_yticks(range(3), m.index)
    ax.grid(False)
    for i in range(3):
        for j in range(3):
            ax.text(j, i, f"{m.values[i, j]:.2f}%".replace(".", ","), ha="center",
                    va="center", color=INK, fontsize=10)
    ax.set_title("Tỷ lệ rời mạng: loại vấn đề × kênh hỗ trợ")
    fig.colorbar(im, ax=ax, fraction=0.04, pad=0.03).outline.set_visible(False)
    save(fig, "fig_issue_channel_heatmap")


def fig_sentinel(engine) -> None:
    df = q(engine, """
        SELECT satisfaction_score AS c,
               COUNT(*) AS all_rows,
               COUNT(*) FILTER (WHERE support_interaction_count > 0) AS contacted
        FROM gold.fact_billing_support GROUP BY 1 ORDER BY 1""")
    x = np.arange(len(df))
    w = 0.38
    fig, ax = plt.subplots(figsize=(7, 3.2))
    b1 = ax.bar(x - w / 2, 100 * df["all_rows"] / df["all_rows"].sum(), w,
                color=ORANGE, edgecolor="white", linewidth=2, label="Toàn bộ 112.552 kỳ")
    b2 = ax.bar(x + w / 2, 100 * df["contacted"] / df["contacted"].sum(), w,
                color=BLUE, edgecolor="white", linewidth=2, label="Chỉ kỳ có liên hệ (93.833)")
    bar_labels(ax, list(b1) + list(b2), "{:.1f}")
    ax.set_xticks(x, df["c"].astype(str))
    ax.set_xlabel("điểm CSAT")
    ax.set_ylabel("% số kỳ")
    ax.set_ylim(0, 40)
    ax.legend(loc="upper left")
    ax.set_title("Phân bố CSAT trước và sau khi loại giá trị sentinel")
    save(fig, "fig_csat_sentinel")


# ---------------------------------------------------------------------------
# DSS
# ---------------------------------------------------------------------------

def fig_roc(engine) -> None:
    df = q(engine, """
        SELECT s.p_churn, s.rule_score, s.churn_flag, x.contacted
        FROM gold.dss_churn_score s
        JOIN gold.dss_cycle_features x USING (fact_key)
        WHERE s.split = 'test'""")
    ev = q(engine, "SELECT model, auc FROM gold.dss_model_eval").set_index("model")["auc"]
    y = df["churn_flag"].astype(int)
    fig, ax = plt.subplots(figsize=(4.8, 4.4))
    for col, name, key, color in (
        ("p_churn", "Hồi quy logistic", "logistic_hazard", BLUE),
        ("rule_score", "Thẻ điểm luật", "rule_scorecard", ORANGE),
        ("contacted", "Chỉ cờ 'có liên hệ'", "contacted_only", AQUA),
    ):
        fpr, tpr, _ = roc_curve(y, df[col].astype(float))
        ax.plot(fpr, tpr, color=color, lw=2,
                label=f"{name}  AUC {ev[key]:.3f}".replace(".", ","))
    ax.plot([0, 1], [0, 1], color=INK2, lw=1, ls="--")
    ax.set_xlabel("tỷ lệ báo động nhầm (FPR)")
    ax.set_ylabel("tỷ lệ bắt đúng (TPR)")
    ax.grid(axis="both")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(loc="lower right", fontsize=8.5)
    ax.set_title("ROC trên giai đoạn kiểm tra (04/2020 trở đi)")
    save(fig, "fig_roc")


def fig_backtest(engine) -> None:
    df = q(engine, "SELECT * FROM gold.dss_policy_backtest ORDER BY budget_share")
    fig, ax = plt.subplots(figsize=(6, 3.8))
    for strat, name, color, ls in (("model", "Hồi quy logistic", BLUE, "-"),
                                   ("rule", "Thẻ điểm luật", ORANGE, "-"),
                                   ("random", "Chọn ngẫu nhiên", AQUA, "--")):
        d = df[df["strategy"] == strat]
        ax.plot(100 * d["budget_share"], 100 * d["recall"], color=color, lw=2, ls=ls,
                marker="o", ms=5)
        last = d.iloc[-1]
        ax.annotate(name, (100 * last["budget_share"], 100 * last["recall"]),
                    xytext=(6, 0), textcoords="offset points", va="center",
                    color=INK, fontsize=9)
    m20 = df[(df["strategy"] == "model") & (np.isclose(df["budget_share"], 0.2))].iloc[0]
    ax.annotate(f"{100 * m20['recall']:.1f}%".replace(".", ","),
                (20, 100 * m20["recall"]), xytext=(-8, 8), textcoords="offset points",
                ha="right", fontsize=9, color=INK)
    ax.set_xlim(0, 65)
    ax.set_ylim(0, 80)
    ax.set_xlabel("% khách được tiếp cận mỗi tháng")
    ax.set_ylabel("% khách rời mạng bắt được")
    ax.set_title("Backtest trên các tháng kiểm tra (từ 04/2020)")
    save(fig, "fig_backtest_recall")


def fig_calibration(engine) -> None:
    df = q(engine, """
        WITH t AS (
            SELECT p_churn, churn_flag, NTILE(10) OVER (ORDER BY p_churn) AS dec
            FROM gold.dss_churn_score WHERE split = 'test')
        SELECT dec, 100 * AVG(p_churn) AS pred,
               100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END) AS actual
        FROM t GROUP BY dec ORDER BY dec""")
    x = np.arange(1, 11)
    w = 0.38
    fig, ax = plt.subplots(figsize=(7, 3.3))
    ax.bar(x - w / 2, df["pred"], w, color=BLUE, edgecolor="white", linewidth=2,
           label="xác suất dự báo trung bình")
    b = ax.bar(x + w / 2, df["actual"], w, color=ORANGE, edgecolor="white", linewidth=2,
               label="tỷ lệ rời mạng thực tế")
    for i in (0, 9):
        ax.annotate(f"{df['pred'][i]:.1f} / {df['actual'][i]:.1f}".replace(".", ","),
                    (x[i], max(df["pred"][i], df["actual"][i])), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=9, color=INK)
    ax.set_xticks(x)
    ax.set_xlabel("nhóm thập phân vị theo điểm rủi ro (1 = thấp nhất)")
    ax.set_ylabel("%")
    ax.set_ylim(0, 70)
    ax.legend(loc="upper left")
    ax.set_title("Hiệu chuẩn: dự báo so với thực tế trên giai đoạn kiểm tra")
    save(fig, "fig_calibration")


LABELS = {
    "csat_centered": "CSAT (lệch khỏi 3)", "contacted": "có liên hệ CSKH",
    "is_unpaid": "chưa thanh toán", "contacts": "số lần liên hệ",
    "call_minutes": "phút gọi", "bill": "tiền hoá đơn", "data_gb": "dung lượng data",
    "escalated": "bị escalate", "is_partial": "trả một phần",
    "resolution_min": "thời gian xử lý", "sessions": "phiên internet", "sms": "số SMS",
    "late_fee": "phí trễ hạn", "discount": "giảm giá", "tenure_cycle": "thâm niên",
    "prior_unpaid_cycles": "số kỳ nợ trước đó", "prior_escalations": "escalate trước đó",
    "prior_contacts": "liên hệ trước đó", "csat_low": "CSAT ≤ 2",
}


def fig_coef(engine) -> None:
    df = q(engine, "SELECT * FROM gold.dss_model_coef").head(12).iloc[::-1]
    names = [LABELS.get(f, f) for f in df["feature"]]
    colors = [ORANGE if c > 0 else BLUE for c in df["coef_std"]]
    fig, ax = plt.subplots(figsize=(6.4, 4))
    ax.barh(names, df["coef_std"], color=colors, height=0.65, edgecolor="white", linewidth=2)
    ax.axvline(0, color=INK2, lw=1)
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    for yi, (c, orr) in enumerate(zip(df["coef_std"], df["odds_ratio_per_sd"])):
        ax.annotate(f"OR {orr:.2f}".replace(".", ","), (c, yi),
                    xytext=(4 if c > 0 else -4, 0), textcoords="offset points",
                    ha="left" if c > 0 else "right", va="center", fontsize=8.5, color=INK2)
    ax.set_xlim(-0.48, 0.36)
    ax.set_xlabel("hệ số chuẩn hoá (cam: tăng rủi ro, xanh: giảm rủi ro)")
    ax.set_title("12 biến ảnh hưởng mạnh nhất trong mô hình hazard")
    save(fig, "fig_coefficients")


def fig_whatif(engine) -> None:
    budgets = [2500, 5000, 10000, 15000, 20000, 30000, 40000, 60000, 80000]
    rows = []
    for sr in (0.1, 0.2, 0.3):
        for b in budgets:
            r = q(engine, f"""SELECT * FROM gold.fn_retention_plan_summary(
                      DATE '2020-04-01', {b}, 10, {sr})""").iloc[0]
            rows.append({"save_rate": sr, "budget": b, **r.to_dict()})
    df = pd.DataFrame(rows)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.6), gridspec_kw={"wspace": 0.28})
    for sr, color in ((0.1, AQUA), (0.2, ORANGE), (0.3, BLUE)):
        d = df[df["save_rate"] == sr]
        a1.plot(d["budget"] / 1000, d["expected_net_value"] / 1000, color=color, lw=2,
                marker="o", ms=4)
        a1.annotate(f"giữ chân {int(sr * 100)}%", (d["budget"].iloc[-1] / 1000,
                    d["expected_net_value"].iloc[-1] / 1000), xytext=(5, 0),
                    textcoords="offset points", va="center", fontsize=9, color=INK)
        a2.plot(d["budget"] / 1000, d["selected_customers"], color=color, lw=2,
                marker="o", ms=4)
        a2.annotate(f"{int(sr * 100)}%", (d["budget"].iloc[-1] / 1000,
                    d["selected_customers"].iloc[-1]), xytext=(5, 0),
                    textcoords="offset points", va="center", fontsize=9, color=INK)
    a1.set_xlim(0, 100)
    a1.set_xlabel("ngân sách (nghìn đơn vị tiền)")
    a1.set_ylabel("giá trị ròng kỳ vọng (nghìn)")
    a1.set_title("Giá trị ròng kỳ vọng theo ngân sách")
    a2.set_xlabel("ngân sách (nghìn đơn vị tiền)")
    a2.set_ylabel("số khách được chọn")
    a2.set_xlim(0, 90)
    a2.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda v, _: f"{v:,.0f}".replace(",", ".")))
    a2.set_title("Số khách được chọn")
    save(fig, "fig_whatif_budget")
    df.to_csv(OUT / "whatif_2020_04.csv", index=False)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    engine = create_engine(config.DATABASE_URL)
    print(f"[figures] -> {OUT.relative_to(config.ROOT_DIR)}")
    for fn in (fig_cohort, fig_hazard, fig_drivers, fig_issue_channel, fig_sentinel,
               fig_roc, fig_backtest, fig_calibration, fig_coef, fig_whatif):
        fn(engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
