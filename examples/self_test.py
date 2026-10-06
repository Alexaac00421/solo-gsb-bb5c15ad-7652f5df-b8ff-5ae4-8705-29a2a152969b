"""端到端调用示例 / 自检脚本（仅依赖标准库）。

用法：
    python examples/self_test.py                 # 默认 http://localhost:8080
    BASE_URL=http://localhost:8080 python examples/self_test.py

覆盖：
1. 乱序读数、累计回退异常（不参与计费，后续有效读数仍衔接）
2. 同事件同内容幂等、异内容 409
3. 同表同时刻异值冲突，以及“新事件号引用原事件号”的更正
4. 跨 UTC 月 + 跨费率按时长比例分摊（整切分，断言精确数值）
5. 月账关账、重复关账幂等
6. 迟到/更正改变已关账用量 → 下一未关账月产生关联原月账的差额
7. 租户与水表不匹配、无时区时间的明确报错
8. 并发提交与并发关账不重复入账
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")
failures: list[str] = []


def call(method: str, path: str, body: dict | None = None, expect: int | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req) as resp:
            status = resp.status
            payload = json.loads(resp.read().decode() or "{}")
    except HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read().decode() or "{}")
    if expect is not None and status != expect:
        failures.append(f"{method} {path} 期望 {expect} 实际 {status}: {payload}")
    return status, payload


def expect_error(method, path, body, status, code) -> dict:
    st, payload = call(method, path, body, expect=status)
    actual = payload.get("error", {}).get("code")
    if actual != code:
        failures.append(f"{path} 期望错误码 {code}，实际 {actual}: {payload}")
    return payload


def find_month(report: dict, period: str) -> dict:
    for m in report["months"]:
        if m["period"] == period:
            return m
    failures.append(f"报表缺少月份 {period}；实际月份 {[m['period'] for m in report['months']]}")
    return {"period": period, "total_usage": "0", "total_amount": "0",
            "adjustment_usage": "0", "adjustment_amount": "0"}


def main() -> int:
    sfx = uuid.uuid4().hex[:8]
    tenant, tenant2 = f"T-{sfx}", f"T2-{sfx}"
    meter, meter2 = f"M-{sfx}", f"M2-{sfx}"

    print("== 0. 健康检查 ==")
    call("GET", "/health", expect=200)

    print("== 1. 租户 / 水表绑定 ==")
    call("POST", "/tenants", {"code": tenant, "name": "A 栋"}, expect=200)
    call("POST", "/tenants", {"code": tenant2, "name": "B 栋"}, expect=200)
    call("POST", "/meters", {"code": meter, "tenant_code": tenant, "name": "A 栋总表"}, expect=200)
    call("POST", "/meters", {"code": meter2, "tenant_code": tenant2, "name": "B 栋总表"}, expect=200)

    # 租户与水表不匹配必须明确报错
    expect_error("POST", "/readings", {
        "tenant_code": tenant2, "meter_code": meter,
        "event_id": f"bad-{sfx}", "read_at": "2026-01-10T00:00:00Z", "cumulative": "1",
    }, 422, "meter_tenant_mismatch")
    # 无效时间（无时区）必须明确报错
    st, payload = call("POST", "/readings", {
        "tenant_code": tenant, "meter_code": meter,
        "event_id": f"naive-{sfx}", "read_at": "2026-01-10T00:00:00", "cumulative": "1",
    })
    if st != 422:
        failures.append(f"无时区时间应 422，实际 {st}: {payload}")

    print("== 2. 乱序读数 + 回退异常（费率 3.50/吨）==")
    # 故意乱序：先到 2 月读数，再到 1 月读数
    # e1 2026-01-01 00:00 = 0（首读数，锚点）
    # e2 2026-01-16 00:00 = 20
    # e3 2026-02-02 00:00 = 54（跨月区间 17 天：1 月 16 天 32 吨，2 月 1 天 2 吨）
    # e4 2026-02-03 00:00 = 5  < 锚点 54：回退异常，不参与计费
    # e5 2026-02-05 00:00 = 94（仍以 54 为锚点，40 吨落在 2 月）
    # e6 2026-03-01 00:00 = 94（零增量）
    def reading(ev, at, val, corrects=None):
        body = {
            "tenant_code": tenant, "meter_code": meter,
            "event_id": f"{ev}-{sfx}", "read_at": at, "cumulative": val,
        }
        if corrects:
            body["corrects_event"] = f"{corrects}-{sfx}"
        return call("POST", "/readings", body, expect=200)

    reading("e3", "2026-02-02T00:00:00Z", "54")
    reading("e6", "2026-03-01T00:00:00Z", "94")
    reading("e1", "2026-01-01T00:00:00Z", "0")
    reading("e5", "2026-02-05T00:00:00Z", "94")
    reading("e2", "2026-01-16T00:00:00Z", "20")
    reading("e4", "2026-02-03T00:00:00Z", "5")

    st, rows = call("GET", f"/readings?meter_code={meter}")
    derived = {r["event_id"].rsplit("-", 1)[0]: r["derived_status"] for r in rows}
    if derived.get("e4") != "anomaly":
        failures.append(f"e4 应为 anomaly，实际 {derived}")
    for ev in ("e1", "e2", "e3", "e5"):
        if derived.get(ev) != "active":
            failures.append(f"{ev} 应为 active，实际 {derived}")

    print("== 3. 幂等 / 冲突 / 更正的错误路径（不改动数据）==")
    st, resp = reading("e1", "2026-01-01T00:00:00Z", "0")
    if resp.get("deduplicated") is not True:
        failures.append(f"同事件同内容应返回 deduplicated=true: {resp}")
    expect_error("POST", "/readings", {
        "tenant_code": tenant, "meter_code": meter,
        "event_id": f"e1-{sfx}", "read_at": "2026-01-01T00:00:00Z", "cumulative": "99",
    }, 409, "event_content_conflict")
    # 同表同时刻异值/同值但未引用原事件号 → 冲突
    expect_error("POST", "/readings", {
        "tenant_code": tenant, "meter_code": meter,
        "event_id": f"x1-{sfx}", "read_at": "2026-02-02T00:00:00Z", "cumulative": "55",
    }, 409, "same_timestamp_conflict")
    expect_error("POST", "/readings", {
        "tenant_code": tenant, "meter_code": meter,
        "event_id": f"x2-{sfx}", "read_at": "2026-02-02T00:00:00Z", "cumulative": "54",
    }, 409, "same_timestamp_conflict")
    # 更正必须时刻相同
    expect_error("POST", "/readings", {
        "tenant_code": tenant, "meter_code": meter,
        "event_id": f"x3-{sfx}", "read_at": "2026-02-02T00:00:01Z",
        "cumulative": "55", "corrects_event": f"e3-{sfx}",
    }, 422, "correction_time_mismatch")
    # 更正值不得与原值相同
    expect_error("POST", "/readings", {
        "tenant_code": tenant, "meter_code": meter,
        "event_id": f"x4-{sfx}", "read_at": "2026-02-02T00:00:00Z",
        "cumulative": "54", "corrects_event": f"e3-{sfx}",
    }, 422, "correction_unchanged")
    # 引用不存在的原事件号
    expect_error("POST", "/readings", {
        "tenant_code": tenant, "meter_code": meter,
        "event_id": f"x5-{sfx}", "read_at": "2026-02-02T00:00:00Z",
        "cumulative": "54", "corrects_event": f"nope-{sfx}",
    }, 404, "original_event_not_found")

    print("== 4. 合法更正：54 -> 44（2/2 时刻）==")
    st, resp = reading("c3", "2026-02-02T00:00:00Z", "44", corrects="e3")
    if resp["reading"]["state"] != "active":
        failures.append("更正读数应为 active")
    st, rows = call("GET", f"/readings?meter_code={meter}")
    e3 = next(r for r in rows if r["event_id"] == f"e3-{sfx}")
    if e3["state"] != "superseded" or e3["superseded_by"] != f"c3-{sfx}":
        failures.append(f"e3 应被 c3 取代: {e3}")
    # 原事件再次被引用 → 409
    expect_error("POST", "/readings", {
        "tenant_code": tenant, "meter_code": meter,
        "event_id": f"x6-{sfx}", "read_at": "2026-02-02T00:00:00Z",
        "cumulative": "45", "corrects_event": f"e3-{sfx}",
    }, 409, "original_already_corrected")

    print("== 5. 关 2026-01 ==")
    # [1/1,1/16]=20；[1/16,2/2] 用量 24（44-20）跨月 16/17 → 1 月 22.588235，2 月 1.411765
    jan_share_q = (Decimal(24) * Decimal(16) / Decimal(17)).quantize(Decimal("0.000001"))
    feb_cross_q = Decimal(24) - jan_share_q
    st, jan = call("POST", "/bills/close", {"tenant_code": tenant, "period": "2026-01"}, expect=200)
    exp_jan_usage = Decimal(20) + jan_share_q
    if Decimal(jan["total_usage"]) != exp_jan_usage:
        failures.append(f"1 月用量应为 {exp_jan_usage}，实际 {jan['total_usage']}")
    exp_jan_amount = sum((Decimal(s["amount"]) for s in jan["segments"]), Decimal(0))
    if Decimal(jan["total_amount"]) != exp_jan_amount:
        failures.append(f"1 月金额应为 {exp_jan_amount}，实际 {jan['total_amount']}")
    # 重复关账幂等
    st, jan_again = call("POST", "/bills/close", {"tenant_code": tenant, "period": "2026-01"}, expect=200)
    if (jan_again["total_usage"], jan_again["total_amount"]) != (jan["total_usage"], jan["total_amount"]):
        failures.append("重复关账结果不一致")
    print(f"   1 月关账: 用量 {jan['total_usage']} 吨, 费用 {jan['total_amount']} 元")

    print("== 6. 关 2026-02（自然用量 + 异常快照）==")
    # 2 月自然 = 跨月 1/17 部分 + [2/2,2/5]=50（94-44，e4 回退被跳过，锚点仍 44）
    st, feb = call("POST", "/bills/close", {"tenant_code": tenant, "period": "2026-02"}, expect=200)
    exp_feb_usage = feb_cross_q + Decimal(50)
    if Decimal(feb["total_usage"]) != exp_feb_usage:
        failures.append(f"2 月自然用量应为 {exp_feb_usage}，实际 {feb['total_usage']}")
    if not any(a["reading_event"].startswith("e4") for a in feb["anomalies"]):
        failures.append("2 月账必须含 e4 累计回退异常明细")
    print(f"   2 月关账: 用量 {feb['total_usage']} 吨（跨月 {feb_cross_q} + 50），异常 {len(feb['anomalies'])} 条")

    print("== 7. 已关账读数更正：改 1 月首读数 0 -> 10，1 月自然 -10 吨，差额挂 3 月 ==")
    reading("c1", "2026-01-01T00:00:00Z", "10", corrects="e1")
    # 新 1 月：[1/1,1/16]=10；跨月区间 (44-10)=34 的 16/17 → 32
    new_jan_total = Decimal(10) + Decimal(32)
    jan_delta = new_jan_total - exp_jan_usage
    st, report = call("GET", f"/bills?tenant_code={tenant}")
    mar = find_month(report, "2026-03")
    if Decimal(mar["adjustment_usage"]) != jan_delta:
        failures.append(f"3 月挂账调整用量应为 {jan_delta}，实际 {mar['adjustment_usage']}")
    exp_adj_amount = (jan_delta * Decimal("3.5")).quantize(Decimal("0.0001"))
    if Decimal(mar["adjustment_amount"]) != exp_adj_amount:
        failures.append(f"3 月挂账调整金额应为 {exp_adj_amount}，实际 {mar['adjustment_amount']}")
    adj_seg = [s for s in mar["segments"] if s["kind"] == "adjustment"]
    if not adj_seg or adj_seg[0]["origin_period"] != "2026-01":
        failures.append("调整段必须关联原月账 2026-01")

    print("== 8. 迟到读数只改变段内分配、不改已关账总量时不产生调整 ==")
    # 迟到读数 1/8 = 15（在锚点 10 与 20 之间）：1 月总量保持 new_jan_total 不变
    reading("late1", "2026-01-08T00:00:00Z", "15")
    st, report = call("GET", f"/bills?tenant_code={tenant}")
    mar = find_month(report, "2026-03")
    if Decimal(mar["adjustment_usage"]) != jan_delta:
        failures.append(
            f"迟到读数未改 1 月总量时调整应不变 {jan_delta}，实际 {mar['adjustment_usage']}"
        )

    print("== 9. 关 3 月吃掉差额；重复/并发关账不重复入账 ==")
    st, mar_closed = call("POST", "/bills/close", {"tenant_code": tenant, "period": "2026-03"}, expect=200)
    if Decimal(mar_closed["natural_usage"]) != 0:
        failures.append(f"3 月自然用量应为 0，实际 {mar_closed['natural_usage']}")
    if Decimal(mar_closed["total_usage"]) != jan_delta:
        failures.append(f"3 月总量应等于调整 {jan_delta}，实际 {mar_closed['total_usage']}")
    with ThreadPoolExecutor(max_workers=5) as pool:
        rs = list(pool.map(
            lambda _: call("POST", "/bills/close", {"tenant_code": tenant, "period": "2026-03"}),
            range(5),
        ))
    if any(s != 200 for s, _ in rs):
        failures.append("并发重复关账应全部 200")
    st, report = call("GET", f"/bills?tenant_code={tenant}")
    mar3 = find_month(report, "2026-03")
    if Decimal(mar3["total_usage"]) != jan_delta:
        failures.append(f"并发关账后 3 月用量仍应为 {jan_delta}，实际 {mar3['total_usage']}")

    print("== 10. 3 月已关后再改 3 月用量：差额挂到下一未关账月（4 月）==")
    # 迟到读数 3/15 = 104：e6(94,3/1) -> 104 = 10，落在 3 月
    reading("late2", "2026-03-15T00:00:00Z", "104")
    # 并发提交同表不同时刻，全部成功
    def post_parallel(i):
        # 4/1..4/5 全部读数 104：零增量，不影响任何月份用量
        return reading(f"par{i}", f"2026-04-0{i+1}T00:00:00Z", "104")
    with ThreadPoolExecutor(max_workers=5) as pool:
        prs = list(pool.map(post_parallel, range(5)))
    if any(s != 200 for s, _ in prs):
        failures.append("并发提交不同事件应全部成功")

    # 同事件同内容并发提交：全部 200，只入库一条
    def post_same(_):
        return call("POST", "/readings", {
            "tenant_code": tenant, "meter_code": meter,
            "event_id": f"idem-{sfx}",
            "read_at": "2026-04-06T00:00:00Z", "cumulative": "104",
        })
    with ThreadPoolExecutor(max_workers=5) as pool:
        irs = list(pool.map(post_same, range(5)))
    if any(s != 200 for s, _ in irs):
        failures.append("同事件同内容并发提交应全部 200")
    st, rows = call("GET", f"/readings?meter_code={meter}")
    if sum(1 for r in rows if r["event_id"] == f"idem-{sfx}") != 1:
        failures.append("同事件并发提交后应只有一条读数")

    st, report = call("GET", f"/bills?tenant_code={tenant}")
    apr = find_month(report, "2026-04")
    if Decimal(apr["adjustment_usage"]) != Decimal(10):
        failures.append(f"4 月挂账调整应为 10 吨，实际 {apr['adjustment_usage']}")
    if Decimal(apr["adjustment_amount"]) != Decimal("35.0000"):
        failures.append(f"4 月挂账调整金额应为 35，实际 {apr['adjustment_amount']}")
    # 3 月快照保持不变（关账后不可改写）
    mar3 = find_month(report, "2026-03")
    if Decimal(mar3["total_usage"]) != jan_delta:
        failures.append("3 月关账快照不得被迟到读数改写")

    print("== 11. 跨费率按时长分摊（独立租户/水表；2026-05 内 48h，中点切价）==")
    call("POST", "/rates", {"effective_at": "2026-05-02T12:00:00Z", "price": "5.0000"}, expect=200)
    call("POST", "/readings", {
        "tenant_code": tenant2, "meter_code": meter2,
        "event_id": f"r1-{sfx}", "read_at": "2026-05-01T12:00:00Z", "cumulative": "0",
    }, expect=200)
    call("POST", "/readings", {
        "tenant_code": tenant2, "meter_code": meter2,
        "event_id": f"r2-{sfx}", "read_at": "2026-05-03T12:00:00Z", "cumulative": "24",
    }, expect=200)
    st, may = call("POST", "/bills/close", {"tenant_code": tenant2, "period": "2026-05"}, expect=200)
    by_price = {}
    for s in may["segments"]:
        by_price[Decimal(s["price"])] = by_price.get(Decimal(s["price"]), Decimal(0)) + Decimal(s["usage"])
    if by_price != {Decimal("3.5000"): Decimal("12.000000"), Decimal("5.0000"): Decimal("12.000000")}:
        failures.append(f"跨费率分摊错误: {by_price}")
    if Decimal(may["total_amount"]) != Decimal("102.0000"):  # 12*3.5 + 12*5
        failures.append(f"跨费率费用应为 102，实际 {may['total_amount']}")
    print("   12 吨@3.50 + 12 吨@5.00 = 102.00 元")

    print()
    if failures:
        print("❌ 自检失败：")
        for f in failures:
            print(" -", f)
        return 1
    print("✅ 全部断言通过")
    print(f"   完整报表: {BASE_URL}/bills?tenant_code={tenant}")
    print(f"   OpenAPI:  {BASE_URL}/docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
