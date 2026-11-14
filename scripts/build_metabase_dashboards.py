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
    # DSS what-if inputs: plain values, not row filters. They feed the
    # arguments of gold.fn_retention_plan(), so each one needs a default.
    "dss_month": ("f_dss_month", "Tháng lập kế hoạch", "date/single", "dss_month"),
    "budget": ("f_budget", "Ngân sách", "number/=", "budget"),
    "offer_cost": ("f_offer_cost", "Chi phí 1 ưu đãi", "number/=", "offer_cost"),
    "save_rate": ("f_save_rate", "Tỷ lệ giữ chân thành công", "number/=", "save_rate"),
}

FILTER_DEFAULTS: dict[str, Any] = {
    "dss_month": "2020-04-01",
    "budget": 20000,
    "offer_cost": 10,
    "save_rate": 0.3,
}


def _tag_type(ptype: str) -> str:
    if ptype.startswith("number"):
        return "number"
    if ptype.startswith("date"):
        return "date"
    return "text"

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
        param = {
            "id": pid,
            "name": name,
            "slug": pid.replace("f_", ""),
            "type": ptype,
            "sectionId": {"number": "number", "date": "date"}.get(_tag_type(ptype), "category"),
        }
        if key in FILTER_DEFAULTS:
            param["default"] = FILTER_DEFAULTS[key]
        params.append(param)
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
                "type": _tag_type(FILTERS[key][2]),
            }
            if key in FILTER_DEFAULTS:
                tags[tag]["default"] = FILTER_DEFAULTS[key]
                tags[tag]["required"] = True
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
    "Tổng quan: quy mô & churn": """
SELECT
    COUNT(*)                                              AS billing_cycles,
    COUNT(DISTINCT customer_key)                          AS customers,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate_pct,
    ROUND(AVG(total_bill_amount)::numeric, 2)             AS avg_bill,
    ROUND(100.0 * COUNT(*) FILTER (WHERE support_interaction_count = 0)
          / COUNT(*), 2)                                  AS silent_pct
FROM gold.fact_billing_support
""",
    "Tỷ lệ khách còn lại & ARPU theo tháng": """
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
    "Tỷ lệ churn theo loại vấn đề và kênh": """
SELECT issue_type, support_channel, churn_rate
FROM gold.mv_churn_by_issue_channel
ORDER BY issue_type, support_channel
""",
    "Churn: khách im lặng vs có liên hệ": """
SELECT
    CASE WHEN support_interaction_count = 0
         THEN 'Không liên hệ' ELSE 'Có liên hệ' END        AS segment,
    COUNT(*)                                               AS cycles,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
GROUP BY 1
ORDER BY churn_rate
""",
    "Churn tăng dần theo số lần liên hệ": """
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
    "Tỷ lệ quá hạn thanh toán": """
SELECT month_start, overdue_rate
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    "Giảm giá & phí phạt (% doanh thu)": """
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
    "Số khách không liên hệ support": """
SELECT month_start, no_support_customers
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    # ---------------- Support Ops ----------------
    "Số ticket theo kênh": """
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
    "Thời gian xử lý TB theo kênh": """
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
    "Phân bố điểm hài lòng (CSAT)": """
SELECT
    satisfaction_score,
    COUNT(*)                                           AS cycles,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1
ORDER BY 1
""",
    "Tỷ lệ escalate theo kênh": """
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
    "Điểm hài lòng theo loại vấn đề và kênh": """
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
    "Churn theo trạng thái thanh toán": """
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
    "Churn theo điểm hài lòng": """
SELECT
    satisfaction_score,
    COUNT(*) AS cycles,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1
ORDER BY 1
""",
    "Churn: có escalate vs không escalate": """
SELECT
    CASE WHEN escalation_flag THEN 'Có escalate' ELSE 'Không' END AS escalation,
    COUNT(*) AS cycles,
    ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1
ORDER BY churn_rate DESC
""",
    "Liên hệ kỳ này có báo trước churn kỳ sau?": """
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
    "Khách churn khác khách ở lại thế nào?": """
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
    "Xếp hạng tín hiệu rủi ro (lift)": """
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
    "OLAP 1 · Roll-up: năm → quý → tháng": """
