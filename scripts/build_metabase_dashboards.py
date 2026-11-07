#!/usr/bin/env python3
"""
Generate Metabase questions + dashboards for the Telco Churn DSS.

What this builds
----------------
  1. Interactive filters — a date range slider plus dropdowns for
     payment_status / issue_type / support_channel, wired to every card that
     can accept them.
  2. OLAP operations — roll-up, drill-down, slice, dice, pivot and cube, each
     as a runnable question, plus a dedicated "OLAP Lab" dashboard.
  3. Mart demos — one dashboard per data mart, so the marts are demonstrated
     rather than merely existing.
  4. EDA — distribution, outlier, correlation and sentinel views over the raw
     measures.

How filtering works (important)
-------------------------------
Metabase "field filters" (`WHERE {{tag}}` with `type: dimension`) only work
when the referenced table appears in the FROM clause WITHOUT an alias. Every
query here joins dimensions with aliases, so field filters fail with
"invalid reference to FROM-clause entry".

The reliable pattern is a SIMPLE VARIABLE inside an optional clause:

    WHERE 1 = 1
    [[AND support_channel = {{channel}}]]
    [[AND month_start >= {{d_start}}::date]]
    [[AND month_start <= {{d_end}}::date]]

Metabase strips the whole `[[...]]` block when the parameter is absent, so the
same card works filtered and unfiltered. This is what the script generates.

Prerequisites
-------------
1. `make up` and `make all` have run (warehouse populated).
2. Metabase set up once, `warehouse` database added:
       host=postgres  port=5432  db=warehouse  user=dw  pass=dw
3. MB_USER / MB_PASS in `.env`.

Usage
-----
    make metabase-dashboards

Idempotency
-----------
Cards and dashboards are matched by name: existing ones are updated in place,
so re-running never duplicates anything.
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
# HTTP client
# ---------------------------------------------------------------------------


class Metabase:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self.session_id: str | None = None
        self._login(user, password)

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{self.base}/api{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.session_id:
            req.add_header("X-Metabase-Session", self.session_id)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:900]
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}\n{detail}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict) -> Any:
        return self._request("POST", path, payload)

    def put(self, path: str, payload: dict) -> Any:
        return self._request("PUT", path, payload)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

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
                print(f"[db] '{name}' -> id={db['id']}")
                return int(db["id"])
        available = ", ".join(d.get("name", "?") for d in items)
        raise RuntimeError(
            f"Database '{name}' not found. Available: {available}\n"
            "Add it: Admin -> Databases -> Add database -> PostgreSQL\n"
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
        template_tags: dict | None = None,
    ) -> int:
        payload: dict[str, Any] = {
            "name": name,
            "display": display,
            "dataset_query": {
                "database": database_id,
                "type": "native",
                "native": {
                    "query": sql,
                    "template-tags": template_tags or {},
                },
            },
            "visualization_settings": viz or {},
            "collection_id": collection_id,
        }
        # Metabase rejects description: "" ("must be a non-blank string").
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

    def set_dashboard_cards(
        self,
        dashboard_id: int,
        layout: list[dict],
        parameters: list[dict] | None = None,
    ) -> None:
        """
        Replace the dashboard layout and its filter widgets.

        Metabase has no POST endpoint for adding a dashcard: the only way is
        `PUT /api/dashboard/{id}/cards` with the FULL list, where new
        dashcards are signalled by a NEGATIVE id.
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
                    "parameter_mappings": item.get("parameter_mappings", []),
                    "visualization_settings": item.get("visualization_settings", {}),
                }
            )

        self.put(f"/dashboard/{dashboard_id}/cards", {"cards": cards_payload})
        print(f"  [layout] {len(cards_payload)} cards placed")

        if parameters:
            self.put(
                f"/dashboard/{dashboard_id}",
                {"parameters": parameters, "auto_apply_filters": True},
            )
            print(f"  [filters] {len(parameters)} widgets attached")


# ---------------------------------------------------------------------------
# Filter definitions
#
# key -> (parameter id, display name, parameter type, template-tag name)
# The template-tag name must match the {{...}} placeholder in the SQL.
# ---------------------------------------------------------------------------

FILTERS: dict[str, tuple[str, str, str, str]] = {
    "date": ("f_date", "Khoảng thời gian", "date/all-options", "d_start"),
    "payment": ("f_payment", "Trạng thái thanh toán", "string/=", "payment"),
    "issue": ("f_issue", "Loại vấn đề", "string/=", "issue"),
    "channel": ("f_channel", "Kênh support", "string/=", "channel"),
}

# A single dashboard parameter cannot feed two template tags with different
# values (Metabase sends the same value to both), so the date range is exposed
# as TWO widgets: "Từ tháng" and "Đến tháng".
DATE_PARAMS = (
    ("f_start", "Từ tháng", "d_start"),
    ("f_end", "Đến tháng", "d_end"),
)


def build_parameters(wanted: list[str]) -> tuple[list[dict], dict[str, str]]:
    """Return (dashboard parameters, {filter key -> parameter id})."""
    params: list[dict] = []
    ids: dict[str, str] = {}
    for key in wanted:
        if key == "date":
            for pid, name, _tag in DATE_PARAMS:
                params.append(
                    {
                        "id": pid,
                        "name": name,
                        "slug": pid.replace("f_", ""),
                        "type": "date/single",
                        "sectionId": "date",
                    }
                )
            ids["date"] = "f_start"  # marker; mappings handled separately
            continue
        pid, name, ptype, _tag = FILTERS[key]
        params.append(
            {
                "id": pid,
                "name": name,
                "slug": pid.replace("f_", ""),
                "type": ptype,
                "sectionId": "category",
            }
        )
        ids[key] = pid
    return params, ids


