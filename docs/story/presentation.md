# Telco Churn DSS — Bài thuyết trình

> **Nguồn dữ liệu:** `gold.fact_billing_support` (112.552 billing cycle × 27.778 khách)
> **Trạng thái:** 12/12 DQ rule PASS · mọi con số trong bài đã verify bằng query thật
> **Ngày verify:** 2026-11-07

---

## Slide 0 — Mở đầu

### Chúng ta đang trả lời câu hỏi gì?

> **Khách hàng rời bỏ vì lý do gì, và can thiệp ở đâu để giữ chân hiệu quả nhất?**

### Quy mô dữ liệu

| Hạng mục | Giá trị |
|---|---|
| Billing cycle | 112.552 |
| Khách hàng | 27.778 |
| Thời gian | 2020-01 → 2022-12 (36 tháng) |
| Cycle/khách | 4,05 |
| Dimension phân tích | issue_type (4), support_channel (4), payment_status (3), escalation_flag, churn_flag |

### ⚠️ Ba điều phải nói trước

1. **Dataset synthetic** — tên file có `evenly_distributed`. Giá trị của bài này là **phương pháp**, không phải kết luận thị trường.
2. **`churn_rate` là mức billing cycle**, không phải mức khách hàng.
3. **CSAT/resolution chỉ tính trên kỳ có liên hệ** (`support_interaction_count > 0`).

---

## Slide 1 — Phát hiện số 0: gần như toàn bộ khách đã churn

### Insight

> **27.777 trong 27.778 khách (99,996%) có ít nhất một kỳ bị đánh dấu churn. Chỉ đúng 1 khách sống sót qua cả 36 kỳ.**

Đây là insight **quan trọng nhất và bị bỏ sót** trong phân tích ban đầu. Nó định nghĩa lại toàn bộ bài toán: không phải "ai sẽ churn" mà là **"churn nhanh hay chậm"**.

### Số liệu chứng minh

```
 churned_customers | customers | churned_cycles | total_cycles
-------------------+-----------+----------------+--------------
             27777 |     27778 |          27777 |       112552
```

Khách duy nhất không churn:

```
 customer_id | cycles | churned
-------------+--------+---------
 CUST0006145 |     36 |       0
```

### Ý nghĩa

| Cách hiểu cũ | Cách hiểu đúng |
|---|---|
| "24,7% khách churn" | "24,7% **billing cycle** bị đánh dấu churn" |
| "Tìm segment rủi ro cao" | "Tìm yếu tố làm churn **nhanh hơn**" |
| "Giữ chân khách" | "**Kéo dài thời gian sống** của khách" |

### SQL

```sql
-- Số khách churn vs tổng
SELECT COUNT(DISTINCT customer_key) FILTER (WHERE churn_flag) AS churned_customers,
       COUNT(DISTINCT customer_key)                           AS customers,
       COUNT(*) FILTER (WHERE churn_flag)                     AS churned_cycles,
       COUNT(*)                                               AS total_cycles
FROM gold.fact_billing_support;

-- Khách không bao giờ churn
SELECT c.customer_id, COUNT(*) AS cycles,
       COUNT(*) FILTER (WHERE f.churn_flag) AS churned
FROM gold.fact_billing_support f
JOIN gold.dim_customer c USING (customer_key)
GROUP BY 1
HAVING COUNT(*) FILTER (WHERE f.churn_flag) = 0;
```

### Chart

**Loại:** KPI card + bảng 1 dòng
**Nguồn:** `gold.fact_billing_support` ⨝ `gold.dim_customer`

---

## Slide 2 — Act 1: Cohort đóng, mất khách đều đặn

### Insight

> **Toàn bộ 27.778 khách đều bắt đầu từ 2020-01, không có khách mới. Mỗi tháng cohort mất ~25% số còn lại — đều đặn, không giảm theo thâm niên.**

### Số liệu chứng minh

**Cohort đóng** — mọi khách có kỳ đầu tiên là `20200101`:

```
 first_cycle | customers
-------------+-----------
    20200101 |     27778
```

**Retention ratio** — tháng sau còn ~75% tháng trước:

| Tháng | billing_cycles | % so tháng trước |
|---|---|---|
| 2020-01 | 27.778 | — |
| 2020-02 | 20.909 | 75,3% |
| 2020-03 | 15.693 | 75,1% |
| 2020-04 | 11.836 | 75,4% |
| 2020-05 | 8.845 | 74,7% |
| 2020-06 | 6.685 | 75,6% |
| 2020-07 | 5.113 | 76,5% |
| 2020-08 | 3.838 | 75,1% |

**Churn rate ổn định** — 12 tháng đủ mẫu:

| Tháng | churn_rate |
|---|---|
| 2020-01 | 24,73% |
| 2020-02 | 24,95% |
| 2020-03 | 24,58% |
| 2020-04 | 25,27% |
| 2020-05 | 24,42% |
| 2020-06 | 23,52% |
| 2020-07 | 24,94% |
| 2020-08 | 24,73% |
| 2020-09 | 23,92% |
| 2020-10 | 23,25% |
| 2020-11 | 24,36% |
| 2020-12 | 25,94% |

→ Chênh lệch chỉ **2,69 điểm %** giữa tháng cao nhất và thấp nhất. Xác suất churn **không đổi theo thâm niên**.

### Ý nghĩa

- Không có "giai đoạn an toàn" sau onboarding — khách tháng thứ 4 rủi ro y như tháng đầu.
- Half-life của cohort ≈ **2,4 tháng** (0,75²·⁴ ≈ 0,5).
- Retention phải chạy **liên tục**, không chỉ ở onboarding.

### SQL