SELECT
    d.year::text AS year, d.quarter, d.month,
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
    "OLAP 2 · Drill-down: năm → tháng": """
SELECT
    CASE GROUPING(d.year, d.month)
        WHEN 0 THEN '2. month detail'
        WHEN 1 THEN '1. year total'
    END                                  AS drill_level,
    d.year::text AS year, d.month,
    COUNT(*)                             AS billing_cycles,
    SUM(f.total_bill_amount)             AS revenue,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.churn_flag) / COUNT(*), 2) AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_date d ON d.date_key = f.billing_start_date_key
WHERE d.is_unknown = FALSE
GROUP BY GROUPING SETS ((d.year), (d.year, d.month))
ORDER BY d.year, GROUPING(d.year, d.month), d.month
""",
    "OLAP 3 · Slice: chỉ kỳ đã thanh toán": """
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
    "OLAP 4 · Dice: 2022 × Paid/Unpaid × Billing": """
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
    "OLAP 5 · Pivot: churn theo vấn đề × kênh": """
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
    "OLAP 6 · Cube: vấn đề × kênh × thanh toán": """
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
    "Mart billing_monthly: bảng đầy đủ": """
SELECT
    month_start, billing_cycles, active_customers, total_revenue, arpu,
    overdue_rate, no_support_customers, churn_rate
FROM gold.mart_billing_monthly
WHERE 1 = 1
[[AND month_start >= {{d_start}}::date]]
[[AND month_start <= {{d_end}}::date]]
ORDER BY month_start
""",
    "Mart support_monthly: bảng đầy đủ": """
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
    "Mart churn_summary: bảng đầy đủ": """
SELECT
    payment_status, issue_type, support_channel,
    billing_cycles, churned_cycles, customers, churned_customers,
    avg_bill, avg_csat, avg_interactions, avg_resolution, churn_rate
FROM gold.mart_churn_summary
WHERE 1 = 1
[[AND payment_status = {{payment}}]]
[[AND issue_type = {{issue}}]]
[[AND support_channel = {{channel}}]]
ORDER BY billing_cycles DESC
""",
    "Mart churn_summary: churn rate tính lại đúng": """
-- churn_flag là measure của mart (không nằm trong grain), nên churn_rate
-- đọc thẳng được; card này chỉ sắp xếp lại theo mức rủi ro.
SELECT
    payment_status, issue_type, support_channel,
    billing_cycles AS cycles, churned_cycles AS churned, churn_rate
FROM gold.mart_churn_summary
WHERE 1 = 1
[[AND payment_status = {{payment}}]]
[[AND issue_type = {{issue}}]]
[[AND support_channel = {{channel}}]]
ORDER BY churn_rate DESC
""",
    # =====================================================================
    # DSS — retention planner (sql/06-07, src/06_dss_model.py)
    # =====================================================================
    "DSS: tóm tắt kế hoạch giữ chân": """
SELECT *
FROM gold.fn_retention_plan_summary(
    {{dss_month}}::date, {{budget}}, {{offer_cost}}, {{save_rate}}
)
""",
    "DSS: danh sách khách ưu tiên": """
SELECT priority, customer_id, risk_band, p_churn, bill,
       value_if_retained, expected_gain, cumulative_cost
FROM gold.fn_retention_plan(
    {{dss_month}}::date, {{budget}}, {{offer_cost}}, {{save_rate}}
)
WHERE selected
ORDER BY priority
LIMIT 500
""",
    "DSS: khách theo nhóm rủi ro mỗi tháng": """
SELECT month_start, risk_band, customers
FROM gold.dss_risk_band_monthly
WHERE month_start >= DATE '2020-04-01'
  AND month_start <  DATE '2021-01-01'
ORDER BY month_start, risk_band
""",
    "DSS: dự báo so với thực tế theo nhóm rủi ro": """
SELECT risk_band,
       COUNT(*)                                                     AS cycles,
       ROUND(100 * AVG(p_churn)::numeric, 2)                        AS predicted_pct,
       ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS actual_pct
FROM gold.dss_churn_score
WHERE split = 'test'
GROUP BY risk_band
ORDER BY predicted_pct DESC
""",
    "DSS: so sánh mô hình trên giai đoạn kiểm tra": """
SELECT model, auc, lift_top10, recall_top10, lift_top20, recall_top20
FROM gold.dss_model_eval
ORDER BY auc DESC
""",
    "DSS: yếu tố ảnh hưởng (hệ số chuẩn hoá)": """
SELECT feature, coef_std, odds_ratio_per_sd
FROM gold.dss_model_coef
ORDER BY ABS(coef_std) DESC
LIMIT 15
""",
    "DSS: tỷ lệ bắt được khách churn theo ngân sách": """