def template_tags_for(filter_keys: list[str]) -> dict:
    """Build the template-tags map for the {{...}} placeholders in a query."""
    tags: dict[str, dict] = {}
    for key in filter_keys:
        if key == "date":
            tags["d_start"] = {
                "id": "tag_d_start",
                "name": "d_start",
                "display-name": "Từ ngày",
                "type": "date",
            }
            tags["d_end"] = {
                "id": "tag_d_end",
                "name": "d_end",
                "display-name": "Đến ngày",
                "type": "date",
            }
        else:
            tag = FILTERS[key][3]
            tags[tag] = {
                "id": f"tag_{tag}",
                "name": tag,
                "display-name": FILTERS[key][1],
                "type": "text",
            }
    return tags


def parameter_mappings(
    filter_keys: list[str], param_ids: dict[str, str], card_id: int
) -> list[dict]:
    """
    Map dashboard parameters onto a card's template tags.

    `card_id` is REQUIRED in each mapping. Without it Metabase stores the
    mapping but never applies the parameter, so the dashboard filter silently
    does nothing (the card still renders, just unfiltered).
    """
    out: list[dict] = []
    for key in filter_keys:
        if key not in param_ids:
            continue
        if key == "date":
            # Two widgets, one tag each.
            for pid, _name, tag in DATE_PARAMS:
                out.append(
                    {
                        "parameter_id": pid,
                        "card_id": card_id,
                        "target": ["variable", ["template-tag", tag]],
                    }
                )
        else:
            tag = FILTERS[key][3]
            out.append(
                {
                    "parameter_id": param_ids[key],
                    "card_id": card_id,
                    "target": ["variable", ["template-tag", tag]],
                }
            )
    return out


# ---------------------------------------------------------------------------
# SQL — business story (verified numbers)
#
# Cards that accept filters carry the optional-clause pattern:
#     WHERE 1 = 1
#     [[AND month_start >= {{d_start}}::date]]
#     [[AND month_start <= {{d_end}}::date]]
# ---------------------------------------------------------------------------