```sql
-- Cohort đóng: mọi khách bắt đầu cùng tháng
SELECT first_cycle, COUNT(*) AS customers
FROM (
  SELECT customer_key, MIN(billing_start_date_key) AS first_cycle
  FROM gold.fact_billing_support
  GROUP BY customer_key
) t
GROUP BY first_cycle ORDER BY first_cycle;

-- Retention ratio theo tháng
SELECT year, month, billing_cycles,
       ROUND(100.0 * billing_cycles
             / LAG(billing_cycles) OVER (ORDER BY year, month), 1) AS pct_of_prev
FROM gold.mv_billing_monthly
ORDER BY year, month;

-- Churn rate ổn định (chỉ tháng đủ mẫu)
SELECT year, month, billing_cycles, churn_rate
FROM gold.mv_billing_monthly
WHERE billing_cycles >= 1000
ORDER BY year, month;
```

### Chart

**Loại:** Combo — cột `% cohort còn hoạt động` + đường `arpu`, trục X = `month_start`
**Nguồn:** `gold.mv_billing_monthly`
**Filter:** `billing_cycles >= 1000`

---

## Slide 3 — Act 1b: Mất khách, không mất giá

### Insight

> **`total_revenue` giảm từ 2,37M xuống 138, nhưng `arpu` giữ ổn định 83,56–86,60 ở các tháng đủ mẫu. Vấn đề là giữ chân, không phải giá.**

### Số liệu chứng minh

| Tháng | billing_cycles | total_revenue | arpu |
|---|---|---|---|
| 2020-01 | 27.778 | 2.368.390,97 | **85,26** |
| 2020-06 | 6.685 | 568.976,81 | **85,11** |
| 2020-12 | 1.276 | 108.937,76 | **85,37** |
| 2021-06 | 224 | 18.665,18 | **83,33** |
| 2021-12 | 39 | 2.911,09 | **74,64** |
| 2022-12 | 1 | 138,11 | **138,11** ⚠️ |

⚠️ **Caveat:** tháng cuối chỉ có 1 cycle → ARPU = hoá đơn của đúng 1 khách. Chỉ so sánh ARPU ở tháng có `billing_cycles >= 1000`.

### Ý nghĩa

- Không có dấu hiệu giảm giá hay giảm chi tiêu/khách.
- Đòn bẩy doanh thu nằm ở **số khách giữ lại**, không phải giá.
- Dashboard phải đặt `total_revenue` **cạnh** `arpu` để tránh hiểu nhầm.

### SQL

```sql
SELECT year, month, billing_cycles, active_customers, total_revenue, arpu
FROM gold.mv_billing_monthly
ORDER BY year, month;
```

### Chart

**Loại:** Line 2 series — `total_revenue` (trục trái) + `arpu` (trục phải)
**Nguồn:** `gold.mv_billing_monthly`
**Filter:** `billing_cycles >= 1000`

---

## Slide 4 — Act 2: Không có thủ phạm rõ ràng

### Insight

> **9 tổ hợp issue × channel đều churn 27,03–28,63%. Chênh lệch chỉ 1,6 điểm %. Dồn nguồn lực theo issue hay kênh không phải đòn bẩy giảm churn.**

### Số liệu chứng minh

| issue_type | support_channel | billing_cycles | churn_rate |
|---|---|---|---|
| Technical | Email | 10.406 | **28,63%** |
| Technical | Phone | 10.467 | 28,62% |
| Billing | Phone | 10.427 | 28,21% |
| Service Quality | Phone | 10.403 | 28,16% |
| Billing | Chat | 10.506 | 28,12% |
| Billing | Email | 10.365 | 28,05% |
| Technical | Chat | 10.406 | 27,57% |
| Service Quality | Chat | 10.439 | 27,32% |
| Service Quality | Email | 10.414 | 27,03% |
| **No Support** | **None** | **18.719** | **8,20%** ← ô duy nhất khác biệt |

### Ý nghĩa

- Không issue nào nổi trội → không lập chương trình retention riêng cho "Technical".
- Ô duy nhất khác biệt là `No Support × None` — dẫn tới Act 3.

### SQL

```sql
SELECT issue_type, support_channel, billing_cycles, churn_rate
FROM gold.mv_churn_by_issue_channel
ORDER BY churn_rate DESC;
```

### Chart

**Loại:** Heatmap `churn_rate` theo `issue_type` × `support_channel`, có tổng hàng/cột
**Nguồn:** `gold.mv_churn_by_issue_channel`
**Ghi chú:** 10 ô (không phải 16) vì `No Support` chỉ đi với `None`

---

## Slide 5 — Act 2b: Tốc độ xử lý không phải đòn bẩy

### Insight

> **Thời gian xử lý gần như giống hệt nhau giữa 3 kênh (65,03–65,13 phút). Không kênh nào vừa nhanh hơn vừa giữ chân tốt hơn.**

### Số liệu chứng minh

| support_channel | tickets | avg_resolution | csat | escalation_rate |
|---|---|---|---|---|
| Email | 31.185 | **65,03** | 2,99 | 9,98% |
| Phone | 31.297 | 65,08 | 3,00 | 10,10% |
| Chat | 31.351 | **65,13** | 3,00 | 9,85% |

Chênh lệch resolution: **0,10 phút** (0,15%). Chênh lệch churn: **< 1,6 điểm %**.

### ⚠️ Sửa lỗi trong phân tích trước

Phân tích ban đầu nói *"Chat xử lý nhanh nhất (64,54–65,47 phút)"*. **Sai.** Số liệu thật: **Email nhanh nhất** (65,03), Chat **chậm nhất** (65,13). Chênh lệch không đáng kể nên kết luận không đổi, nhưng con số phải sửa.

### Ý nghĩa

- KPI tốc độ xử lý chưa phải proxy cho retention.
- Rút SLA thêm vài phút khó đổi thành giữ chân.
- Giữ SLA như **chỉ số vệ sinh**, không phải KPI chiến lược.

### SQL