SELECT ROUND(100 * budget_share::numeric)::int AS pct_khach_duoc_tiep_can,
       strategy,
       ROUND(100 * recall::numeric, 1) AS pct_churn_bat_duoc
FROM gold.dss_policy_backtest
ORDER BY budget_share, strategy
""",
    # =====================================================================
    # EDA
    # =====================================================================
    "EDA: thống kê mô tả 10 chỉ số": """
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
    "EDA: phân bố hoá đơn": """
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
    "EDA: phân bố dung lượng data": """
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
    "EDA: kiểm tra giá trị ngoại lai (IQR)": """
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
    "EDA: chỉ số nào tương quan với churn": """
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
    "EDA: kiểm tra dữ liệu thiếu & sentinel": """
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
    "EDA: số lượng thành viên mỗi dimension": """
SELECT 'dim_customer' AS dimension, COUNT(*) AS members FROM gold.dim_customer
UNION ALL SELECT 'dim_date', COUNT(*) FROM gold.dim_date
UNION ALL SELECT 'dim_payment_status', COUNT(*) FROM gold.dim_payment_status
UNION ALL SELECT 'dim_issue_type', COUNT(*) FROM gold.dim_issue_type
UNION ALL SELECT 'dim_support_channel', COUNT(*) FROM gold.dim_support_channel
ORDER BY members DESC
""",
    "EDA: mỗi khách có bao nhiêu kỳ": """
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
# One-line descriptions shown under each question title in Metabase.
# They state what the chart answers and, where it matters, the caveat.
# ---------------------------------------------------------------------------

