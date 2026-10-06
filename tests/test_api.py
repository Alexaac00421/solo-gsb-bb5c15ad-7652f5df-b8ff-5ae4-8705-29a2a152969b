"""API 功能测试：在真实 PostgreSQL 上运行（DATABASE_URL 可覆盖）。"""

import uuid


def uid() -> str:
    return uuid.uuid4().hex[:12]


def mk_reading(tid, mid, event, ts, value, ref=None):
    body = {
        "tenant_id": tid,
        "meter_id": mid,
        "event_no": event,
        "collected_at": ts,
        "cumulative_value": value,
    }
    if ref:
        body["ref_event_no"] = ref
    return body


def get_bill(client, tid, month, meter_id=None):
    r = client.get(f"/tenants/{tid}/bills", params={"month": month})
    assert r.status_code == 200
    bills = r.json()["bills"]
    if meter_id is not None:
        bills = [b for b in bills if b["meter_id"] == meter_id]
    return bills[0] if bills else None


def test_allocation_across_month_and_rate(client, wm):
    tid, mid = wm
    assert client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-02-28T12:00:00Z", 100)).status_code == 201
    assert client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-03-02T12:00:00Z", 160)).status_code == 201

    feb = get_bill(client, tid, "2026-02", mid)
    mar = get_bill(client, tid, "2026-03", mid)
    # 48h 区间 60 吨：2 月 12h -> 15t @2.0；3 月 36h -> 45t @3.0
    assert feb["usage"] == 15.0 and feb["amount"] == 30.0
    assert mar["usage"] == 45.0 and mar["amount"] == 135.0
    assert feb["segments"][0]["price_per_unit"] == 2.0
    assert mar["segments"][0]["price_per_unit"] == 3.0
    assert feb["segments"][0]["period_end"] == "2026-03-01T00:00:00+00:00"


def test_idempotent_and_conflicting_events(client, wm):
    tid, mid = wm
    e2 = uid()
    client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-04-01T00:00:00Z", 10))
    assert client.post("/readings", json=mk_reading(tid, mid, e2, "2026-04-10T00:00:00Z", 20)).status_code == 201

    # 同事件同内容 -> 200 返回原结果
    r = client.post("/readings", json=mk_reading(tid, mid, e2, "2026-04-10T00:00:00Z", 20))
    assert r.status_code == 200 and r.json()["duplicate"] is True
    # 同事件异内容（值不同 / 时间不同）-> 409
    assert client.post("/readings", json=mk_reading(tid, mid, e2, "2026-04-10T00:00:00Z", 25)).status_code == 409
    assert client.post("/readings", json=mk_reading(tid, mid, e2, "2026-04-11T00:00:00Z", 20)).status_code == 409

    # 同时刻同值、不同事件 -> 接受为 duplicate，不影响用量
    r = client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-04-10T00:00:00Z", 20))
    assert r.status_code == 201 and r.json()["status"] == "duplicate"
    bill = get_bill(client, tid, "2026-04", mid)
    assert bill["usage"] == 10.0


def test_same_timestamp_different_value_requires_correction(client, wm):
    tid, mid = wm
    e1 = uid()
    client.post("/readings", json=mk_reading(tid, mid, e1, "2026-05-01T00:00:00Z", 100))
    # 同时刻异值、无引用 -> 409（提示可引用的原事件号）
    r = client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-05-01T00:00:00Z", 110))
    assert r.status_code == 409 and e1 in str(r.json())
    # 更正时刻与原读数不一致 -> 400
    assert client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-05-02T00:00:00Z", 110, ref=e1)).status_code == 400
    # 引用不存在的事件 -> 400
    assert client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-05-01T00:00:00Z", 110, ref="nope")).status_code == 400
    # 合法更正 -> 接受并替代原值
    assert client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-05-01T00:00:00Z", 110, ref=e1)).status_code == 201
    r = client.get(f"/tenants/{tid}/meters/{mid}/readings")
    statuses = {x["event_no"]: x["status"] for x in r.json()["readings"]}
    assert statuses[e1] == "superseded"


def test_out_of_order_and_anomaly_chaining(client, wm):
    tid, mid = wm
    # 乱序：先 6/10 再 6/01
    assert client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-06-10T00:00:00Z", 100)).status_code == 201
    assert client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-06-01T00:00:00Z", 90)).status_code == 201
    # 回退 -> 异常，不计费
    r = client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-06-15T00:00:00Z", 50))
    assert r.json()["status"] == "anomaly"
    # 后续有效读数跳过异常，衔接上一有效值 100
    r = client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-06-20T00:00:00Z", 130))
    assert r.json()["usage_from_prev"] == 30.0
    jun = get_bill(client, tid, "2026-06", mid)
    assert jun["usage"] == 40.0  # 10 + 30，异常的 50 不计
    assert len(jun["anomalies"]) == 1