```sql
SELECT support_channel,
       SUM(tickets) AS tickets,
       ROUND(SUM(avg_resolution_min * tickets) / SUM(tickets), 2) AS avg_res,
       ROUND(SUM(csat * tickets) / SUM(tickets), 2) AS csat,
       ROUND(100.0 * SUM(escalations) / SUM(tickets), 2) AS esc_rate
FROM gold.mart_support_monthly
GROUP BY support_channel
ORDER BY avg_res;
```

### Chart

**Loại:** Bảng scorecard có thanh màu
**Nguồn:** `gold.mart_support_monthly`
**Ghi chú:** Phải weighted theo `tickets`, không lấy trung bình của trung bình

---

## Slide 6 — Act 3: Nghịch lý im lặng

### Insight

> **Kỳ không liên hệ support churn 8,20%. Kỳ có liên hệ churn 27,97% — gấp 3,4 lần. Rủi ro tập trung tại điểm chạm support, không phải ở nhóm im lặng.**

### Số liệu chứng minh

| segment | cycles | churned | churn_rate | % cycles | % churn |
|---|---|---|---|---|---|
| Contacted | 93.833 | 26.242 | **27,97%** | 83,37% | **94,47%** |
| Silent | 18.719 | 1.535 | **8,20%** | 16,63% | 5,53% |

### Ý nghĩa

- Giả định "khách im lặng mới nguy hiểm" **không đúng** ở dataset này.
- Nhóm có liên hệ chứa **94,47% ca churn** nhưng chiếm **83,37% khối lượng** → lift chỉ **1,13×**.
- Trigger duy nhất "có liên hệ" tạo hàng đợi ~23.164 khách ngay tháng đầu — quá rộng.

### SQL

```sql
SELECT CASE WHEN support_interaction_count = 0 THEN 'Silent' ELSE 'Contacted' END AS segment,
       COUNT(*) AS cycles,
       COUNT(*) FILTER (WHERE churn_flag) AS churned,
       ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_cycles,
       ROUND(100.0 * COUNT(*) FILTER (WHERE churn_flag)
             / SUM(COUNT(*) FILTER (WHERE churn_flag)) OVER (), 2) AS pct_churn
FROM gold.fact_billing_support
GROUP BY 1;
```

### Chart

**Loại:** Bar 2 cột + nhãn n
**Nguồn:** `gold.fact_billing_support`

---

## Slide 7 — Act 3b: CSAT 3,00 là điểm giữa của phân bố đều

### Insight

> **Trong nhóm có liên hệ, CSAT phân bố gần như đều: mỗi mức 1–5 chiếm ~20%. Không có "40% chấm 5" như phân tích ban đầu.**

### Số liệu chứng minh

**Trong nhóm có liên hệ (93.833 cycle):**

| satisfaction_score | cycles | % |
|---|---|---|
| 1 | 18.816 | 20,05% |
| 2 | 18.714 | 19,94% |
| 3 | 18.709 | 19,94% |
| 4 | 18.895 | 20,14% |
| 5 | 18.699 | 19,93% |

**Toàn bộ 112.552 cycle (gồm cả sentinel):**

| satisfaction_score | cycles | % |
|---|---|---|
| 1 | 18.816 | 16,72% |
| 2 | 18.714 | 16,63% |
| 3 | 18.709 | 16,62% |
| 4 | 18.895 | 16,79% |
| **5** | **37.418** | **33,25%** ← gấp đôi vì 18.719 sentinel |

### ⚠️ Sửa lỗi trong phân tích trước

Phân tích ban đầu nói *"~40% kỳ có liên hệ chấm 5, phần còn lại trung bình ≈ 1,66"*. **Sai.** Con số 40% có được là do **lẫn 18.719 dòng sentinel** (không interaction nhưng CSAT = 5). Trong nhóm có liên hệ thật, phân bố **đều 20% mỗi mức**.

### Ý nghĩa

- CSAT trung bình 3,00 **không** che giấu phân bố lưỡng cực — nó là trung bình của phân bố đều.
- Nhưng CSAT vẫn là KPI hữu ích: churn rate giảm đơn điệu theo CSAT (xem Slide 9).
- **Bài học:** luôn filter `support_interaction_count > 0` trước khi tính CSAT.

### SQL

```sql
-- Phân bố CSAT trong nhóm có liên hệ
SELECT satisfaction_score, COUNT(*) AS cycles,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1 ORDER BY 1;

-- So sánh: phân bố CSAT toàn bộ (bị nhiễu bởi sentinel)
SELECT satisfaction_score, COUNT(*) AS cycles,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM gold.fact_billing_support
GROUP BY 1 ORDER BY 1;
```

### Chart

**Loại:** Histogram 5 cột, 2 series (có filter / không filter)
**Nguồn:** `gold.fact_billing_support`

---

## Slide 8 — Act 3c: Quên filter làm số liệu "đẹp giả"

### Insight

> **Không filter `support_interaction_count > 0` làm CSAT tăng 11% và resolution time giảm 16,6%.**

### Số liệu chứng minh

| variant | cycles | csat | resolution |
|---|---|---|---|
| Không filter | 112.552 | **3,33** | **54,25** |
| Có filter | 93.833 | **3,00** | **65,08** |
| Chênh lệch | — | **+11,0%** | **−16,6%** |

### ⚠️ Sửa lỗi trong phân tích trước

Phân tích ban đầu nói resolution giảm **−19%** (66,8 → 54,3). **Sai.** Số thật: **65,08 → 54,25 = −16,6%**. Con số 66,8 không tái lập được từ bất kỳ filter nào.

### Ý nghĩa

- Mọi chart query fact trực tiếp mà quên filter sẽ báo cáo support tốt hơn thực tế.
- Cần tạo **saved question chuẩn** "kỳ có liên hệ" trong Metabase.
- Đề xuất data engineer đổi sentinel thành `NULL` ở Gold layer.

### SQL