DESCRIPTIONS: dict[str, str] = {
    "DSS: tóm tắt kế hoạch giữ chân":
        "Chi phí, số khách được chọn, giá trị kỳ vọng và ROI của kế hoạch. "
        "Hai cột cuối là backtest: trong số khách được chọn, bao nhiêu thật sự churn.",
    "DSS: danh sách khách ưu tiên":
        "Khách được chọn theo expected_gain giảm dần cho tới khi hết ngân sách. "
        "Đổi ngân sách, chi phí ưu đãi hoặc tỷ lệ giữ chân để xem danh sách thay đổi.",
    "DSS: dự báo so với thực tế theo nhóm rủi ro":
        "Kiểm tra hiệu chuẩn: xác suất mô hình dự báo so với tỷ lệ churn thật, "
        "chỉ trên giai đoạn kiểm tra (từ 04/2020).",
    "DSS: tỷ lệ bắt được khách churn theo ngân sách":
        "Backtest: nếu mỗi tháng tiếp cận k% khách, mỗi chiến lược bắt được bao nhiêu % "
        "khách churn. random là kỳ vọng khi chọn ngẫu nhiên.",
    # --- Overview ---
    "Tổng quan: quy mô & churn":
        "112.552 kỳ thanh toán của 27.778 khách. Churn 24,68% là tỷ lệ theo KỲ, "
        "không phải tỷ lệ khách hàng.",
    "Tỷ lệ khách còn lại & ARPU theo tháng":
        "Cột: % số khách còn hoạt động so với tháng đầu. Đường: ARPU. "
        "ARPU gần như phẳng (~85) chứng tỏ mất khách chứ không mất giá.",
    "Tỷ lệ churn theo loại vấn đề và kênh":
        "9 tổ hợp đều 27–29%: không loại vấn đề hay kênh nào nổi trội. "
        "Ô No Support × None (8,20%) là ngoại lệ duy nhất.",
    "Churn: khách im lặng vs có liên hệ":
        "Kỳ không liên hệ support churn 8,20%; kỳ có liên hệ 27,97% — gấp 3,4 lần. "
        "Ngược với giả định 'khách im lặng mới nguy hiểm'.",
    "Churn tăng dần theo số lần liên hệ":
        "8,20% (0 lần) → 33,36% (5 lần). Mỗi lần liên hệ thêm làm tăng rủi ro "
        "2–3 điểm %, tức là ma sát tích luỹ chứ không phải tín hiệu huỷ đơn thuần.",
    # --- Revenue & Billing ---
    "Doanh thu theo tháng":
        "Tổng hoá đơn theo tháng. Giảm mạnh vì số khách giảm dần (cohort đóng), "
        "không phải vì giá — luôn đọc kèm ARPU.",
    "ARPU theo tháng":
        "Doanh thu bình quân trên mỗi khách hoạt động. Ổn định 83–87 ở các tháng "
        "đủ mẫu; tháng cuối chỉ 1 khách nên bị nhiễu.",
    "Tỷ lệ quá hạn thanh toán":
        "% kỳ có trạng thái Unpaid hoặc Partial. Dao động quanh 20%, khá ổn định.",
    "Giảm giá & phí phạt (% doanh thu)":
        "Hai khoản chiết khấu tính theo % doanh thu. Giảm giá ~11,7% doanh thu, "
        "gấp ~5,8 lần phí phạt — khoản chi lớn chưa chứng minh hiệu quả giữ chân.",
    "Số khách không liên hệ support":
        "Số kỳ khách không liên hệ support trong tháng. Đây là nhóm 'im lặng' "
        "có churn thấp nhưng chưa được tiếp cận.",
    # --- Support Ops ---
    "Số ticket theo kênh":
        "Số kỳ có liên hệ support, chia theo kênh. Lưu ý: đây là số KỲ, "
        "không phải số lần liên hệ (xem 'Số lần liên hệ TB').",
    "Thời gian xử lý TB theo kênh":
        "Thời gian xử lý trung bình, weighted theo số ticket. Ba kênh gần như "
        "bằng nhau (65,03–65,13 phút) nên tốc độ không phải đòn bẩy giữ chân.",
    "Tỷ lệ escalate theo kênh":
        "% ticket bị escalate. Khoảng 10% ở cả ba kênh.",
    "Phân bố điểm hài lòng (CSAT)":
        "Chỉ tính kỳ CÓ liên hệ. Phân bố gần như đều 20% mỗi mức 1–5. "
        "Nếu tính cả kỳ không liên hệ, mức 5 sẽ bị thổi lên gấp đôi.",
    "Điểm hài lòng theo loại vấn đề và kênh":
        "CSAT trung bình (weighted theo ticket) cho từng tổ hợp. "
        "Chênh lệch giữa các ô rất nhỏ.",
    # --- Churn Risk ---
    "Xếp hạng tín hiệu rủi ro (lift)":
        "Lift = churn của nhóm phơi nhiễm ÷ churn nền 24,68%. "
        "Escalation 1,60× > Unpaid 1,55× > CSAT≤2 1,43× > Liên hệ ≥3 lần 1,23×.",
    "Churn theo trạng thái thanh toán":
        "Unpaid 38,14% > Partial 30,34% > Paid 22,31%. Đây là tín hiệu mạnh nhất "
        "và có sẵn trong billing, không cần survey.",
    "Churn: có escalate vs không escalate":
        "39,52% vs 26,69% (lift 1,48×). Escalation chỉ chiếm ~10% ticket nên "
        "là tín hiệu tầng 2 lý tưởng cho retention.",
    "Churn theo điểm hài lòng":
        "Giảm đơn điệu: 37,97% (CSAT 1) → 18,53% (CSAT 5). "
        "Chỉ tính kỳ có liên hệ.",
    "Liên hệ kỳ này có báo trước churn kỳ sau?":
        "24,73% (có liên hệ) vs 24,39% (không) — lift 1,014×, gần như bằng 1. "
        "Kết luận: liên hệ là TRIỆU CHỨNG cùng kỳ, không phải nguyên nhân.",
    "Khách churn khác khách ở lại thế nào?":
        "So sánh 5 chỉ số bình quân. Khách churn: dùng ít dịch vụ hơn, "
        "phí phạt cao hơn 73,5%, liên hệ support nhiều hơn 28,2%.",
    # --- OLAP ---
    "OLAP 1 · Roll-up: năm → quý → tháng":
        "GROUP BY ROLLUP(year, quarter, month). GROUPING() phân biệt NULL thật "
        "với NULL sinh ra bởi dòng subtotal.",
    "OLAP 2 · Drill-down: năm → tháng":
        "GROUPING SETS ((year), (year, month)) — hai mức zoom trong một kết quả. "
        "Cột drill_level cho biết dòng nào là tổng năm, dòng nào là chi tiết tháng.",
    "OLAP 3 · Slice: chỉ kỳ đã thanh toán":
        "Cố định một chiều (payment_status = 'Paid'), tổng hợp các chiều còn lại "
        "theo loại vấn đề và kênh.",
    "OLAP 4 · Dice: 2022 × Paid/Unpaid × Billing":
        "Lọc đồng thời ba chiều: năm = 2022, trạng thái ∈ {Paid, Unpaid}, "
        "vấn đề = Billing.",
    "OLAP 5 · Pivot: churn theo vấn đề × kênh":
        "Crosstab bằng conditional aggregation: hàng = loại vấn đề, "
        "cột = kênh. Loại 'No Support' vì không có kênh.",
    "OLAP 6 · Cube: vấn đề × kênh × thanh toán":
        "GROUP BY CUBE sinh mọi tổ hợp subtotal (3 chiều, 2 chiều, 1 chiều, tổng). "
        "Cột grouping_level cho biết chiều nào đã bị gộp.",
    # --- Mart demos ---
    "Mart billing_monthly: bảng đầy đủ":
        "Toàn bộ cột của mart_billing_monthly (grain: 1 dòng/tháng). "
        "Đây là view nên luôn phản ánh dữ liệu mới nhất.",
    "Mart support_monthly: bảng đầy đủ":
        "Toàn bộ cột của mart_support_monthly (grain: 1 dòng/tháng × kênh × vấn đề). "
        "Đã loại 18.719 kỳ không liên hệ để CSAT không bị sentinel làm lệch.",
    "Mart churn_summary: bảng đầy đủ":
        "Toàn bộ cột của mart_churn_summary (grain: thanh toán × vấn đề × kênh). "
        "churn_flag là measure nên churn_rate đọc trực tiếp được.",
    "Mart churn_summary: churn rate tính lại đúng":
        "Các segment xếp theo churn_rate giảm dần, lọc được theo thanh toán, "
        "vấn đề và kênh.",
    # --- EDA ---
    "EDA: thống kê mô tả 10 chỉ số":
        "n, mean, sd, min, p25, median, p75, max cho 10 measure. "
        "Dùng để phát hiện phân bố lệch và thang đo trước khi phân tích.",
    "EDA: phân bố hoá đơn":
        "Histogram total_bill_amount chia 13 khoảng đều từ 20 đến 150.",
    "EDA: phân bố dung lượng data":
        "Histogram data_usage_gb chia 10 khoảng đều từ 0 đến 100 GB.",
    "EDA: kiểm tra giá trị ngoại lai (IQR)":
        "Q1, Q3, IQR và hàng rào 1,5×IQR cho total_bill_amount, "
        "kèm số dòng nằm ngoài hàng rào.",
    "EDA: chỉ số nào tương quan với churn":
        "Hệ số tương quan Pearson giữa từng measure và cờ churn, "
        "sắp xếp theo trị tuyệt đối. Tương quan ≠ nhân quả.",
    "EDA: kiểm tra dữ liệu thiếu & sentinel":
        "Đếm 5 loại giá trị đặc biệt: issue NULL, interaction = 0, CSAT = 5, "
        "resolution = 0, ngày tương tác = key 0. Đây là nguồn gây hiểu nhầm chính.",
    "EDA: số lượng thành viên mỗi dimension":
        "Số dòng của 5 dimension, gồm cả dòng key 0 (Unknown).",
    "EDA: mỗi khách có bao nhiêu kỳ":
        "Phân bố số billing cycle trên mỗi khách (1–36). "
        "Cho thấy cohort co dần chứ không có khách mới.",
}