Q: dict[str, str] = {
    # ---------------- Overview ----------------
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
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    "Churn theo issue × channel": """
SELECT issue_type, support_channel, churn_rate
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
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    "ARPU theo tháng": """
SELECT month_start, arpu
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    "Tỷ lệ quá hạn": """
SELECT month_start, overdue_rate
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    "Discount & late fee (% doanh thu)": """
SELECT
    month_start,
    ROUND(100.0 * total_discount / NULLIF(total_revenue, 0), 2) AS discount_pct,
    ROUND(100.0 * total_late_fee / NULLIF(total_revenue, 0), 2) AS late_fee_pct
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    "Khách không liên hệ support": """
SELECT month_start, no_support_customers
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    # ---------------- Support Ops ----------------
    "Tickets theo kênh": """
SELECT support_channel, SUM(tickets) AS tickets
FROM gold.mart_support_monthly
WHERE 1 = 1
[[AND support_channel = {{channel}}]]
[[AND issue_type = {{issue}}]]
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
GROUP BY 1
ORDER BY tickets DESC
""",
    "Thời gian xử lý theo kênh": """
SELECT
    support_channel,
    ROUND(SUM(avg_resolution_min * tickets) / SUM(tickets), 2) AS avg_resolution_min
FROM gold.mart_support_monthly
WHERE 1 = 1
[[AND support_channel = {{channel}}]]
[[AND issue_type = {{issue}}]]
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
GROUP BY 1
ORDER BY avg_resolution_min
""",
    "Phân bố CSAT": """
SELECT
    satisfaction_score,
    COUNT(*)                                           AS cycles,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
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
WHERE 1 = 1
[[AND support_channel = {{channel}}]]
[[AND issue_type = {{issue}}]]
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
GROUP BY 1
ORDER BY escalation_rate DESC
""",
    "CSAT theo issue × channel": """
SELECT
    issue_type,
    support_channel,
    ROUND(SUM(csat * tickets) / SUM(tickets), 2) AS csat
FROM gold.mart_support_monthly
WHERE 1 = 1
[[AND support_channel = {{channel}}]]
[[AND issue_type = {{issue}}]]
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
GROUP BY 1, 2
ORDER BY issue_type, support_channel
""",
    # ---------------- Churn Risk ----------------
    "Churn theo payment status": """
SELECT
    ps.payment_status,
    COUNT(*) AS cycles,
    ROUND(100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_payment_status ps USING (payment_status_key)
WHERE 1 = 1
[[AND ps.payment_status = {{payment}}]]
GROUP BY 1
ORDER BY churn_rate DESC
""",
    "Churn theo CSAT": """
SELECT
    satisfaction_score,
    COUNT(*) AS cycles,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1
ORDER BY 1
""",
    "Churn theo escalation": """
SELECT
    CASE WHEN escalation_flag THEN 'Có escalate' ELSE 'Không' END AS escalation,
    COUNT(*) AS cycles,
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
    COUNT(*) AS cycle_pairs,
    ROUND(100.0 * AVG(CASE WHEN churn_next THEN 1.0 ELSE 0.0 END), 2) AS churn_ky_sau
FROM seq
WHERE churn_flag = FALSE
  AND churn_next IS NOT NULL
GROUP BY 1
ORDER BY churn_ky_sau DESC
""",
    "Chân dung khách churn": """
SELECT
    CASE WHEN churn_flag THEN 'Churn' ELSE 'Ở lại' END AS nhom,
    ROUND(AVG(call_minutes)::numeric, 1)              AS avg_call_min,
    ROUND(AVG(data_usage_gb)::numeric, 2)             AS avg_data_gb,
    ROUND(AVG(late_fee)::numeric, 2)                  AS avg_late_fee,
    ROUND(AVG(support_interaction_count)::numeric, 2) AS avg_interactions
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
           COUNT(*) FILTER (WHERE ps.payment_status = 'Unpaid'), COUNT(*),
           AVG(CASE WHEN ps.payment_status = 'Unpaid'
                    THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
    FROM gold.fact_billing_support f
    JOIN gold.dim_payment_status ps USING (payment_status_key)
    UNION ALL
    SELECT 'CSAT <= 2',
           COUNT(*) FILTER (WHERE f.satisfaction_score <= 2), COUNT(*),
           AVG(CASE WHEN f.satisfaction_score <= 2
                    THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
    FROM gold.fact_billing_support f
    WHERE f.support_interaction_count > 0
    UNION ALL
    SELECT 'Interactions >= 3',
           COUNT(*) FILTER (WHERE f.support_interaction_count >= 3), COUNT(*),
           AVG(CASE WHEN f.support_interaction_count >= 3
                    THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
    FROM gold.fact_billing_support f
    UNION ALL
    SELECT 'Có liên hệ',
           COUNT(*) FILTER (WHERE f.support_interaction_count > 0), COUNT(*),
           AVG(CASE WHEN f.support_interaction_count > 0
                    THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
    FROM gold.fact_billing_support f
)
SELECT
    s.signal,
    s.exposed,
    ROUND(100.0 * s.exposed / s.total, 2)  AS coverage_pct,
    ROUND(100.0 * s.exposed_rate, 2)       AS exposed_churn,
    ROUND(s.exposed_rate / b.base_rate, 2) AS lift
FROM sig s CROSS JOIN base b
ORDER BY lift DESC
""",
    # =====================================================================
    # OLAP operations
    # =====================================================================
    "OLAP-1 · ROLL-UP (year → quarter → month)": """
SELECT
    d.year, d.quarter, d.month,
    GROUPING(d.year, d.quarter, d.month) AS grouping_level,
    CASE GROUPING(d.year, d.quarter, d.month)
        WHEN 0 THEN 'month'
        WHEN 1 THEN 'quarter subtotal'
        WHEN 3 THEN 'year subtotal'
        WHEN 7 THEN 'GRAND TOTAL'
    END                                  AS level_label,
    COUNT(*)                             AS billing_cycles,
    SUM(f.total_bill_amount)             AS revenue
FROM gold.fact_billing_support f
JOIN gold.dim_date d ON d.date_key = f.billing_start_date_key
WHERE d.is_unknown = FALSE
GROUP BY ROLLUP (d.year, d.quarter, d.month)
ORDER BY d.year NULLS LAST, d.quarter NULLS LAST, d.month NULLS LAST
""",
    "OLAP-2 · DRILL-DOWN (year → month)": """
SELECT
    CASE GROUPING(d.year, d.month)
        WHEN 0 THEN '2. month detail'
        WHEN 1 THEN '1. year total'
    END                                  AS drill_level,
    d.year, d.month,
    COUNT(*)                             AS billing_cycles,
    SUM(f.total_bill_amount)             AS revenue,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.churn_flag) / COUNT(*), 2) AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_date d ON d.date_key = f.billing_start_date_key
WHERE d.is_unknown = FALSE
GROUP BY GROUPING SETS ((d.year), (d.year, d.month))
ORDER BY d.year, GROUPING(d.year, d.month), d.month
""",
    "OLAP-3 · SLICE (payment_status = Paid)": """
SELECT
    it.issue_type,
    ch.support_channel,
    COUNT(*)                             AS billing_cycles,
    SUM(f.total_bill_amount)             AS revenue,
    ROUND(AVG(f.total_bill_amount), 2)   AS avg_bill
FROM gold.fact_billing_support f
JOIN gold.dim_payment_status  ps ON ps.payment_status_key  = f.payment_status_key
JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
WHERE ps.payment_status = 'Paid'
GROUP BY it.issue_type, ch.support_channel
ORDER BY revenue DESC
""",
    "OLAP-4 · DICE (year=2022 × Paid/Unpaid × Billing)": """
SELECT
    d.year, d.quarter,
    ps.payment_status,
    it.issue_type,
    COUNT(*)                             AS billing_cycles,
    SUM(f.total_bill_amount)             AS revenue,
    SUM(f.late_fee)                      AS late_fees
FROM gold.fact_billing_support f
JOIN gold.dim_date           d  ON d.date_key            = f.billing_start_date_key
JOIN gold.dim_payment_status ps ON ps.payment_status_key = f.payment_status_key
JOIN gold.dim_issue_type     it ON it.issue_type_key     = f.issue_type_key
WHERE d.year = 2022
  AND ps.payment_status IN ('Paid', 'Unpaid')
  AND it.issue_type = 'Billing'
GROUP BY d.year, d.quarter, ps.payment_status, it.issue_type
ORDER BY d.quarter, ps.payment_status
""",
    "OLAP-5 · PIVOT (issue × channel churn)": """
SELECT
    it.issue_type,
    ROUND(100.0 * AVG(CASE WHEN ch.support_channel = 'Email'
        THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END), 2) AS churn_email,
    ROUND(100.0 * AVG(CASE WHEN ch.support_channel = 'Phone'
        THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END), 2) AS churn_phone,
    ROUND(100.0 * AVG(CASE WHEN ch.support_channel = 'Chat'
        THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END), 2) AS churn_chat,
    COUNT(*) AS billing_cycles
FROM gold.fact_billing_support f
JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
WHERE it.issue_type <> 'No Support'
GROUP BY it.issue_type
ORDER BY it.issue_type
""",
    "OLAP-6 · CUBE (issue × channel × payment)": """
SELECT
    it.issue_type,
    ch.support_channel,
    ps.payment_status,
    GROUPING(it.issue_type, ch.support_channel, ps.payment_status) AS grouping_level,
    COUNT(*)                             AS billing_cycles,
    ROUND(100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_issue_type      it ON it.issue_type_key      = f.issue_type_key
JOIN gold.dim_support_channel ch ON ch.support_channel_key = f.channel_key
JOIN gold.dim_payment_status  ps ON ps.payment_status_key  = f.payment_status_key
GROUP BY CUBE (it.issue_type, ch.support_channel, ps.payment_status)
ORDER BY grouping_level,
         it.issue_type NULLS LAST,
         ch.support_channel NULLS LAST,
         ps.payment_status NULLS LAST
""",
    # =====================================================================
    # Mart demos
    # =====================================================================
    "MART · billing_monthly — toàn cảnh": """
SELECT
    month_start, billing_cycles, active_customers, total_revenue, arpu,
    overdue_rate, no_support_customers, churn_rate
FROM gold.mart_billing_monthly
WHERE 1 = 1
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    "MART · support_monthly — toàn cảnh": """
SELECT
    month_start, support_channel, issue_type, tickets,
    avg_resolution_min, csat, escalation_rate, avg_interactions
FROM gold.mart_support_monthly
WHERE 1 = 1
[[AND support_channel = {{channel}}]]
[[AND issue_type = {{issue}}]]
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start, support_channel, issue_type
""",
    "MART · churn_summary — toàn cảnh": """
SELECT
    churn_flag, payment_status, issue_type, support_channel,
    billing_cycles, customers, churned_customers,
    avg_bill, avg_csat, avg_interactions, avg_resolution
FROM gold.mart_churn_summary
WHERE 1 = 1
[[AND payment_status = {{payment}}]]
[[AND issue_type = {{issue}}]]
[[AND support_channel = {{channel}}]]
ORDER BY billing_cycles DESC
""",
    "MART · churn_summary — churn rate đúng cách": """
-- mart_churn_summary.churn_rate luôn 0 hoặc 100 (GROUP BY churn_flag),
-- nên phải tự tính lại từ billing_cycles.
SELECT
    payment_status, issue_type, support_channel,
    SUM(billing_cycles)                                        AS cycles,
    SUM(billing_cycles) FILTER (WHERE churn_flag)              AS churned,
    ROUND(100.0 * SUM(billing_cycles) FILTER (WHERE churn_flag)
          / SUM(billing_cycles), 2)                            AS churn_rate
FROM gold.mart_churn_summary
WHERE 1 = 1
[[AND payment_status = {{payment}}]]
[[AND issue_type = {{issue}}]]
[[AND support_channel = {{channel}}]]
GROUP BY payment_status, issue_type, support_channel
ORDER BY churn_rate DESC
""",
    # =====================================================================
    # EDA
    # =====================================================================
    "EDA · Thống kê mô tả 10 measure": """
SELECT 'call_minutes' AS measure,
       COUNT(*) AS n, ROUND(AVG(call_minutes)::numeric,2) AS mean,
       ROUND(STDDEV(call_minutes)::numeric,2) AS sd,
       MIN(call_minutes) AS min,
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY call_minutes)::numeric,2) AS p25,
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY call_minutes)::numeric,2) AS median,
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY call_minutes)::numeric,2) AS p75,
       MAX(call_minutes) AS max
FROM gold.fact_billing_support
UNION ALL SELECT 'data_usage_gb', COUNT(*), ROUND(AVG(data_usage_gb)::numeric,2),
       ROUND(STDDEV(data_usage_gb)::numeric,2), MIN(data_usage_gb),
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY data_usage_gb)::numeric,2),
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY data_usage_gb)::numeric,2),
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY data_usage_gb)::numeric,2),
       MAX(data_usage_gb)
FROM gold.fact_billing_support
UNION ALL SELECT 'sms_count', COUNT(*), ROUND(AVG(sms_count)::numeric,2),
       ROUND(STDDEV(sms_count)::numeric,2), MIN(sms_count),
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY sms_count)::numeric,2),
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY sms_count)::numeric,2),
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY sms_count)::numeric,2),
       MAX(sms_count)
FROM gold.fact_billing_support
UNION ALL SELECT 'internet_sessions', COUNT(*), ROUND(AVG(internet_sessions)::numeric,2),
       ROUND(STDDEV(internet_sessions)::numeric,2), MIN(internet_sessions),
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY internet_sessions)::numeric,2),
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY internet_sessions)::numeric,2),
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY internet_sessions)::numeric,2),
       MAX(internet_sessions)
FROM gold.fact_billing_support
UNION ALL SELECT 'total_bill_amount', COUNT(*), ROUND(AVG(total_bill_amount)::numeric,2),
       ROUND(STDDEV(total_bill_amount)::numeric,2), MIN(total_bill_amount),
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY total_bill_amount)::numeric,2),
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY total_bill_amount)::numeric,2),
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY total_bill_amount)::numeric,2),
       MAX(total_bill_amount)
FROM gold.fact_billing_support
UNION ALL SELECT 'late_fee', COUNT(*), ROUND(AVG(late_fee)::numeric,2),
       ROUND(STDDEV(late_fee)::numeric,2), MIN(late_fee),
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY late_fee)::numeric,2),
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY late_fee)::numeric,2),
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY late_fee)::numeric,2),
       MAX(late_fee)
FROM gold.fact_billing_support
UNION ALL SELECT 'discounts_applied', COUNT(*), ROUND(AVG(discounts_applied)::numeric,2),
       ROUND(STDDEV(discounts_applied)::numeric,2), MIN(discounts_applied),
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY discounts_applied)::numeric,2),
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY discounts_applied)::numeric,2),
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY discounts_applied)::numeric,2),
       MAX(discounts_applied)
FROM gold.fact_billing_support
UNION ALL SELECT 'support_interaction_count', COUNT(*), ROUND(AVG(support_interaction_count)::numeric,2),
       ROUND(STDDEV(support_interaction_count)::numeric,2), MIN(support_interaction_count),
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY support_interaction_count)::numeric,2),
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY support_interaction_count)::numeric,2),
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY support_interaction_count)::numeric,2),
       MAX(support_interaction_count)
FROM gold.fact_billing_support
UNION ALL SELECT 'resolution_time_minutes', COUNT(*), ROUND(AVG(resolution_time_minutes)::numeric,2),
       ROUND(STDDEV(resolution_time_minutes)::numeric,2), MIN(resolution_time_minutes),
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY resolution_time_minutes)::numeric,2),
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY resolution_time_minutes)::numeric,2),
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY resolution_time_minutes)::numeric,2),
       MAX(resolution_time_minutes)
FROM gold.fact_billing_support
UNION ALL SELECT 'satisfaction_score', COUNT(*), ROUND(AVG(satisfaction_score)::numeric,2),
       ROUND(STDDEV(satisfaction_score)::numeric,2), MIN(satisfaction_score),
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY satisfaction_score)::numeric,2),
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY satisfaction_score)::numeric,2),
       ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY satisfaction_score)::numeric,2),
       MAX(satisfaction_score)
FROM gold.fact_billing_support
""",
    "EDA · Histogram total_bill_amount": """
SELECT
    WIDTH_BUCKET(total_bill_amount, 20, 150, 13) AS bucket,
    ROUND(MIN(total_bill_amount)::numeric, 1)    AS bucket_from,
    ROUND(MAX(total_bill_amount)::numeric, 1)    AS bucket_to,
    COUNT(*)                                     AS cycles,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM gold.fact_billing_support
GROUP BY 1
ORDER BY 1
""",
    "EDA · Histogram data_usage_gb": """
SELECT
    WIDTH_BUCKET(data_usage_gb, 0, 100, 10) AS bucket,
    ROUND(MIN(data_usage_gb)::numeric, 1)   AS bucket_from,
    ROUND(MAX(data_usage_gb)::numeric, 1)   AS bucket_to,
    COUNT(*)                                AS cycles,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM gold.fact_billing_support
GROUP BY 1
ORDER BY 1
""",
    "EDA · Outlier check (IQR)": """
WITH s AS (
    SELECT
        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY total_bill_amount) AS q1,
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY total_bill_amount) AS q3
    FROM gold.fact_billing_support
)
SELECT
    'total_bill_amount' AS measure,
    ROUND(q1::numeric, 2) AS q1,
    ROUND(q3::numeric, 2) AS q3,
    ROUND((q3 - q1)::numeric, 2) AS iqr,
    ROUND((q1 - 1.5 * (q3 - q1))::numeric, 2) AS lower_fence,
    ROUND((q3 + 1.5 * (q3 - q1))::numeric, 2) AS upper_fence,
    (SELECT COUNT(*) FROM gold.fact_billing_support
      WHERE total_bill_amount < q1 - 1.5 * (q3 - q1)
         OR total_bill_amount > q3 + 1.5 * (q3 - q1)) AS outliers
FROM s
""",
    "EDA · Tương quan measure vs churn": """
-- ORDER BY on an expression is not allowed directly after UNION, so the
-- union is wrapped in a subquery and sorted outside.
SELECT measure, corr_with_churn
FROM (
    SELECT 'call_minutes' AS measure,
           ROUND(CORR(call_minutes::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4) AS corr_with_churn
    FROM gold.fact_billing_support
    UNION ALL SELECT 'data_usage_gb',
           ROUND(CORR(data_usage_gb::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4)
    FROM gold.fact_billing_support
    UNION ALL SELECT 'sms_count',
           ROUND(CORR(sms_count::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4)
    FROM gold.fact_billing_support
    UNION ALL SELECT 'internet_sessions',
           ROUND(CORR(internet_sessions::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4)
    FROM gold.fact_billing_support
    UNION ALL SELECT 'total_bill_amount',
           ROUND(CORR(total_bill_amount::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4)
    FROM gold.fact_billing_support
    UNION ALL SELECT 'late_fee',
           ROUND(CORR(late_fee::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4)
    FROM gold.fact_billing_support
    UNION ALL SELECT 'discounts_applied',
           ROUND(CORR(discounts_applied::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4)
    FROM gold.fact_billing_support
    UNION ALL SELECT 'support_interaction_count',
           ROUND(CORR(support_interaction_count::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4)
    FROM gold.fact_billing_support
    UNION ALL SELECT 'resolution_time_minutes',
           ROUND(CORR(resolution_time_minutes::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4)
    FROM gold.fact_billing_support
    UNION ALL SELECT 'satisfaction_score',
           ROUND(CORR(satisfaction_score::numeric, CASE WHEN churn_flag THEN 1 ELSE 0 END)::numeric, 4)
    FROM gold.fact_billing_support
) t
ORDER BY ABS(corr_with_churn) DESC
""",
    "EDA · Missing / sentinel audit": """
SELECT 'primary_issue_type IS NULL' AS check_name,
       COUNT(*) AS rows,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM gold.fact_billing_support), 2) AS pct
FROM gold.fact_billing_support WHERE issue_type_key = 4
UNION ALL
SELECT 'support_interaction_count = 0', COUNT(*),
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM gold.fact_billing_support), 2)
FROM gold.fact_billing_support WHERE support_interaction_count = 0
UNION ALL
SELECT 'satisfaction_score = 5 (sentinel?)', COUNT(*),
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM gold.fact_billing_support), 2)
FROM gold.fact_billing_support WHERE satisfaction_score = 5
UNION ALL
SELECT 'resolution_time_minutes = 0', COUNT(*),
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM gold.fact_billing_support), 2)
FROM gold.fact_billing_support WHERE resolution_time_minutes = 0
UNION ALL
SELECT 'last_interaction_date_key = 0', COUNT(*),
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM gold.fact_billing_support), 2)
FROM gold.fact_billing_support WHERE last_interaction_date_key = 0
ORDER BY rows DESC
""",
    "EDA · Cardinality các dimension": """
SELECT 'dim_customer' AS dimension, COUNT(*) AS members FROM gold.dim_customer
UNION ALL SELECT 'dim_date', COUNT(*) FROM gold.dim_date
UNION ALL SELECT 'dim_payment_status', COUNT(*) FROM gold.dim_payment_status
UNION ALL SELECT 'dim_issue_type', COUNT(*) FROM gold.dim_issue_type
UNION ALL SELECT 'dim_support_channel', COUNT(*) FROM gold.dim_support_channel
ORDER BY members DESC
""",
    "EDA · Phân bố số cycle mỗi khách": """
SELECT
    cycles_per_customer,
    COUNT(*) AS customers,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM (
    SELECT customer_key, COUNT(*) AS cycles_per_customer
    FROM gold.fact_billing_support
    GROUP BY 1
) t
GROUP BY 1
ORDER BY 1
""",
}


# ---------------------------------------------------------------------------
# Which filters each question accepts (drives template-tags generation)
# ---------------------------------------------------------------------------

CARD_FILTERS: dict[str, list[str]] = {
    "Cohort retention & ARPU": ["date"],
    "Doanh thu theo tháng": ["date"],
    "ARPU theo tháng": ["date"],
    "Tỷ lệ quá hạn": ["date"],
    "Discount & late fee (% doanh thu)": ["date"],
    "Khách không liên hệ support": ["date"],
    "Tickets theo kênh": ["date", "channel", "issue"],
    "Thời gian xử lý theo kênh": ["date", "channel", "issue"],
    "Escalation rate theo kênh": ["date", "channel", "issue"],
    "CSAT theo issue × channel": ["date", "channel", "issue"],
    "Churn theo payment status": ["payment"],
    "MART · billing_monthly — toàn cảnh": ["date"],
    "MART · support_monthly — toàn cảnh": ["date", "channel", "issue"],
    "MART · churn_summary — toàn cảnh": ["payment", "issue", "channel"],
    "MART · churn_summary — churn rate đúng cách": ["payment", "issue", "channel"],
}


# ---------------------------------------------------------------------------
# Visualization settings
# ---------------------------------------------------------------------------

VIZ: dict[str, tuple[str, dict]] = {
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
    # OLAP
    "OLAP-1 · ROLL-UP (year → quarter → month)": ("table", {}),
    "OLAP-2 · DRILL-DOWN (year → month)": (
        "bar",
        {"graph.dimensions": ["year", "month"], "graph.metrics": ["revenue"]},
    ),
    "OLAP-3 · SLICE (payment_status = Paid)": (
        "bar",
        {"graph.dimensions": ["issue_type"], "graph.metrics": ["revenue"]},
    ),
    "OLAP-4 · DICE (year=2022 × Paid/Unpaid × Billing)": (
        "bar",
        {"graph.dimensions": ["quarter", "payment_status"], "graph.metrics": ["revenue"]},
    ),
    "OLAP-5 · PIVOT (issue × channel churn)": ("table", {}),
    "OLAP-6 · CUBE (issue × channel × payment)": ("table", {}),
    # Mart demos
    "MART · billing_monthly — toàn cảnh": ("table", {}),
    "MART · support_monthly — toàn cảnh": ("table", {}),
    "MART · churn_summary — toàn cảnh": ("table", {}),
    "MART · churn_summary — churn rate đúng cách": (
        "bar",
        {"graph.dimensions": ["issue_type"], "graph.metrics": ["churn_rate"]},
    ),
    # EDA
    "EDA · Thống kê mô tả 10 measure": ("table", {}),
    "EDA · Histogram total_bill_amount": (
        "bar",
        {"graph.dimensions": ["bucket"], "graph.metrics": ["cycles"]},
    ),
    "EDA · Histogram data_usage_gb": (
        "bar",
        {"graph.dimensions": ["bucket"], "graph.metrics": ["cycles"]},
    ),
    "EDA · Outlier check (IQR)": ("table", {}),
    "EDA · Tương quan measure vs churn": (
        "bar",
        {"graph.dimensions": ["measure"], "graph.metrics": ["corr_with_churn"]},
    ),
    "EDA · Missing / sentinel audit": ("table", {}),
    "EDA · Cardinality các dimension": (
        "bar",
        {"graph.dimensions": ["dimension"], "graph.metrics": ["members"]},
    ),
    "EDA · Phân bố số cycle mỗi khách": (
        "bar",
        {"graph.dimensions": ["cycles_per_customer"], "graph.metrics": ["customers"]},
    ),
}


# ---------------------------------------------------------------------------
# Dashboard definitions
#
# layout item: (question name, row, col, size_x, size_y)
# The filters a card responds to come from CARD_FILTERS, not from the layout.
# ---------------------------------------------------------------------------

DASHBOARDS: dict[str, dict] = {
    "1 · Overview — Churn Story": {
        "description": (
            "Bức tranh tổng thể: cohort co lại, churn đều giữa issue/kênh, "
            "nghịch lý im lặng. Dùng slider thời gian để xem từng giai đoạn."
        ),
        "filters": ["date"],
        "layout": [
            ("KPI · Tổng quan", 0, 0, 24, 4),
            ("Cohort retention & ARPU", 4, 0, 24, 8),
            ("Churn theo issue × channel", 12, 0, 12, 8),
            ("Im lặng vs có liên hệ", 12, 12, 12, 8),
            ("Churn theo số lần liên hệ", 20, 0, 24, 8),
        ],
    },
    "2 · Revenue & Billing": {
        "description": (
            "Doanh thu, ARPU, quá hạn, discount. Dùng gold.mv_billing_monthly "
            "(materialized). total_revenue giảm do cohort co lại, không phải "
            "do giá — luôn đọc kèm ARPU."
        ),
        "filters": ["date"],
        "layout": [
            ("Doanh thu theo tháng", 0, 0, 12, 8),
            ("ARPU theo tháng", 0, 12, 12, 8),
            ("Tỷ lệ quá hạn", 8, 0, 12, 8),
            ("Discount & late fee (% doanh thu)", 8, 12, 12, 8),
            ("Khách không liên hệ support", 16, 0, 24, 8),
        ],
    },
    "3 · Support Ops": {
        "description": (
            "Hiệu suất support theo kênh và issue. Dùng gold.mart_support_monthly "
            "(đã loại 18.719 kỳ không liên hệ). tickets = số billing cycle có "
            "liên hệ, không phải số lần liên hệ."
        ),
        "filters": ["date", "channel", "issue"],
        "layout": [
            ("Tickets theo kênh", 0, 0, 12, 8),
            ("Thời gian xử lý theo kênh", 0, 12, 12, 8),
            ("Escalation rate theo kênh", 8, 0, 12, 8),
            ("Phân bố CSAT", 8, 12, 12, 8),
            ("CSAT theo issue × channel", 16, 0, 24, 8),
        ],
    },
    "4 · Churn Risk": {
        "description": (
            "Tín hiệu rủi ro churn xếp theo lift. Escalation 1,60× > Payment "
            "Unpaid 1,55× > CSAT≤2 1,43× > Interactions≥3 1,23× > Có liên hệ 1,13×."
        ),
        "filters": ["payment", "issue", "channel"],
        "layout": [
            ("Bảng xếp hạng tín hiệu (lift)", 0, 0, 24, 8),
            ("Churn theo payment status", 8, 0, 8, 8),
            ("Churn theo escalation", 8, 8, 8, 8),
            ("Churn theo CSAT", 8, 16, 8, 8),
            ("Cảnh báo sớm: liên hệ kỳ t → churn kỳ t+1", 16, 0, 12, 8),
            ("Chân dung khách churn", 16, 12, 12, 8),
        ],
    },
    "5 · OLAP Lab — 6 kỹ thuật": {
        "description": (
            "Mỗi card là một kỹ thuật OLAP chạy được: ROLL-UP, DRILL-DOWN, "
            "SLICE, DICE, PIVOT, CUBE. GROUPING() phân biệt NULL thật với "
            "NULL sinh ra bởi subtotal."
        ),
        "filters": [],
        "layout": [
            ("OLAP-1 · ROLL-UP (year → quarter → month)", 0, 0, 24, 10),
            ("OLAP-2 · DRILL-DOWN (year → month)", 10, 0, 12, 8),
            ("OLAP-3 · SLICE (payment_status = Paid)", 10, 12, 12, 8),
            ("OLAP-4 · DICE (year=2022 × Paid/Unpaid × Billing)", 18, 0, 12, 8),
            ("OLAP-5 · PIVOT (issue × channel churn)", 18, 12, 12, 8),
            ("OLAP-6 · CUBE (issue × channel × payment)", 26, 0, 24, 10),
        ],
    },
    "6 · Mart Demo — billing_monthly": {
        "description": (
            "Demo mart_billing_monthly: grain 1 dòng/tháng, phục vụ CFO. "
            "Cùng nội dung với mv_billing_monthly nhưng là view (luôn tươi)."
        ),
        "filters": ["date"],
        "layout": [
            ("MART · billing_monthly — toàn cảnh", 0, 0, 24, 10),
            ("Doanh thu theo tháng", 10, 0, 12, 8),
            ("ARPU theo tháng", 10, 12, 12, 8),
            ("Tỷ lệ quá hạn", 18, 0, 12, 8),
            ("Khách không liên hệ support", 18, 12, 12, 8),
        ],
    },
    "7 · Mart Demo — support_monthly": {
        "description": (
            "Demo mart_support_monthly: grain 1 dòng/(tháng × kênh × issue), "
            "phục vụ Head of Support. Đã loại 18.719 kỳ không liên hệ để "
            "CSAT và resolution không bị sentinel làm lệch."
        ),
        "filters": ["date", "channel", "issue"],
        "layout": [
            ("MART · support_monthly — toàn cảnh", 0, 0, 24, 10),
            ("Tickets theo kênh", 10, 0, 12, 8),
            ("Thời gian xử lý theo kênh", 10, 12, 12, 8),
            ("Escalation rate theo kênh", 18, 0, 12, 8),
            ("CSAT theo issue × channel", 18, 12, 12, 8),
        ],
    },
    "8 · Mart Demo — churn_summary": {
        "description": (
            "Demo mart_churn_summary: grain 1 dòng/(churn × payment × issue × "
            "channel). LƯU Ý: cột churn_rate của mart luôn 0 hoặc 100 vì "
            "GROUP BY churn_flag — card cuối tự tính lại đúng cách."
        ),
        "filters": ["payment", "issue", "channel"],
        "layout": [
            ("MART · churn_summary — toàn cảnh", 0, 0, 24, 10),
            ("MART · churn_summary — churn rate đúng cách", 10, 0, 24, 8),
            ("Churn theo payment status", 18, 0, 12, 8),
            ("Chân dung khách churn", 18, 12, 12, 8),
        ],
    },
    "9 · EDA — Khám phá dữ liệu": {
        "description": (
            "Phân tích khám phá: phân bố, outlier, tương quan, sentinel. "
            "Đây là phần trả lời 'dữ liệu này trông thế nào' trước khi tin "
            "vào bất kỳ insight nào."
        ),
        "filters": [],
        "layout": [
            ("EDA · Thống kê mô tả 10 measure", 0, 0, 24, 10),
            ("EDA · Histogram total_bill_amount", 10, 0, 12, 8),
            ("EDA · Histogram data_usage_gb", 10, 12, 12, 8),
            ("EDA · Tương quan measure vs churn", 18, 0, 12, 8),
            ("EDA · Outlier check (IQR)", 18, 12, 12, 8),
            ("EDA · Missing / sentinel audit", 26, 0, 12, 8),
            ("EDA · Cardinality các dimension", 26, 12, 12, 8),
            ("EDA · Phân bố số cycle mỗi khách", 34, 0, 24, 8),
        ],
    },
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
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
            "Then re-run:  make metabase-dashboards",
            file=sys.stderr,
        )
        return 2

    mb = Metabase(args.url, args.user, args.password)
    database_id = mb.find_database(args.database)
    collection_id = mb.ensure_collection(args.collection)

    # ---- 1. questions ----------------------------------------------------
    print("\n=== Questions ===")
    card_ids: dict[str, int] = {}
    for name, sql in Q.items():
        display, viz = VIZ.get(name, ("table", {}))
        filters = CARD_FILTERS.get(name, [])
        tags = template_tags_for(filters) if filters else {}
        card_ids[name] = mb.upsert_card(
            name=name,
            sql=sql.strip(),
            display=display,
            database_id=database_id,
            collection_id=collection_id,
            viz=viz,
            template_tags=tags,
        )

    # ---- 2. dashboards ---------------------------------------------------
    print("\n=== Dashboards ===")
    for dash_name, spec in DASHBOARDS.items():
        dash_id = mb.ensure_dashboard(dash_name, spec["description"], collection_id)
        params, param_ids = build_parameters(spec["filters"])

        layout = []
        for (qname, row, col, sx, sy) in spec["layout"]:
            card_filters = CARD_FILTERS.get(qname, [])
            # Only map filters this card declares AND the dashboard offers.
            usable = [k for k in card_filters if k in param_ids]
            layout.append(
                {
                    "card_id": card_ids[qname],
                    "row": row,
                    "col": col,
                    "size_x": sx,
                    "size_y": sy,
                    "parameter_mappings": parameter_mappings(
                        usable, param_ids, card_ids[qname]
                    ),
                }
            )

        mb.set_dashboard_cards(dash_id, layout, params)

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