```sql
SELECT 'with filter' AS variant, COUNT(*) AS cycles,
       ROUND(AVG(satisfaction_score)::numeric, 2) AS csat,
       ROUND(AVG(resolution_time_minutes)::numeric, 2) AS resolution
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
UNION ALL
SELECT 'no filter', COUNT(*),
       ROUND(AVG(satisfaction_score)::numeric, 2),
       ROUND(AVG(resolution_time_minutes)::numeric, 2)
FROM gold.fact_billing_support;
```

### Chart

**Loại:** Bar so sánh 2 cặp (CSAT và resolution, mỗi cái 2 cột)
**Nguồn:** `gold.fact_billing_support`

---

## Slide 9 — Act 4: Ma sát tích luỹ — churn tăng theo số lần liên hệ

### Insight

> **Churn tăng đơn điệu theo số lần liên hệ: 8,20% (0 lần) → 23,60% (1 lần) → 33,36% (5 lần). Đây là bằng chứng của ma sát tích luỹ, không phải tín hiệu huỷ đơn thuần.**

### Số liệu chứng minh

| support_interaction_count | cycles | churned | churn_rate |
|---|---|---|---|
| 0 | 18.719 | 1.535 | **8,20%** |
| 1 | 18.708 | 4.416 | **23,60%** |
| 2 | 18.736 | 4.692 | **25,04%** |
| 3 | 18.740 | 5.207 | **27,79%** |
| 4 | 18.755 | 5.624 | **29,99%** |
| 5 | 18.894 | 6.303 | **33,36%** |

**Mức tăng:** +15,40 điểm % từ 0→1, rồi +1,44 / +2,75 / +2,20 / +3,37 điểm % mỗi bước.

### Diễn giải theo quy tắc đã chốt trước

| Kết quả | Diễn giải | Hệ quả |
|---|---|---|
| Tăng dần từ 1 → 5 | ✅ **Ma sát tích luỹ** | Ưu tiên giải quyết ngay lần đầu; trigger mạnh từ lần thứ 2 |

Kết quả khớp với kịch bản "tăng dần" — **không** phải "nhảy bậc rồi đi ngang".

### Ý nghĩa

- Mỗi lần liên hệ thêm làm tăng rủi ro churn ~2–3 điểm %.
- Can thiệp hiệu quả nhất là **giải quyết dứt điểm ngay lần đầu**.
- Trigger nên bật từ lần liên hệ thứ 2, không phải lần đầu.

### SQL

```sql
SELECT support_interaction_count AS n,
       COUNT(*) AS cycles,
       COUNT(*) FILTER (WHERE churn_flag) AS churned,
       ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
GROUP BY 1 ORDER BY 1;
```

### Chart

**Loại:** Cột 6 mức + nhãn n
**Nguồn:** `gold.fact_billing_support`

---

## Slide 10 — Act 4b: Escalation là tín hiệu mạnh

### Insight

> **Ticket bị escalate có churn 39,52% so với 26,69% ở ticket không escalate — lift 1,48×. Escalation chỉ chiếm ~10% ticket nên là tín hiệu tầng 2 lý tưởng.**

### Số liệu chứng minh

| escalation_flag | cycles | churn_rate | avg_csat |
|---|---|---|---|
| false | 84.472 | **26,69%** | 3,00 |
| true | 9.361 | **39,52%** | 3,02 |

**Escalation rate tổng:** 9.361 / 93.833 = **9,98%**

### Ý nghĩa

- Lift 1,48× — **gấp 3 lần** mức chênh lệch giữa các issue/kênh (1,6 điểm %).
- Chỉ 10% ticket bị escalate → hàng đợi retention nhỏ, chi phí thấp.
- **Đây là tín hiệu tầng 2 tốt nhất** cho rule 2 tầng.

### SQL

```sql
SELECT escalation_flag,
       COUNT(*) AS cycles,
       ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate,
       ROUND(AVG(satisfaction_score)::numeric, 2) AS avg_csat
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1 ORDER BY 1;
```

### Chart

**Loại:** Bar 2 cột + nhãn n
**Nguồn:** `gold.fact_billing_support`
**Filter:** `support_interaction_count > 0`

---

## Slide 11 — Act 4c: Payment status là tín hiệu mạnh nhất

### Insight

> **Churn rate theo payment status: Unpaid 38,14% > Partial 30,34% > Paid 22,31%. Chênh lệch 15,83 điểm % — mạnh nhất trong mọi dimension.**

### Số liệu chứng minh

| payment_status | cycles | churned | churn_rate |
|---|---|---|---|
| Unpaid | 11.220 | 4.279 | **38,14%** |
| Partial | 11.067 | 3.358 | **30,34%** |
| Paid | 90.265 | 20.140 | **22,31%** |

**Lift Unpaid vs Paid:** 38,14 / 22,31 = **1,71×**

### Ý nghĩa

- Đây là tín hiệu **mạnh nhất** trong tất cả dimension đã kiểm tra.
- Có sẵn trong billing, không cần survey → **chi phí bằng 0**.
- Phủ 19,80% cycle (Unpaid 9,97% + Partial 9,83%) — đủ rộng để có tác động.
- ⚠️ **Caveat:** `payment_due_date` nằm sau `billing_cycle_end` → quá hạn của kỳ t chỉ biết sau kỳ t. Muốn làm cảnh báo sớm phải test quá hạn kỳ t → churn kỳ t+1.

### SQL

```sql
SELECT ps.payment_status,
       COUNT(*) AS cycles,
       COUNT(*) FILTER (WHERE f.churn_flag) AS churned,
       ROUND(100.0 * AVG(CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support f
JOIN gold.dim_payment_status ps USING (payment_status_key)
GROUP BY 1
ORDER BY churn_rate DESC;
```

### Chart

**Loại:** Bar 3 cột + nhãn n
**Nguồn:** `gold.fact_billing_support` ⨝ `gold.dim_payment_status`

---

## Slide 12 — Act 4d: CSAT thấp đi kèm churn cao

### Insight