# ---------------------------------------------------------------------------
# Which filters each question accepts (drives template-tags generation)
# ---------------------------------------------------------------------------

CARD_FILTERS: dict[str, list[str]] = {
    "DSS: tóm tắt kế hoạch giữ chân": ["dss_month", "budget", "offer_cost", "save_rate"],
    "DSS: danh sách khách ưu tiên": ["dss_month", "budget", "offer_cost", "save_rate"],
    "Tỷ lệ khách còn lại & ARPU theo tháng": ["date"],
    "Doanh thu theo tháng": ["date"],
    "ARPU theo tháng": ["date"],
    "Tỷ lệ quá hạn thanh toán": ["date"],
    "Giảm giá & phí phạt (% doanh thu)": ["date"],
    "Số khách không liên hệ support": ["date"],
    "Số ticket theo kênh": ["date", "channel", "issue"],
    "Thời gian xử lý TB theo kênh": ["date", "channel", "issue"],
    "Tỷ lệ escalate theo kênh": ["date", "channel", "issue"],
    "Điểm hài lòng theo loại vấn đề và kênh": ["date", "channel", "issue"],
    "Churn theo trạng thái thanh toán": ["payment"],
    "Mart billing_monthly: bảng đầy đủ": ["date"],
    "Mart support_monthly: bảng đầy đủ": ["date", "channel", "issue"],
    "Mart churn_summary: bảng đầy đủ": ["payment", "issue", "channel"],
    "Mart churn_summary: churn rate tính lại đúng": ["payment", "issue", "channel"],
}


