#!/usr/bin/env python3
"""
Generate Metabase questions + dashboards from the verified SQL in
docs/story/presentation.md.

Why a script instead of clicking?
  * 21 questions is tedious to build by hand
  * every chart here maps to a number that was verified against the warehouse
  * re-running after `make reset` rebuilds everything in ~30 seconds

Prerequisites
-------------
1. `make up` and `make all` have run (warehouse populated)
2. Metabase has been set up once (admin account created) and the
   `warehouse` database has been added:
       host=postgres  port=5432  db=warehouse  user=dw  pass=dw
3. Metabase is reachable at http://localhost:3000

Usage
-----
    export MB_URL=http://localhost:3000
    export MB_USER=you@example.com
    export MB_PASS=yourpassword
    python scripts/build_metabase_dashboards.py

    # or pass explicitly
    python scripts/build_metabase_dashboards.py \
        --url http://localhost:3000 --user a@b.c --password secret

Idempotency
-----------
Cards are matched by name. If a card with the same name already exists it is
updated in place (PUT) instead of duplicated. Dashboards are matched by name
too. Re-running is therefore safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no extra dependency)
# ---------------------------------------------------------------------------


class Metabase:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self.session_id: str | None = None
        self._login(user, password)

    # -- low level ---------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{self.base}/api{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.session_id:
            req.add_header("X-Metabase-Session", self.session_id)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:800]
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}\n{detail}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict) -> Any:
        return self._request("POST", path, payload)

    def put(self, path: str, payload: dict) -> Any:
        return self._request("PUT", path, payload)

    # -- auth --------------------------------------------------------------
    def _login(self, user: str, password: str) -> None:
        result = self.post("/session", {"username": user, "password": password})
        self.session_id = result["id"]
        print(f"[auth] logged in as {user}")

    # -- discovery ---------------------------------------------------------
    def find_database(self, name: str) -> int:
        databases = self.get("/database")
        items = databases["data"] if isinstance(databases, dict) else databases
        for db in items:
            if db.get("name", "").lower() == name.lower():
                print(f"[db] found '{name}' -> id={db['id']}")
                return int(db["id"])
        available = ", ".join(d.get("name", "?") for d in items)
        raise RuntimeError(
            f"Database '{name}' not found in Metabase. Available: {available}\n"
            "Add it first: Admin -> Databases -> Add database -> PostgreSQL\n"
            "  host=postgres port=5432 db=warehouse user=dw pass=dw"
        )

    def find_collection(self, name: str) -> int | None:
        try:
            items = self.get("/collection")
        except RuntimeError:
            return None
        for c in items or []:
            if c.get("name", "").lower() == name.lower():
                return int(c["id"])
        return None

    def ensure_collection(self, name: str) -> int:
        existing = self.find_collection(name)
        if existing is not None:
            print(f"[collection] reusing '{name}' -> id={existing}")
            return existing
        created = self.post("/collection", {"name": name, "color": "#509EE3"})
        print(f"[collection] created '{name}' -> id={created['id']}")
        return int(created["id"])

    # -- cards -------------------------------------------------------------
    def find_card(self, name: str) -> dict | None:
        results = self.get("/card")
        items = results["data"] if isinstance(results, dict) else results
        for card in items or []:
            if card.get("name") == name:
                return card
        return None

    def upsert_card(
        self,
        *,
        name: str,
        sql: str,
        display: str,
        database_id: int,
        collection_id: int | None,
        description: str = "",
        viz: dict | None = None,
    ) -> int:
        payload = {
            "name": name,
            "display": display,
            "dataset_query": {
                "database": database_id,
                "type": "native",
                "native": {"query": sql, "template-tags": {}},
            },
            "visualization_settings": viz or {},
            "collection_id": collection_id,
        }
        # Metabase rejects `description: ""` ("nullable value must be a
        # non-blank string"), so only send the key when it has content.
        if description:
            payload["description"] = description
        existing = self.find_card(name)
        if existing:
            card_id = int(existing["id"])
            self.put(f"/card/{card_id}", payload)
            print(f"  [card] updated  {name}")
            return card_id
        created = self.post("/card", payload)
        print(f"  [card] created  {name}")
        return int(created["id"])

    # -- dashboards --------------------------------------------------------
    def find_dashboard(self, name: str) -> dict | None:
        results = self.get("/dashboard")
        items = results["data"] if isinstance(results, dict) else results
        for dash in items or []:
            if dash.get("name") == name:
                return dash
        return None

    def ensure_dashboard(
        self, name: str, description: str, collection_id: int | None
    ) -> int:
        existing = self.find_dashboard(name)
        if existing:
            dash_id = int(existing["id"])
            self.put(f"/dashboard/{dash_id}", {"name": name, "description": description})
            print(f"[dashboard] reusing '{name}' -> id={dash_id}")
            return dash_id
        created = self.post(
            "/dashboard",
            {"name": name, "description": description, "collection_id": collection_id},
        )
        print(f"[dashboard] created '{name}' -> id={created['id']}")
        return int(created["id"])

    def set_dashboard_cards(self, dashboard_id: int, layout: list[dict]) -> None:
        """
        Replace the dashboard layout.

        Metabase v0.5x+ has NO POST endpoint for adding a dashcard. The only
        way to place cards is `PUT /api/dashboard/{id}/cards` with the FULL
        list. New dashcards are signalled by a NEGATIVE `id`; existing ones
        keep their real id. Omitting an existing dashcard from the payload
        removes it from the dashboard.
        """
        current = self.get(f"/dashboard/{dashboard_id}")
        existing_by_card = {
            dc["card_id"]: dc["id"]
            for dc in current.get("dashcards", [])
            if dc.get("card_id")
        }

        cards_payload = []
        next_negative = -1
        for item in layout:
            card_id = item["card_id"]
            dashcard_id = existing_by_card.get(card_id)
            if dashcard_id is None:
                dashcard_id = next_negative
                next_negative -= 1
            cards_payload.append(
                {
                    "id": dashcard_id,
                    "card_id": card_id,
                    "row": item["row"],
                    "col": item["col"],
                    "size_x": item["size_x"],
                    "size_y": item["size_y"],
                    "parameter_mappings": [],
                    "visualization_settings": {},
                }
            )

        self.put(f"/dashboard/{dashboard_id}/cards", {"cards": cards_payload})
        print(f"  [layout] {len(cards_payload)} cards placed")


# ---------------------------------------------------------------------------
# SQL definitions — every query verified against the warehouse
# ---------------------------------------------------------------------------

Q = {
    # ---------------- Overview / KPI ----------------
    "KPI · Tổng quan": """