> **Churn rate giảm đơn điệu theo CSAT: 37,97% (CSAT 1) → 18,53% (CSAT 5). Chênh lệch 19,44 điểm %.**

### Số liệu chứng minh

| satisfaction_score | cycles | churn_rate |
|---|---|---|
| 1 | 18.816 | **37,97%** |
| 2 | 18.714 | **32,69%** |
| 3 | 18.709 | **27,32%** |
| 4 | 18.895 | **23,31%** |
| 5 | 18.699 | **18,53%** |

**Mức giảm:** ~4,9 điểm % mỗi mức CSAT.

### Ý nghĩa

- CSAT là tín hiệu **đơn điệu và mạnh** — không phải nhiễu.
- Nhưng nhóm CSAT thấp (1–2) chiếm **40% kỳ có liên hệ** → quá rộng để làm trigger duy nhất.
- Kết hợp với escalation (10% ticket) sẽ thu hẹp đáng kể.

### SQL

```sql
SELECT satisfaction_score AS csat,
       COUNT(*) AS cycles,
       ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate
FROM gold.fact_billing_support
WHERE support_interaction_count > 0
GROUP BY 1 ORDER BY 1;
```

### Chart

**Loại:** Cột 5 mức + nhãn n
**Nguồn:** `gold.fact_billing_support`
**Filter:** `support_interaction_count > 0`

---

## Slide 13 — Act 4e: Phát hiện then chốt — liên hệ KHÔNG báo trước churn

### Insight

> **Liên hệ ở kỳ t không làm tăng churn ở kỳ t+1: 24,73% (có liên hệ) vs 24,39% (không liên hệ). Chênh lệch 0,34 điểm % — không có ý nghĩa.**
>
> **Kết luận: liên hệ support là TRIỆU CHỨNG cùng kỳ của churn, không phải NGUYÊN NHÂN.**

### Số liệu chứng minh

| contacted ở kỳ t | cycle pairs | churn_next_rate |
|---|---|---|
| false | 17.184 | **24,39%** |
| true | 67.590 | **24,73%** |

**Lift:** 24,73 / 24,39 = **1,014×** — gần như bằng 1.

### Đây là phát hiện quan trọng nhất của toàn bộ phân tích

| Nếu lift cao (>1,3) | Nếu lift ≈ 1 (thực tế) |
|---|---|
| Liên hệ là **nguyên nhân** → chăm sóc sau liên hệ sẽ giữ chân được | Liên hệ là **triệu chứng** → chăm sóc sau liên hệ **không** giữ chân được |
| Trigger sau sự kiện liên hệ | Can thiệp phải diễn ra **ngay tại điểm liên hệ** |

### Ý nghĩa

- Phá vỡ giả định "chăm sóc khách sau khi họ phàn nàn sẽ giữ được họ".
- Khách liên hệ ở kỳ t có thể chính là **cuộc gọi huỷ** — churn xảy ra cùng kỳ.
- Chiến lược đúng: **giải quyết vấn đề ngay trong cuộc gọi**, không phải follow-up sau đó.
- Cần **ngày churn** và **reason code** để xác nhận dứt điểm.

### SQL

```sql
WITH seq AS (
  SELECT customer_key,
         support_interaction_count > 0 AS contacted,
         churn_flag,
         LEAD(churn_flag) OVER (PARTITION BY customer_key
                                ORDER BY billing_start_date_key) AS churn_next
  FROM gold.fact_billing_support
)
SELECT contacted,
       COUNT(*) AS cycle_pairs,
       ROUND(100.0 * AVG(CASE WHEN churn_next THEN 1.0 ELSE 0.0 END), 2) AS churn_next_rate
FROM seq
WHERE churn_flag = FALSE        -- kỳ t chưa churn
  AND churn_next IS NOT NULL    -- có kỳ t+1
GROUP BY contacted;
```

### Chart

**Loại:** Bar 2 cột + nhãn n
**Nguồn:** `gold.fact_billing_support`
**Filter:** `churn_flag = FALSE AND churn_next IS NOT NULL`

---

## Slide 14 — Act 4f: Chân dung khách churn

### Insight

> **Khách churn dùng ít dịch vụ hơn, trả phí phạt nhiều hơn, và liên hệ support nhiều hơn.**

### Số liệu chứng minh

| measure | không churn (84.775) | churn (27.777) | chênh lệch |
|---|---|---|---|
| `call_minutes` | 356,3 | 330,0 | **−7,4%** |
| `data_usage_gb` | 51,87 | 46,93 | **−9,5%** |
| `sms_count` | 100,8 | 95,6 | **−5,2%** |
| `late_fee` | 1,51 | 2,62 | **+73,5%** |
| `discounts_applied` | 10,11 | 9,67 | **−4,4%** |
| `support_interaction_count` | 2,34 | 3,00 | **+28,2%** |

### Ý nghĩa

- **Tín hiệu sớm không cần khách liên hệ:** usage giảm + late_fee tăng.
- Đây là cách phủ được cả nhóm im lặng (18.719 cycle).
- `late_fee` tăng 73,5% là tín hiệu mạnh nhất trong các measure hành vi.

### SQL

```sql
SELECT churn_flag,
       COUNT(*) AS cycles,
       ROUND(AVG(call_minutes)::numeric, 1) AS avg_call,
       ROUND(AVG(data_usage_gb)::numeric, 2) AS avg_data,
       ROUND(AVG(sms_count)::numeric, 1) AS avg_sms,
       ROUND(AVG(late_fee)::numeric, 2) AS avg_late,
       ROUND(AVG(discounts_applied)::numeric, 2) AS avg_disc,
       ROUND(AVG(support_interaction_count)::numeric, 2) AS avg_int
FROM gold.fact_billing_support
GROUP BY 1 ORDER BY 1;
```

### Chart

**Loại:** Index bar (không churn = 100)
**Nguồn:** `gold.fact_billing_support`

---

