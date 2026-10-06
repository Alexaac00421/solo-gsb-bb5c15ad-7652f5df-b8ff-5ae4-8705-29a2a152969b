#!/usr/bin/env python3
"""园区分表用水结算服务 —— 端到端调用示例（兼作集成校验）。

用法:
    pip install requests
    BASE=http://localhost:8000 python3 examples/demo.py

覆盖场景:
  1. 建租户/水表/费率（费率按生效时间分段）
  2. 跨 UTC 月、跨费率区间用量按时长比例分摊
  3. 同事件同内容重复提交 -> 返回原结果；异内容 -> 409
  4. 同表同时刻异值 -> 409；引用原事件号的更正 -> 接受
  5. 乱序到达、累计值回退 -> 异常且不参与计费，后续有效读数仍衔接
  6. 关账后迟到/更正读数 -> 差额记入下一未关账月份并关联原月账
  7. 重复关账、并发提交 -> 不重复入账
"""

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = os.getenv("BASE", "http://localhost:8000").rstrip("/")

FAILED = []


def check(cond, msg):
    mark = "OK " if cond else "FAIL"
    print(f"  [{mark}] {msg}")
    if not cond:
        FAILED.append(msg)


def show(title, resp):
    print(f"\n== {title} ==  (HTTP {resp.status_code})")
    try:
        print(resp.json())
    except Exception:
        print(resp.text)