# ---------------------------------------------------------------------------
# Visualization settings
# ---------------------------------------------------------------------------

VIZ: dict[str, tuple[str, dict]] = {
    "DSS: khách theo nhóm rủi ro mỗi tháng": (
        "bar",
        {
            "graph.dimensions": ["month_start", "risk_band"],
            "graph.metrics": ["customers"],
            "stackable.stack_type": "stacked",
        },
    ),
    "DSS: dự báo so với thực tế theo nhóm rủi ro": (
        "bar",
        {"graph.dimensions": ["risk_band"], "graph.metrics": ["predicted_pct", "actual_pct"]},
    ),
    "DSS: yếu tố ảnh hưởng (hệ số chuẩn hoá)": (
        "row",
        {"graph.dimensions": ["feature"], "graph.metrics": ["coef_std"]},
    ),
    "DSS: tỷ lệ bắt được khách churn theo ngân sách": (
        "line",
        {
            "graph.dimensions": ["pct_khach_duoc_tiep_can", "strategy"],
            "graph.metrics": ["pct_churn_bat_duoc"],
        },
    ),
    "Tổng quan: quy mô & churn": ("table", {}),
    "Tỷ lệ khách còn lại & ARPU theo tháng": (
        "line",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["pct_cohort_active", "arpu"]},
    ),
    # Metabase only pivots query-builder questions, so native SQL uses grouped bars.
    "Tỷ lệ churn theo loại vấn đề và kênh": (
        "bar",
        {"graph.dimensions": ["issue_type", "support_channel"], "graph.metrics": ["churn_rate"]},
    ),
    "Churn: khách im lặng vs có liên hệ": (
        "bar",
        {"graph.dimensions": ["segment"], "graph.metrics": ["churn_rate"]},
    ),
    "Churn tăng dần theo số lần liên hệ": (
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
    "Tỷ lệ quá hạn thanh toán": (
        "line",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["overdue_rate"]},
    ),
    "Giảm giá & phí phạt (% doanh thu)": (
        "line",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["discount_pct", "late_fee_pct"]},
    ),
    "Số khách không liên hệ support": (
        "bar",
        {"graph.dimensions": ["month_start"], "graph.metrics": ["no_support_customers"]},
    ),
    "Số ticket theo kênh": (
        "bar",
        {"graph.dimensions": ["support_channel"], "graph.metrics": ["tickets"]},
    ),
    "Thời gian xử lý TB theo kênh": (
        "bar",
        {"graph.dimensions": ["support_channel"], "graph.metrics": ["avg_resolution_min"]},
    ),
    "Phân bố điểm hài lòng (CSAT)": (
        "bar",
        {"graph.dimensions": ["satisfaction_score"], "graph.metrics": ["pct"]},
    ),
    "Tỷ lệ escalate theo kênh": (
        "bar",
        {"graph.dimensions": ["support_channel"], "graph.metrics": ["escalation_rate"]},
    ),
    "Điểm hài lòng theo loại vấn đề và kênh": (
        "bar",
        {"graph.dimensions": ["issue_type", "support_channel"], "graph.metrics": ["csat"]},
    ),
    "Churn theo trạng thái thanh toán": (
        "bar",
        {"graph.dimensions": ["payment_status"], "graph.metrics": ["churn_rate"]},
    ),
    "Churn theo điểm hài lòng": (
        "bar",
        {"graph.dimensions": ["satisfaction_score"], "graph.metrics": ["churn_rate"]},
    ),
    "Churn: có escalate vs không escalate": (
        "bar",
        {"graph.dimensions": ["escalation"], "graph.metrics": ["churn_rate"]},
    ),
    "Liên hệ kỳ này có báo trước churn kỳ sau?": (
        "bar",
        {"graph.dimensions": ["ky_truoc"], "graph.metrics": ["churn_ky_sau"]},
    ),
    "Khách churn khác khách ở lại thế nào?": ("table", {}),
    "Xếp hạng tín hiệu rủi ro (lift)": ("table", {}),
    # OLAP
    "OLAP 1 · Roll-up: năm → quý → tháng": ("table", {}),
    "OLAP 2 · Drill-down: năm → tháng": ("table", {}),
    "OLAP 3 · Slice: chỉ kỳ đã thanh toán": (
        "bar",
        {"graph.dimensions": ["issue_type"], "graph.metrics": ["revenue"]},
    ),
    "OLAP 4 · Dice: 2022 × Paid/Unpaid × Billing": (
        "bar",
        {"graph.dimensions": ["quarter", "payment_status"], "graph.metrics": ["revenue"]},
    ),
    "OLAP 5 · Pivot: churn theo vấn đề × kênh": ("table", {}),
    "OLAP 6 · Cube: vấn đề × kênh × thanh toán": ("table", {}),
    # Mart demos
    "Mart billing_monthly: bảng đầy đủ": ("table", {}),
    "Mart support_monthly: bảng đầy đủ": ("table", {}),
    "Mart churn_summary: bảng đầy đủ": ("table", {}),
    "Mart churn_summary: churn rate tính lại đúng": (
        "bar",
        {"graph.dimensions": ["issue_type"], "graph.metrics": ["churn_rate"]},
    ),
    # EDA
    "EDA: thống kê mô tả 10 chỉ số": ("table", {}),
    "EDA: phân bố hoá đơn": (
        "bar",
        {"graph.dimensions": ["bucket"], "graph.metrics": ["cycles"]},
    ),
    "EDA: phân bố dung lượng data": (
        "bar",
        {"graph.dimensions": ["bucket"], "graph.metrics": ["cycles"]},
    ),
    "EDA: kiểm tra giá trị ngoại lai (IQR)": ("table", {}),
    "EDA: chỉ số nào tương quan với churn": (
        "bar",
        {"graph.dimensions": ["measure"], "graph.metrics": ["corr_with_churn"]},
    ),
    "EDA: kiểm tra dữ liệu thiếu & sentinel": ("table", {}),
    "EDA: số lượng thành viên mỗi dimension": (
        "bar",
        {"graph.dimensions": ["dimension"], "graph.metrics": ["members"]},
    ),
    "EDA: mỗi khách có bao nhiêu kỳ": (
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
            ("Tổng quan: quy mô & churn", 0, 0, 24, 4),
            ("Tỷ lệ khách còn lại & ARPU theo tháng", 4, 0, 24, 8),
            ("Tỷ lệ churn theo loại vấn đề và kênh", 12, 0, 12, 8),
            ("Churn: khách im lặng vs có liên hệ", 12, 12, 12, 8),
            ("Churn tăng dần theo số lần liên hệ", 20, 0, 24, 8),
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
            ("Tỷ lệ quá hạn thanh toán", 8, 0, 12, 8),
            ("Giảm giá & phí phạt (% doanh thu)", 8, 12, 12, 8),
            ("Số khách không liên hệ support", 16, 0, 24, 8),
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
            ("Số ticket theo kênh", 0, 0, 12, 8),
            ("Thời gian xử lý TB theo kênh", 0, 12, 12, 8),
            ("Tỷ lệ escalate theo kênh", 8, 0, 12, 8),
            ("Phân bố điểm hài lòng (CSAT)", 8, 12, 12, 8),
            ("Điểm hài lòng theo loại vấn đề và kênh", 16, 0, 24, 8),
        ],
    },
    "4 · Churn Risk": {
        "description": (
            "Tín hiệu rủi ro churn xếp theo lift. Escalation 1,60× > Payment "
            "Unpaid 1,55× > CSAT≤2 1,43× > Interactions≥3 1,23× > Có liên hệ 1,13×."
        ),
        "filters": ["payment", "issue", "channel"],
        "layout": [
            ("Xếp hạng tín hiệu rủi ro (lift)", 0, 0, 24, 8),
            ("Churn theo trạng thái thanh toán", 8, 0, 8, 8),
            ("Churn: có escalate vs không escalate", 8, 8, 8, 8),
            ("Churn theo điểm hài lòng", 8, 16, 8, 8),
            ("Liên hệ kỳ này có báo trước churn kỳ sau?", 16, 0, 12, 8),
            ("Khách churn khác khách ở lại thế nào?", 16, 12, 12, 8),
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
            ("OLAP 1 · Roll-up: năm → quý → tháng", 0, 0, 24, 10),
            ("OLAP 2 · Drill-down: năm → tháng", 10, 0, 12, 8),
            ("OLAP 3 · Slice: chỉ kỳ đã thanh toán", 10, 12, 12, 8),
            ("OLAP 4 · Dice: 2022 × Paid/Unpaid × Billing", 18, 0, 12, 8),
            ("OLAP 5 · Pivot: churn theo vấn đề × kênh", 18, 12, 12, 8),
            ("OLAP 6 · Cube: vấn đề × kênh × thanh toán", 26, 0, 24, 10),
        ],
    },
    "6 · Mart Demo — billing_monthly": {
        "description": (
            "Demo mart_billing_monthly: grain 1 dòng/tháng, phục vụ CFO. "
            "Cùng nội dung với mv_billing_monthly nhưng là view (luôn tươi)."
        ),
        "filters": ["date"],
        "layout": [
            ("Mart billing_monthly: bảng đầy đủ", 0, 0, 24, 10),
            ("Doanh thu theo tháng", 10, 0, 12, 8),
            ("ARPU theo tháng", 10, 12, 12, 8),
            ("Tỷ lệ quá hạn thanh toán", 18, 0, 12, 8),
            ("Số khách không liên hệ support", 18, 12, 12, 8),
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
            ("Mart support_monthly: bảng đầy đủ", 0, 0, 24, 10),
            ("Số ticket theo kênh", 10, 0, 12, 8),
            ("Thời gian xử lý TB theo kênh", 10, 12, 12, 8),
            ("Tỷ lệ escalate theo kênh", 18, 0, 12, 8),
            ("Điểm hài lòng theo loại vấn đề và kênh", 18, 12, 12, 8),
        ],
    },
    "8 · Mart Demo — churn_summary": {
        "description": (
            "Demo mart_churn_summary: grain 1 dòng/(payment × issue × channel), "
            "churn_flag là measure nên churn_rate đọc thẳng được."
        ),
        "filters": ["payment", "issue", "channel"],
        "layout": [
            ("Mart churn_summary: bảng đầy đủ", 0, 0, 24, 10),
            ("Mart churn_summary: churn rate tính lại đúng", 10, 0, 24, 8),
            ("Churn theo trạng thái thanh toán", 18, 0, 12, 8),
            ("Khách churn khác khách ở lại thế nào?", 18, 12, 12, 8),
        ],
    },
    "10 · DSS — Retention Planner": {
        "description": (
            "Thành phần hỗ trợ quyết định: mô hình hazard chấm điểm rủi ro từng "
            "khách, hàm gold.fn_retention_plan chọn ai nhận ưu đãi trong giới hạn "
            "ngân sách. Đổi 4 tham số phía trên để chạy what-if."
        ),
        "filters": ["dss_month", "budget", "offer_cost", "save_rate"],
        "layout": [
            ("DSS: tóm tắt kế hoạch giữ chân", 0, 0, 24, 4),
            ("DSS: danh sách khách ưu tiên", 4, 0, 14, 10),
            ("DSS: so sánh mô hình trên giai đoạn kiểm tra", 4, 14, 10, 5),
            ("DSS: dự báo so với thực tế theo nhóm rủi ro", 9, 14, 10, 5),
            ("DSS: tỷ lệ bắt được khách churn theo ngân sách", 14, 0, 12, 8),
            ("DSS: yếu tố ảnh hưởng (hệ số chuẩn hoá)", 14, 12, 12, 8),
            ("DSS: khách theo nhóm rủi ro mỗi tháng", 22, 0, 24, 8),
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
            ("EDA: thống kê mô tả 10 chỉ số", 0, 0, 24, 10),
            ("EDA: phân bố hoá đơn", 10, 0, 12, 8),
            ("EDA: phân bố dung lượng data", 10, 12, 12, 8),
            ("EDA: chỉ số nào tương quan với churn", 18, 0, 12, 8),
            ("EDA: kiểm tra giá trị ngoại lai (IQR)", 18, 12, 12, 8),
            ("EDA: kiểm tra dữ liệu thiếu & sentinel", 26, 0, 12, 8),
            ("EDA: số lượng thành viên mỗi dimension", 26, 12, 12, 8),
            ("EDA: mỗi khách có bao nhiêu kỳ", 34, 0, 24, 8),
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
            description=DESCRIPTIONS.get(name, ""),
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