## Slide 15 — Act 4g: Discount có tương quan nghịch nhẹ

### Insight

> **Churn rate giảm nhẹ khi discount tăng: 26,31% (bucket 1) → 22,78% (bucket 4). Chênh 3,53 điểm % — tương quan yếu, không đủ để kết luận nhân quả.**

### Số liệu chứng minh

| discount bucket | avg_discount | cycles | churn_rate |
|---|---|---|---|
| 1 | 2,50 | 28.089 | **26,31%** |
| 2 | 7,50 | 28.094 | **25,72%** |
| 3 | 12,48 | 28.180 | **23,92%** |
| 4 | 17,50 | 28.160 | **22,78%** |
| 5 | 20,00 | 29 | 17,24% ⚠️ mẫu quá nhỏ |

### ⚠️ Cảnh báo reverse causality

Discount có thể được trao cho khách **sắp rời** (retention offer) → chiều nhân quả ngược. Tương quan nghịch **không** chứng minh discount hiệu quả.

### Ý nghĩa

- Discount chiếm **11,71% doanh thu** (tháng 2020-01) — khoản chi lớn.
- Chưa có bằng chứng nhân quả → cần **holdout test** trước khi mở rộng.
- Đây là khoản tiết kiệm tiềm năng lớn nếu chứng minh không hiệu quả.

### SQL

```sql
SELECT WIDTH_BUCKET(discounts_applied, 0, 20, 4) AS bucket,
       COUNT(*) AS cycles,
       ROUND(100.0 * AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END), 2) AS churn_rate,
       ROUND(AVG(discounts_applied)::numeric, 2) AS avg_disc
FROM gold.fact_billing_support
GROUP BY 1 ORDER BY 1;
```

### Chart

**Loại:** Bar 4 cột (bỏ bucket 5 vì n=29)
**Nguồn:** `gold.fact_billing_support`

---

## Slide 16 — Act 5: Tổng hợp tín hiệu — bảng xếp hạng

### Insight

> **Xếp hạng tín hiệu theo lift và độ phủ. Escalation mạnh nhất (1,60×) nhưng chỉ phủ 9,98%; CSAT ≤ 2 phủ 40% với lift 1,43× — kết hợp cả hai cho rule 2 tầng.**

### Bảng xếp hạng

| # | Tín hiệu | Lift | Độ phủ | Chi phí | Dùng làm |
|---|---|---|---|---|---|
| 1 | **Escalation = true** | **1,60×** | 9,98% | 0 (có sẵn) | Tầng 2 |
| 2 | **Payment status = Unpaid** | **1,55×** | 9,97% | 0 (có sẵn) | Tầng 2 |
| 3 | **CSAT ≤ 2** | **1,43×** | 40,00% | 0 (có sẵn) | Tầng 2 (rộng) |
| 4 | **Interactions ≥ 3** | **1,23×** | 50,10% | 0 (có sẵn) | Tầng 2 (rộng) |
| 5 | Có liên hệ support | 1,13× | 83,37% | 0 | **Tầng 1** |
| 6 | Issue type | 1,06× | 27,8% | — | ❌ Không dùng |
| 7 | Support channel | 1,02× | 27,8% | — | ❌ Không dùng |

**Base churn rate:** 24,68% (toàn bộ 112.552 cycle)

### Đề xuất rule 2 tầng

```
TẦNG 1 (đưa vào pool):  support_interaction_count > 0
                        → 93.833 cycle (83,37%)

TẦNG 2 (xếp hạng ưu tiên):
  Điểm = 3 × (escalation_flag = TRUE)
       + 3 × (payment_status = 'Unpaid')
       + 2 × (satisfaction_score <= 2)
       + 1 × (support_interaction_count >= 3)
```

**Ước tính hàng đợi ưu tiên cao** (điểm ≥ 5): ~5–8% pool ≈ 5.000–7.500 cycle.

### SQL

```sql
-- Bảng xếp hạng tín hiệu (đã verify)
WITH base AS (
  SELECT AVG(CASE WHEN churn_flag THEN 1.0 ELSE 0.0 END) AS base_rate
  FROM gold.fact_billing_support
),
sig AS (
  SELECT 'escalation' AS signal,
         COUNT(*) FILTER (WHERE f.escalation_flag) AS exposed,
         COUNT(*) AS total,
         AVG(CASE WHEN f.escalation_flag
                  THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END) AS exposed_rate
  FROM gold.fact_billing_support f
  WHERE f.support_interaction_count > 0
  UNION ALL
  SELECT 'payment_unpaid',
         COUNT(*) FILTER (WHERE ps.payment_status = 'Unpaid'),
         COUNT(*),
         AVG(CASE WHEN ps.payment_status = 'Unpaid'
                  THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
  FROM gold.fact_billing_support f
  JOIN gold.dim_payment_status ps USING (payment_status_key)
  UNION ALL
  SELECT 'csat_le_2',
         COUNT(*) FILTER (WHERE f.satisfaction_score <= 2),
         COUNT(*),
         AVG(CASE WHEN f.satisfaction_score <= 2
                  THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
  FROM gold.fact_billing_support f
  WHERE f.support_interaction_count > 0
  UNION ALL
  SELECT 'interactions_ge_3',
         COUNT(*) FILTER (WHERE f.support_interaction_count >= 3),
         COUNT(*),
         AVG(CASE WHEN f.support_interaction_count >= 3
                  THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
  FROM gold.fact_billing_support f
  UNION ALL
  SELECT 'contacted',
         COUNT(*) FILTER (WHERE f.support_interaction_count > 0),
         COUNT(*),
         AVG(CASE WHEN f.support_interaction_count > 0
                  THEN CASE WHEN f.churn_flag THEN 1.0 ELSE 0.0 END END)
  FROM gold.fact_billing_support f
)
SELECT s.signal,
       s.exposed,
       ROUND(100.0 * s.exposed / s.total, 2) AS coverage_pct,
       ROUND(100.0 * s.exposed_rate, 2)      AS exposed_churn,
       ROUND(100.0 * b.base_rate, 2)         AS base_churn,
       ROUND(s.exposed_rate / b.base_rate, 2) AS lift
FROM sig s CROSS JOIN base b
ORDER BY lift DESC;
```