def main():
    # ---- 0. 健康检查
    r = requests.get(f"{BASE}/health")
    check(r.status_code == 200, "health check")

    # ---- 1. 租户、水表、费率
    r = requests.post(f"{BASE}/tenants", json={"name": f"demo-park-{uuid.uuid4().hex[:8]}"})
    show("create tenant", r)
    tenant_id = r.json()["id"]

    r = requests.post(f"{BASE}/meters", json={"tenant_id": tenant_id, "code": "WM-001"})
    show("create meter", r)
    meter_id = r.json()["id"]

    r = requests.post(f"{BASE}/rates", json={
        "effective_from": "2026-01-01T00:00:00+00:00", "price_per_unit": 2.0})
    check(r.status_code in (200, 201), "rate 2.0 from 2026-01-01")
    r = requests.post(f"{BASE}/rates", json={
        "effective_from": "2026-03-01T00:00:00+00:00", "price_per_unit": 3.0})
    check(r.status_code in (200, 201), "rate 3.0 from 2026-03-01")

    def submit(event_no, collected_at, value, ref=None, tid=tenant_id, mid=meter_id):
        body = {"tenant_id": tid, "meter_id": mid, "event_no": event_no,
                "collected_at": collected_at, "cumulative_value": value}
        if ref:
            body["ref_event_no"] = ref
        return requests.post(f"{BASE}/readings", json=body)

    def bill(month):
        r = requests.get(f"{BASE}/tenants/{tenant_id}/bills", params={"month": month})
        bills = r.json()["bills"]
        return bills[0] if bills else None

    # ---- 2. 跨月跨费率分摊: 2026-02-28 12:00Z=100 -> 2026-03-02 12:00Z=160 (48h, 60 吨)
    submit("e1", "2026-02-28T12:00:00+00:00", 100)
    submit("e2", "2026-03-02T12:00:00+00:00", 160)
    feb, mar = bill("2026-02"), bill("2026-03")
    show("Feb bill", requests.get(f"{BASE}/tenants/{tenant_id}/bills", params={"month": "2026-02"}))
    check(feb["usage"] == 15.0 and feb["amount"] == 30.0, "Feb: 12h/48h*60=15t @2.0 = 30")
    check(mar["usage"] == 45.0 and mar["amount"] == 135.0, "Mar: 36h/48h*60=45t @3.0 = 135")
    check(len(feb["segments"]) == 1 and len(mar["segments"]) == 1, "segments split by month & rate")

    # ---- 3. 幂等: 同事件同内容 -> 原结果; 异内容 -> 409
    r = submit("e2", "2026-03-02T12:00:00+00:00", 160)
    check(r.status_code == 200 and r.json()["duplicate"] is True, "same event same content -> 200 duplicate")
    r = submit("e2", "2026-03-02T12:00:00+00:00", 999)
    check(r.status_code == 409, "same event different content -> 409")

    # ---- 4. 同表同时刻异值: 非更正 -> 409; 更正(引用原事件号) -> 接受
    r = submit("e3", "2026-03-02T12:00:00+00:00", 170)
    check(r.status_code == 409 and "ref_event_no" in r.json().get("error", "") + str(r.json()),
          "same timestamp different value without ref -> 409")
    r = submit("e4", "2026-03-02T12:00:00+00:00", 150, ref="e2")
    check(r.status_code == 201, "correction with ref_event_no accepted")
    feb, mar = bill("2026-02"), bill("2026-03")
    check(feb["usage"] == 12.5 and feb["amount"] == 25.0, "after correction Feb: 12.5t = 25")
    check(mar["usage"] == 37.5 and mar["amount"] == 112.5, "after correction Mar: 37.5t = 112.5")

    # ---- 5. 乱序到达 + 累计值回退异常 + 后续衔接
    r = submit("e5", "2026-02-20T00:00:00+00:00", 90)   # 乱序: 更早的读数后到
    check(r.status_code == 201, "out-of-order reading accepted")
    feb = bill("2026-02")
    check(feb["usage"] == 22.5 and feb["amount"] == 45.0, "Feb recomputed: 10t + 12.5t = 22.5t = 45")

    r = submit("e6", "2026-03-10T00:00:00+00:00", 80)   # 回退: 80 < 150 -> 异常
    check(r.status_code == 201 and r.json()["status"] == "anomaly", "regression marked anomaly")
    r = submit("e7", "2026-03-20T00:00:00+00:00", 200)  # 仍衔接上一有效读数 150
    check(r.json()["usage_from_prev"] == 50.0, "next valid reading chains from 150 -> usage 50")
    mar = bill("2026-03")
    check(mar["usage"] == 87.5 and mar["amount"] == 262.5, "Mar: 37.5 + 50 = 87.5t = 262.5")
    check(len(mar["anomalies"]) == 1 and mar["anomalies"][0]["event_no"] == "e6",
          "anomaly detail in bill query")

    # ---- 6. 关账 -> 迟到更正 -> 差额记入下一未关账月
    r = requests.post(f"{BASE}/tenants/{tenant_id}/bills/close", json={"month": "2026-02"})
    show("close 2026-02", r)
    check(r.status_code == 200 and r.json()["already_closed"] is False, "close 2026-02")
    feb = bill("2026-02")
    check(feb["status"] == "closed" and feb["billed_usage"] == 22.5 and feb["billed_amount"] == 45.0,
          "Feb snapshot 22.5t/45 frozen")

    r = submit("e9", "2026-02-20T00:00:00+00:00", 85, ref="e5")  # 迟到更正, 改变已关账用量
    check(r.status_code == 201, "late correction for closed month accepted")
    feb, mar = bill("2026-02"), bill("2026-03")
    check(feb["billed_usage"] == 22.5 and feb["billed_amount"] == 45.0, "closed Feb bill NOT rewritten")
    check(len(mar["adjustments"]) == 1, "adjustment posted to next unclosed month (Mar)")
    adj = mar["adjustments"][0]
    check(adj["source_month"] == "2026-02" and adj["usage_delta"] == 5.0 and adj["amount_delta"] == 10.0,
          "adjustment +5t/+10 linked to Feb bill")
    check(mar["total_amount"] == 272.5, "Mar total = 262.5 + 10 adjustment")

    # ---- 7. 重复关账 & 并发提交不重复入账
    r = requests.post(f"{BASE}/tenants/{tenant_id}/bills/close", json={"month": "2026-02"})
    check(r.json()["already_closed"] is True, "re-close is idempotent (already_closed=true)")
    mar = bill("2026-03")
    check(len(mar["adjustments"]) == 1, "no double posting after re-close")

    with ThreadPoolExecutor(max_workers=8) as pool:
        rs = list(pool.map(lambda _: submit("e9", "2026-02-20T00:00:00+00:00", 85, ref="e5"), range(8)))
    check(all(x.status_code == 200 for x in rs) and all(x.json()["duplicate"] for x in rs),
          "8 concurrent duplicate submits -> all return original")
    mar = bill("2026-03")
    check(len(mar["adjustments"]) == 1, "concurrent submits -> still exactly 1 adjustment")

    with ThreadPoolExecutor(max_workers=4) as pool:
        rs = list(pool.map(lambda _: requests.post(
            f"{BASE}/tenants/{tenant_id}/bills/close", json={"month": "2026-03"}), range(4)))
    closed_flags = [x.json()["already_closed"] for x in rs]
    check(closed_flags.count(False) == 1, "4 concurrent closes -> exactly one performs the close")

    # 关账 3 月后, 3 月的迟到读数 -> 调整记入 4 月
    submit("e10", "2026-03-25T00:00:00+00:00", 260)
    apr = bill("2026-04")
    check(apr is not None and len(apr["adjustments"]) == 1 and apr["adjustments"][0]["source_month"] == "2026-03",
          "late reading for closed Mar -> adjustment posted to Apr")

    # ---- 8. 错误处理
    r = requests.post(f"{BASE}/readings", json={
        "tenant_id": tenant_id, "meter_id": meter_id, "event_no": "bad-tz",
        "collected_at": "2026-03-01 12:00:00", "cumulative_value": 1})
    check(r.status_code == 400, "naive collected_at -> 400")
    r = submit("x1", "2026-03-01T00:00:00+00:00", 1, tid=tenant_id + 999)
    check(r.status_code == 404, "unknown tenant -> 404")
    r = submit("x2", "2026-03-01T00:00:00+00:00", 1, mid=meter_id + 999)
    check(r.status_code == 404, "unknown meter -> 404")
    r = requests.post(f"{BASE}/meters", json={"tenant_id": tenant_id, "code": "WM-002"})
    other_meter = r.json()["id"]
    r = submit("x3", "2026-03-01T00:00:00+00:00", 1, tid=tenant_id + 999, mid=other_meter)
    check(r.status_code == 404, "tenant not found checked first")
    r = requests.get(f"{BASE}/tenants/{tenant_id}/bills", params={"month": "2026-13"})
    check(r.status_code == 400, "invalid month -> 400")
    r = submit("x4", "2026-04-01T00:00:00+00:00", 5, ref="no-such-event")
    check(r.status_code == 400, "correction referencing missing event -> 400")

    print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"{len(FAILED)} CHECKS FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