SELECT
    COUNT(*)                                              AS billing_cycles,
    COUNT(DISTINCT customer_key)                          AS customers,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate_pct,
    ROUND(AVG(total_bill_amount)::numeric, 2)             AS avg_bill,
    ROUND(100.0 * COUNT(*) FILTER (WHERE support_interaction_count = 0)
          / COUNT(*), 2)                                  AS silent_pct
FROM gold.fact_billing_support
""",
    "Cohort retention & ARPU": """
SELECT
    month_start,
    ROUND(100.0 * active_customers
          / FIRST_VALUE(active_customers) OVER (ORDER BY month_start), 1) AS pct_cohort_active,
    arpu
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
ORDER BY month_start
""",
    "Churn theo issue × channel": """
SELECT
    issue_type,
    support_channel,
    churn_rate
FROM gold.mv_churn_by_issue_channel
ORDER BY issue_type, support_channel
""",
    "Im lặng vs có liên hệ": """
SELECT
    CASE WHEN support_interaction_count = 0
         THEN 'Không liên hệ' ELSE 'Có liên hệ' END        AS segment,
    COUNT(*)                                               AS cycles,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
GROUP BY 1
ORDER BY churn_rate
""",
    "Churn theo số lần liên hệ": """
SELECT
    support_interaction_count                              AS so_lan_lien_he,
    COUNT(*)                                               AS cycles,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