### Chart

**Loại:** Bảng có thanh lift
**Nguồn:** `gold.fact_billing_support` ⨝ dims

---

## Slide 17 — Act 5b: Quy mô và chi phí

### Insight

> **Mỗi điểm % churn giảm được đáng ~4% doanh thu kỳ vọng của cohort. Pilot cần ~4.900 khách/nhóm.**

### Số liệu chứng minh

**Quan hệ churn rate ↔ số cycle kỳ vọng:**

| churn_rate | cycle kỳ vọng/khách | so với 24,7% |
|---|---|---|
| 24,7% (hiện tại) | 4,05 | — |
| 23,7% | 4,22 | **+4,2%** |
| 22,2% | 4,50 | **+11,1%** |
| 20,0% | 5,00 | **+23,5%** |

Công thức: `cycle_kỳ_vọng = 1 / churn_rate`

**Cỡ mẫu pilot:**

| Tham số | Giá trị |
|---|---|
| Baseline churn rate | 27,97% |
| Mục tiêu giảm | 2,5 điểm % (→ 25,47%) |
| Power | 80% |
| α | 5% |
| **Cỡ mẫu/nhóm** | **~4.900 khách** |
| Cycle có liên hệ tháng 2020-01 | 23.164 |
| **Kết luận** | Đủ mẫu trong **1 tháng** |

### Ý nghĩa

- Cho phép đặt mục tiêu và trần ngân sách retention bằng con số.
- Pilot chỉ cần chi phí thấp hơn 4% doanh thu kỳ vọng là đáng làm.

### SQL

```sql
-- Quan hệ churn rate ↔ cycle kỳ vọng
SELECT churn_rate,
       ROUND(1.0 / (churn_rate / 100.0), 2) AS expected_cycles_per_customer,
       ROUND(100.0 * (1.0 / (churn_rate / 100.0)) / 4.05 - 100, 1) AS pct_vs_current
FROM (VALUES (24.7), (23.7), (22.2), (20.0)) AS t(churn_rate);

-- Quy mô hàng đợi tầng 1 theo tháng
SELECT year, month, SUM(tickets) AS contacted_cycles
FROM gold.mart_support_monthly
GROUP BY year, month
ORDER BY year, month;
```

### Chart

**Loại:** Line 2 series — `churn_rate` vs `expected_cycles_per_customer`
**Nguồn:** Tính từ `gold.mv_billing_monthly`

---

## Slide 18 — Act 5c: Khuyến nghị

### Sáu khuyến nghị theo thứ tự ưu tiên

| # | Khuyến nghị | Owner | Dựa trên | Đo bằng | Khi nào |
|---|---|---|---|---|---|
| 1 | **Sửa lớp metric:** filter sentinel chuẩn, thay `mart_churn_summary.churn_rate`, thêm cột `discount_ratio` | BI / Data Engineer | Slide 7, 8 | DQ rule mới PASS | **Ngay** |
| 2 | **Không tái phân bổ nguồn lực theo issue/kênh** để giảm churn; giữ SLA như chỉ số vệ sinh | Head of Support | Slide 4, 5 | — | **Ngay** |
| 3 | **Pilot retention tại điểm liên hệ** (không phải sau đó), rule 2 tầng, có nhóm đối chứng | Head of Retention + Head of Support | Slide 6, 9, 10, 13 | `churn_rate` pilot vs đối chứng | Tháng tới |
| 4 | **Holdout test cho discount** trước khi mở rộng | CFO + Head of Retention | Slide 15 | `churn_rate`, `discount_ratio` nhóm holdout | Quý tới |
| 5 | **Bổ sung data:** ngày churn, reason code, cờ trả lời survey, plan/region, khách mới | Data owner | Slide 13, 19 | Act 2 và Act 4 kể được đầy đủ | Quý tới |
| 6 | **Xây mart usage** để phân tích hành vi sử dụng (hiện phải query fact) | BI | Slide 14 | Mart mới có DQ rule | Quý tới |

### Ba quyết định xin duyệt hôm nay

1. **Duyệt 2 tuần** để sửa lớp metric và xác nhận các insight còn `low confidence`.
2. **Duyệt pilot retention** theo sự kiện liên hệ, có nhóm đối chứng, ~4.900 khách/nhóm.
3. **Yêu cầu bổ sung** ngày churn + reason code để lần sau trả lời được câu "vì sao".

---

## Slide 19 — Giới hạn và việc cần làm

### Những gì chưa trả lời được

| Câu hỏi | Trạng thái | Cần gì |
|---|---|---|
| Churn theo vùng, gói cước, nhân khẩu | ❌ Không có cột | Bổ sung plan/region/tenure |
| Khách rời vì lý do gì (giá, đối thủ, chuyển nơi) | ❌ Không có reason code | Bổ sung reason code |
| Liên hệ xảy ra **trước** hay **chính là** cuộc gọi huỷ | ⚠️ Chưa xác định | Bổ sung ngày churn |
| Tăng trưởng / acquisition | ❌ Cohort đóng | Bổ sung khách mới |
| Mùa vụ | ❌ Tháng-trong-năm trùng thâm niên | Cần nhiều cohort |
| Quy ra tiền thật | ❌ Currency UNKNOWN | Xác nhận đơn vị tiền |
| Nhân quả của discount, kênh, tốc độ xử lý | ❌ Chỉ có tương quan | A/B test hoặc holdout |

### Những gì đã sửa so với phân tích ban đầu