def test_close_and_late_adjustment(client, wm):
    tid, mid = wm
    e2 = uid()
    client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-07-01T00:00:00Z", 0))
    client.post("/readings", json=mk_reading(tid, mid, e2, "2026-07-10T00:00:00Z", 100))

    r = client.post(f"/tenants/{tid}/bills/close", json={"month": "2026-07"})
    assert r.status_code == 200 and r.json()["already_closed"] is False
    jul = get_bill(client, tid, "2026-07", mid)
    assert jul["status"] == "closed" and jul["billed_usage"] == 100.0

    # 迟到更正改变已关账用量 -> 差额记入 8 月并关联 7 月账单
    client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-07-10T00:00:00Z", 120, ref=e2))
    jul = get_bill(client, tid, "2026-07", mid)
    assert jul["billed_usage"] == 100.0  # 已关账不改写
    aug = get_bill(client, tid, "2026-08", mid)
    assert len(aug["adjustments"]) == 1
    adj = aug["adjustments"][0]
    assert adj["source_month"] == "2026-07" and adj["usage_delta"] == 20.0
    assert adj["amount_delta"] == 20.0 * 3.0  # 7 月适用 3.0 费率

    # 重复关账幂等，不重复入账
    r = client.post(f"/tenants/{tid}/bills/close", json={"month": "2026-07"})
    assert r.json()["already_closed"] is True
    assert len(get_bill(client, tid, "2026-08", mid)["adjustments"]) == 1

    # 再次迟到更正 -> 只追加新增差额（10），不重复入账
    client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-07-10T00:00:00Z", 130, ref=e2))
    adjs = get_bill(client, tid, "2026-08", mid)["adjustments"]
    assert len(adjs) == 2 and adjs[1]["usage_delta"] == 10.0


def test_adjustment_skips_closed_months(client, wm):
    tid, mid = wm
    e2 = uid()
    client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-09-01T00:00:00Z", 0))
    client.post("/readings", json=mk_reading(tid, mid, e2, "2026-09-05T00:00:00Z", 10))
    client.post(f"/tenants/{tid}/bills/close", json={"month": "2026-09"})
    client.post(f"/tenants/{tid}/bills/close", json={"month": "2026-10"})
    # 9、10 月均关账 -> 9 月的迟到更正，调整落到下一未关账月（11 月）
    client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-09-05T00:00:00Z", 15, ref=e2))
    nov = get_bill(client, tid, "2026-11", mid)
    assert nov is not None and len(nov["adjustments"]) == 1
    assert nov["adjustments"][0]["source_month"] == "2026-09"
    assert nov["adjustments"][0]["usage_delta"] == 5.0


def test_validation_errors(client, wm):
    tid, mid = wm
    # 无时区 -> 400
    r = client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-01-05 10:00:00", 1))
    assert r.status_code == 400
    # 非法月份 -> 400
    assert client.get(f"/tenants/{tid}/bills", params={"month": "2026-13"}).status_code == 400
    assert client.post(f"/tenants/{tid}/bills/close", json={"month": "2026/01"}).status_code == 400
    # 租户/水表不存在 -> 404
    assert client.post("/readings", json=mk_reading(999999, mid, uid(), "2026-01-05T00:00:00Z", 1)).status_code == 404
    assert client.post("/readings", json=mk_reading(tid, 999999, uid(), "2026-01-05T00:00:00Z", 1)).status_code == 404
    # 水表与租户不匹配 -> 400
    t2 = client.post("/tenants", json={"name": f"t-{uid()}"}).json()
    r = client.post("/readings", json=mk_reading(t2["id"], mid, uid(), "2026-01-05T00:00:00Z", 1))
    assert r.status_code == 400
    assert client.get(f"/tenants/{t2['id']}/meters/{mid}/readings").status_code == 400
    # 负累计值 -> 400
    assert client.post("/readings", json=mk_reading(tid, mid, uid(), "2026-01-05T00:00:00Z", -1)).status_code == 400


def test_rate_endpoints_and_missing_rate(client, wm):
    # 同时刻同价 -> 幂等返回；异价 -> 409
    r = client.post("/rates", json={"effective_from": "2026-01-01T00:00:00Z", "price_per_unit": 2.0})
    assert r.status_code == 200
    r = client.post("/rates", json={"effective_from": "2026-01-01T00:00:00Z", "price_per_unit": 9.9})
    assert r.status_code == 409
    # 无费率覆盖的区间 -> 提交失败并明确报错（最早费率 2026-01-01）
    tid, mid = wm
    assert client.post("/readings", json=mk_reading(tid, mid, uid(), "2020-01-01T00:00:00Z", 0)).status_code == 201
    r = client.post("/readings", json=mk_reading(tid, mid, uid(), "2020-02-01T00:00:00Z", 10))
    assert r.status_code == 400 and "no rate effective" in r.json()["error"]