GROUP BY 1
ORDER BY 1
""",
    # ---------------- Revenue & Billing ----------------
    "Doanh thu theo tháng": """
SELECT month_start, total_revenue
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
ORDER BY month_start
""",
    "ARPU theo tháng": """
SELECT month_start, arpu
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
ORDER BY month_start
""",
    "Tỷ lệ quá hạn": """
SELECT month_start, overdue_rate
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
ORDER BY month_start
""",
    "Discount & late fee (% doanh thu)": """
SELECT
    month_start,
    ROUND(100.0 * total_discount  / NULLIF(total_revenue, 0), 2) AS discount_pct,
    ROUND(100.0 * total_late_fee  / NULLIF(total_revenue, 0), 2) AS late_fee_pct
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
ORDER BY month_start
""",
    "Khách không liên hệ support": """
SELECT month_start, no_support_customers
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
ORDER BY month_start
""",
    # ---------------- Support Ops ----------------
    "Tickets theo kênh": """
SELECT support_channel, SUM(tickets) AS tickets
FROM gold.mart_support_monthly
GROUP BY 1
ORDER BY tickets DESC
""",
    "Thời gian xử lý theo kênh": """
SELECT
    support_channel,
    ROUND(SUM(avg_resolution_min * tickets) / SUM(tickets), 2) AS avg_resolution_min
FROM gold.mart_support_monthly
GROUP BY 1
ORDER BY avg_resolution_min
""",
    "Phân bố CSAT": """
SELECT
    satisfaction_score,
    COUNT(*)                                               AS cycles,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2)     AS pct
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1
ORDER BY 1
""",
    "Escalation rate theo kênh": """
SELECT
    support_channel,
    ROUND(100.0 * SUM(escalations) / SUM(tickets), 2) AS escalation_rate
FROM gold.mart_support_monthly
GROUP BY 1
ORDER BY escalation_rate DESC
""",
    "CSAT theo issue × channel": """
SELECT
    issue_type,
    support_channel,
    ROUND(SUM(csat * tickets) / SUM(tickets), 2) AS csat
FROM gold.mart_support_monthly
GROUP BY 1, 2
ORDER BY issue_type, support_channel
""",
    # ---------------- Churn Risk ----------------
    "Churn theo payment status": """