| # | Phân tích ban đầu | Số liệu thật | Mức độ |
|---|---|---|---|
| 1 | "Chat xử lý nhanh nhất" | Email 65,03 < Phone 65,08 < Chat 65,13 | Nhỏ — kết luận không đổi |
| 2 | "~40% kỳ có liên hệ chấm CSAT 5" | Phân bố đều 20% mỗi mức | **Lớn** — do lẫn sentinel |
| 3 | "Resolution giảm 19% khi quên filter" | Giảm 16,6% (65,08 → 54,25) | Trung bình |
| 4 | "66,8 phút là resolution thật" | 65,08 phút | Trung bình |
| 5 | Không nêu 99,996% khách churn | 27.777/27.778 | **Lớn** — insight bị bỏ sót |

### Việc cần làm để nâng confidence

| Insight | Confidence hiện tại | Cách nâng |
|---|---|---|
| I-03 (churn đều theo thâm niên) | **high** (đã verify) | — |
| I-13 (ma sát tích luỹ) | **high** (đã verify) | — |
| I-14 (escalation) | **high** (đã verify) | — |
| I-15 (payment status) | **high** (đã verify) | — |
| I-16 (CSAT) | **high** (đã verify) | — |
| I-19 (liên hệ không báo trước) | **high** (đã verify) | Cần ngày churn để xác nhận nhân quả |
| I-20 (discount) | **low** | Holdout test |

---

## Phụ lục A — Bảng tổng hợp mọi con số đã verify

| # | Metric | Giá trị | Nguồn |
|---|---|---|---|
| 1 | Tổng billing cycle | 112.552 | `fact_billing_support` |
| 2 | Tổng khách | 27.778 | `dim_customer` |
| 3 | Khách churn | 27.777 (99,996%) | `fact_billing_support` |
| 4 | Khách không churn | 1 (`CUST0006145`) | `fact_billing_support` |
| 5 | Cycle sau kỳ churn đầu tiên | 0 | `fact_billing_support` |
| 6 | Churn rate toàn kỳ | 24,68% | `fact_billing_support` |
| 7 | Churn rate tháng 2020-01 | 24,73% | `mv_billing_monthly` |
| 8 | Retention ratio tháng | 74,7–76,5% | `mv_billing_monthly` |
| 9 | ARPU tháng đủ mẫu | 83,56–86,60 | `mv_billing_monthly` |
| 10 | Revenue 2020-01 | 2.368.390,97 | `mv_billing_monthly` |
| 11 | Revenue 2022-12 | 138,11 | `mv_billing_monthly` |
| 12 | Churn rate 9 ô issue×channel | 27,03–28,63% | `mv_churn_by_issue_channel` |
| 13 | Churn rate No Support | 8,20% | `mv_churn_by_issue_channel` |
| 14 | Churn rate Contacted | 27,97% | `fact_billing_support` |
| 15 | % cycle Contacted | 83,37% | `fact_billing_support` |
| 16 | % churn cycle Contacted | 94,47% | `fact_billing_support` |
| 17 | Resolution Email | 65,03 phút | `mart_support_monthly` |
| 18 | Resolution Phone | 65,08 phút | `mart_support_monthly` |
| 19 | Resolution Chat | 65,13 phút | `mart_support_monthly` |
| 20 | CSAT có filter | 3,00 | `fact_billing_support` |
| 21 | CSAT không filter | 3,33 | `fact_billing_support` |
| 22 | Resolution có filter | 65,08 phút | `fact_billing_support` |
| 23 | Resolution không filter | 54,25 phút | `fact_billing_support` |
| 24 | Churn rate interactions=0 | 8,20% | `fact_billing_support` |
| 25 | Churn rate interactions=5 | 33,36% | `fact_billing_support` |
| 26 | Churn rate escalation=true | 39,52% | `fact_billing_support` |
| 27 | Churn rate escalation=false | 26,69% | `fact_billing_support` |
| 28 | Escalation rate | 9,98% | `fact_billing_support` |
| 29 | Churn rate Unpaid | 38,14% | `fact_billing_support` |
| 30 | Churn rate Partial | 30,34% | `fact_billing_support` |
| 31 | Churn rate Paid | 22,31% | `fact_billing_support` |
| 32 | Churn rate CSAT=1 | 37,97% | `fact_billing_support` |
| 33 | Churn rate CSAT=5 | 18,53% | `fact_billing_support` |
| 34 | Churn next rate (contacted) | 24,73% | `fact_billing_support` |
| 35 | Churn next rate (silent) | 24,39% | `fact_billing_support` |
| 36 | Overdue rate | 19,80% | `fact_billing_support` |
| 37 | Discount ratio 2020-01 | 11,71% | `mv_billing_monthly` |
| 38 | Discount / late fee | 5,78× | `mv_billing_monthly` |
| 39 | avg_late_fee churn | 2,62 | `fact_billing_support` |
| 40 | avg_late_fee không churn | 1,51 | `fact_billing_support` |
| 41 | Lift escalation | 1,60× | `fact_billing_support` |
| 42 | Lift payment Unpaid | 1,55× | `fact_billing_support` |
| 43 | Lift CSAT ≤ 2 | 1,43× | `fact_billing_support` |
| 44 | Lift interactions ≥ 3 | 1,23× | `fact_billing_support` |
| 45 | Lift contacted | 1,13× | `fact_billing_support` |
| 46 | Base churn rate | 24,68% | `fact_billing_support` |

---

## Phụ lục B — Checklist trình bày

- [x] Mọi con số đã verify bằng query thật trên `gold.*`
- [x] Mỗi insight có SQL tái lập được
- [x] Mỗi chart map tới bảng + cột thực tế
- [x] Có nêu rõ grain (billing cycle vs khách hàng)
- [x] Có nêu rõ filter (`support_interaction_count > 0`)
- [x] Có cảnh báo dataset synthetic
- [x] Có sửa 5 lỗi trong phân tích ban đầu
- [x] Có nêu giới hạn và việc cần làm
- [x] Không có TODO/TBD