SELECT
    ps.payment_status,
    COUNT(*)                                               AS cycles,
    ROUND(100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_payment_status ps USING (payment_status_key)
GROUP BY 1
ORDER BY churn_rate DESC
""",
    "Churn theo CSAT": """
SELECT
    satisfaction_score,
    COUNT(*)                                               AS cycles,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1
ORDER BY 1
""",
    "Churn theo escalation": """
SELECT
    CASE WHEN escalation_flag THEN 'Có escalate' ELSE 'Không' END AS escalation,
    COUNT(*)                                               AS cycles,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1
ORDER BY churn_rate DESC
""",
    "Cảnh báo sớm: liên hệ kỳ t → churn kỳ t+1": """
WITH seq AS (
    SELECT
        customer_key,
        support_interaction_count > 0 AS contacted,
        churn_flag,
        LEAD(churn_flag) OVER (
            PARTITION BY customer_key ORDER BY billing_start_date_key
        ) AS churn_next
    FROM gold.fact_billing_support
)
SELECT
    CASE WHEN contacted THEN 'Có liên hệ' ELSE 'Không liên hệ' END AS ky_truoc,
    COUNT(*)                                               AS cycle_pairs,
    ROUND(100.0 * AVG(CASE WHEN churn_next THEN 1.0 ELSE 0.0 END), 2) AS churn_ky_sau
FROM seq
WHERE churn_flag = FALSE
  AND churn_next IS NOT NULL
GROUP BY 1
ORDER BY churn_ky_sau DESC
""",
    "Chân dung khách churn": """
SELECT
    CASE WHEN churn_flag THEN 'Churn' ELSE 'Ở lại' END     AS nhom,
    ROUND(AVG(call_minutes)::numeric, 1)                   AS avg_call_min,
    ROUND(AVG(data_usage_gb)::numeric, 2)                  AS avg_data_gb,
    ROUND(AVG(late_fee)::numeric, 2)                       AS avg_late_fee,
    ROUND(AVG(support_interaction_count)::numeric, 2)      AS avg_interactions
FROM gold.fact_billing_support
GROUP BY 1
ORDER BY 1
""",
    "Bảng xếp hạng tín hiệu (lift)": """
WITH base AS (
    SELECT AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END) AS base_rate
    FROM gold.fact_billing_support
),
sig AS (
    SELECT 'Escalation' AS signal,
           COUNT(*) FILTER (WHERE f.escalation_flag) AS exposed,
           COUNT(*) AS total,
           AVG(CASE WHEN f.escalation_flag
                    THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END) AS exposed_rate
    FROM gold.fact_billing_support f
    WHERE f.support_interaction_count > 0
    UNION ALL
    SELECT 'Payment Unpaid',
           COUNT(*) FILTER (WHERE ps.payment_status = 'Unpaid'),
           COUNT(*),
           AVG(CASE WHEN ps.payment_status = 'Unpaid'
                    THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
    FROM gold.fact_billing_support f
    JOIN gold.dim_payment_status ps USING (payment_status_key)
    UNION ALL
    SELECT 'CSAT <= 2',
           COUNT(*) FILTER (WHERE f.satisfaction_score <= 2),
           COUNT(*),
           AVG(CASE WHEN f.satisfaction_score <= 2
                    THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
    FROM gold.fact_billing_support f
    WHERE f.support_interaction_count > 0
    UNION ALL
    SELECT 'Interactions >= 3',
           COUNT(*) FILTER (WHERE f.support_interaction_count >= 3),
           COUNT(*),
           AVG(CASE WHEN f.support_interaction_count >= 3
                    THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
    FROM gold.fact_billing_support f
    UNION ALL
    SELECT 'Có liên hệ',
           COUNT(*) FILTER (WHERE f.support_interaction_count > 0),
           COUNT(*),
           AVG(CASE WHEN f.support_interaction_count > 0
                    THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
    FROM gold.fact_billing_support f
)
SELECT
    s.signal,
    s.exposed,
    ROUND(100.0 * s.exposed / s.total, 2)      AS coverage_pct,
    ROUND(100.0 * s.exposed_rate, 2)           AS exposed_churn,
    ROUND(s.exposed_rate / b.base_rate, 2)     AS lift
FROM sig s CROSS JOIN base b
ORDER BY lift DESC
""",
}


# ---------------------------------------------------------------------------
# Chart specs: question name -> (display type, visualization_settings)
# ---------------------------------------------------------------------------

VIZ = {
    "KPI · Tổng quan": ("table", {}),
    "Cohort retention & ARPU": (
        "line",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["pct_cohort_active", "arpu"]},
    ),
    "Churn theo issue × channel": (
        "pivot",
        {
            "pivot_table.column_split": {
                "rows": ["issue_type"],
                "columns": ["support_channel"],
                "values": ["churn_rate"],
            }
        },
    ),
    "Im lặng vs có liên hệ": (
        "bar",
        {"graph.dimensions": ["segment"], "graph.metrics": ["churn_rate"]},
    ),
    "Churn theo số lần liên hệ": (
        "bar",
        {"graph.dimensions": ["so_lan_lien_he"], "graph.metrics": ["churn_rate"]},
    ),
    "Doanh thu theo tháng": (
        "line",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["total_revenue"]},
    ),
    "ARPU theo tháng": (
        "bar",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["arpu"]},
    ),
    "Tỷ lệ quá hạn": (
        "line",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["overdue_rate"]},
    ),
    "Discount & late fee (% doanh thu)": (
        "line",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["discount_pct", "late_fee_pct"]},
    ),
    "Khách không liên hệ support": (
        "bar",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["no_support_customers"]},
    ),
    "Tickets theo kênh": (
        "bar",
        {"graph.dimensions": ["support_channel"], "graph.metrics": ["tickets"]},
    ),
    "Thời gian xử lý theo kênh": (
        "bar",
        {"graph.dimensions": ["support_channel"], "graph.metrics": ["avg_resolution_min"]},
    ),
    "Phân bố CSAT": (
        "bar",
        {"graph.dimensions": ["satisfaction_score"], "graph.metrics": ["pct"]},
    ),
    "Escalation rate theo kênh": (
        "bar",
        {"graph.dimensions": ["support_channel"], "graph.metrics": ["escalation_rate"]},
    ),
    "CSAT theo issue × channel": (
        "pivot",
        {
            "pivot_table.column_split": {
                "rows": ["issue_type"],
                "columns": ["support_channel"],
                "values": ["csat"],
            }
        },
    ),
    "Churn theo payment status": (
        "bar",
        {"graph.dimensions": ["payment_status"], "graph.metrics": ["churn_rate"]},
    ),
    "Churn theo CSAT": (
        "bar",
        {"graph.dimensions": ["satisfaction_score"], "graph.metrics": ["churn_rate"]},
    ),
    "Churn theo escalation": (
        "bar",
        {"graph.dimensions": ["escalation"], "graph.metrics": ["churn_rate"]},
    ),
    "Cảnh báo sớm: liên hệ kỳ t → churn kỳ t+1": (
        "bar",
        {"graph.dimensions": ["ky_truoc"], "graph.metrics": ["churn_ky_sau"]},
    ),
    "Chân dung khách churn": ("table", {}),
    "Bảng xếp hạng tín hiệu (lift)": ("table", {}),
}


# ---------------------------------------------------------------------------
# Dashboard layouts: name -> (description, [(question, row, col, size_x, size_y)])
# Grid is 24 columns wide.
# ---------------------------------------------------------------------------

DASHBOARDS: dict[str, tuple[str, list[tuple[str, int, int, int, int]]]] = {
    "1 · Overview — Churn Story": (
        "Bức tranh tổng thể: cohort co lại, churn đều giữa issue/kênh, "
        "nghịch lý im lặng. Nguồn: gold.mv_billing_monthly, "
        "gold.mv_churn_by_issue_channel, gold.fact_billing_support.",
        [
            ("KPI · Tổng quan", 0, 0, 24, 4),
            ("Cohort retention & ARPU", 4, 0, 24, 8),
            ("Churn theo issue × channel", 12, 0, 12, 8),
            ("Im lặng vs có liên hệ", 12, 12, 12, 8),
            ("Churn theo số lần liên hệ", 20, 0, 24, 8),
        ],
    ),
    "2 · Revenue & Billing": (
        "Doanh thu, ARPU, quá hạn, discount. Dùng gold.mv_billing_monthly "
        "(materialized). Lưu ý: total_revenue giảm do cohort co lại, "
        "không phải do giá — luôn đọc kèm ARPU.",
        [
            ("Doanh thu theo tháng", 0, 0, 12, 8),
            ("ARPU theo tháng", 0, 12, 12, 8),
            ("Tỷ lệ quá hạn", 8, 0, 12, 8),
            ("Discount & late fee (% doanh thu)", 8, 12, 12, 8),
            ("Khách không liên hệ support", 16, 0, 24, 8),
        ],
    ),
    "3 · Support Ops": (
        "Hiệu suất support theo kênh và issue. Dùng gold.mart_support_monthly "
        "(đã loại 18.719 kỳ không liên hệ). tickets = số billing cycle có "
        "liên hệ, không phải số lần liên hệ.",
        [
            ("Tickets theo kênh", 0, 0, 12, 8),
            ("Thời gian xử lý theo kênh", 0, 12, 12, 8),
            ("Escalation rate theo kênh", 8, 0, 12, 8),
            ("Phân bố CSAT", 8, 12, 12, 8),
            ("CSAT theo issue × channel", 16, 0, 24, 8),
        ],
    ),
    "4 · Churn Risk": (
        "Tín hiệu rủi ro churn xếp theo lift. Escalation 1,60× > Payment "
        "Unpaid 1,55× > CSAT≤2 1,43× > Interactions≥3 1,23× > Có liên hệ 1,13×.",
        [
            ("Bảng xếp hạng tín hiệu (lift)", 0, 0, 24, 8),
            ("Churn theo payment status", 8, 0, 8, 8),
            ("Churn theo escalation", 8, 8, 8, 8),
            ("Churn theo CSAT", 8, 16, 8, 8),
            ("Cảnh báo sớm: liên hệ kỳ t → churn kỳ t+1", 16, 0, 12, 8),
            ("Chân dung khách churn", 16, 12, 12, 8),
        ],
    ),
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Load .env from the repo root so the script also works when invoked
    # directly (`python scripts/build_metabase_dashboards.py`) instead of
    # through `make`, which already exports the file.
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:  # python-dotenv missing -> rely on real env vars
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("MB_URL", "http://localhost:3000"))
    parser.add_argument("--user", default=os.getenv("MB_USER"))
    parser.add_argument("--password", default=os.getenv("MB_PASS"))
    parser.add_argument("--database", default=os.getenv("MB_DATABASE", "warehouse"))
    parser.add_argument("--collection", default=os.getenv("MB_COLLECTION", "Telco Churn DSS"))
    args = parser.parse_args()

    if not args.user or not args.password:
        print(
            "ERROR: Metabase credentials required.\n"
            "\n"
            "Add them to the `.env` file in the repo root (gitignored):\n"
            "    MB_USER=you@example.com\n"
            "    MB_PASS=yourpassword\n"
            "\n"
            "Then re-run:  make metabase-dashboards\n"
            "\n"
            "Or pass them explicitly:\n"
            "    python scripts/build_metabase_dashboards.py \\\n"
            "        --user you@example.com --password yourpassword",
            file=sys.stderr,
        )
        return 2

    mb = Metabase(args.url, args.user, args.password)
    database_id = mb.find_database(args.database)
    collection_id = mb.ensure_collection(args.collection)

    # ---- 1. create / update every question ------------------------------
    print("\n=== Questions ===")
    card_ids: dict[str, int] = {}
    for name, sql in Q.items():
        display, viz = VIZ.get(name, ("table", {}))
        card_ids[name] = mb.upsert_card(
            name=name,
            sql=sql.strip(),
            display=display,
            database_id=database_id,
            collection_id=collection_id,
            viz=viz,
        )

    # ---- 2. create / update every dashboard -----------------------------
    print("\n=== Dashboards ===")
    for dash_name, (description, layout) in DASHBOARDS.items():
        dash_id = mb.ensure_dashboard(dash_name, description, collection_id)
        mb.set_dashboard_cards(
            dash_id,
            [
                {
                    "card_id": card_ids[q],
                    "row": row,
                    "col": col,
                    "size_x": sx,
                    "size_y": sy,
                }
                for (q, row, col, sx, sy) in layout
            ],
        )

    print("\n" + "=" * 70)
    print(f"Done. {len(card_ids)} questions, {len(DASHBOARDS)} dashboards.")
    print(f"Open {args.url} -> collection '{args.collection}'")
    print("=" * 70)
    print(
        "\nNext: persist the dashboards so teammates get them on clone:\n"
        "  make metabase-dump\n"
        "  git add metabase_app.sql && git commit -m 'feat(metabase): dashboards'"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
