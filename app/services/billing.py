"""计费核心：读数链重算、跨月/跨费率分摊、月账维护、关账与调整。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from ..models import Adjustment, Bill, BillSegment, Meter, Rate, Reading
from ..timeutil import add_months, as_utc, month_of, next_month_dt

Q6 = Decimal("0.000001")
ZERO = Decimal("0")


def D(x) -> Decimal:
    """数据库读出的数值统一转 Decimal（兼容不同后端的返回类型）。"""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def q6(x: Decimal) -> Decimal:
    return x.quantize(Q6)


class NoRateError(Exception):
    """某时刻没有任何已生效费率。"""

    def __init__(self, at: datetime):
        super().__init__(f"no rate effective at {at.isoformat()}")
        self.at = at


# ---------------------------------------------------------------- 并发控制


def lock_meter(session: Session, meter_id: int) -> None:
    """水表级事务咨询锁：串行化同一水表的读数提交/重算/关账。"""
    if session.bind and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(1, :id)"), {"id": meter_id})


def lock_tenant_close(session: Session, tenant_id: int) -> None:
    """租户级事务咨询锁：串行化同一租户的关账操作。"""
    if session.bind and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(2, :id)"), {"id": tenant_id})


# ---------------------------------------------------------------- 费率与分摊


def load_rates(session: Session) -> list[tuple[datetime, Decimal]]:
    rows = session.execute(select(Rate).order_by(Rate.effective_from)).scalars().all()
    return [(as_utc(r.effective_from), D(r.price_per_unit)) for r in rows]


def rate_at(rates: list[tuple[datetime, Decimal]], t: datetime) -> Decimal:
    price = None
    for eff, p in rates:
        if eff <= t:
            price = p
        else:
            break
    if price is None:
        raise NoRateError(t)
    return price


def allocate_interval(
    rates: list[tuple[datetime, Decimal]],
    start: datetime,
    end: datetime,
    usage: Decimal,
) -> list[dict]:
    """把 [start, end) 区间的用量按时长比例分摊到 UTC 月与费率分段。"""
    bounds = {start, end}
    cur = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    while True:
        cur = next_month_dt(cur)
        if cur >= end:
            break
        bounds.add(cur)
    for eff, _ in rates:
        if start < eff < end:
            bounds.add(eff)
    pts = sorted(bounds)
    total = Decimal((end - start).total_seconds())
    segments = []
    for a, b in zip(pts, pts[1:]):
        frac = Decimal((b - a).total_seconds()) / total
        seg_usage = q6(usage * frac)
        price = rate_at(rates, a)
        segments.append(
            {
                "month": month_of(a),
                "period_start": a,
                "period_end": b,
                "usage": seg_usage,
                "price": price,
                "amount": q6(seg_usage * price),
            }
        )
    return segments


# ---------------------------------------------------------------- 读数链重算


def recompute_meter(session: Session, meter_id: int) -> None:
    """全量重算一块水表：有效读数链 -> 异常标记 -> 区间分摊 -> 月账/调整。

    在同一事务内执行（调用方已持有该水表的咨询锁），保证并发下不重不漏。
    """
    readings = (
        session.execute(select(Reading).where(Reading.meter_id == meter_id).order_by(Reading.id))
        .scalars()
        .all()
    )
    if not readings:
        return

    # 1) 每个采集时刻取“有效读数”：最新更正优先，否则取首条原始读数
    by_ts: dict[datetime, list[Reading]] = defaultdict(list)
    for r in readings:
        by_ts[as_utc(r.collected_at)].append(r)

    effective: list[Reading] = []
    for ts, rs in by_ts.items():
        bases = [r for r in rs if r.ref_event_no is None]
        corrs = [r for r in rs if r.ref_event_no is not None]
        canonical = bases[0] if bases else None  # rs 已按 id 排序
        latest_corr = corrs[-1] if corrs else None
        eff = latest_corr or canonical
        for r in rs:
            if r is eff:
                continue
            r.usage_from_prev = None
            if r.ref_event_no is None and r is not canonical:
                r.status = "duplicate"
                r.note = f"same timestamp & value as event {canonical.event_no}"
            else:
                r.status = "superseded"
                r.note = f"superseded by event {eff.event_no}" if eff else None
        if eff is not None:
            effective.append(eff)

    # 2) 按采集时间串联：累计值回退 -> 异常，不参与计费；后续有效读数跳过它衔接
    effective.sort(key=lambda r: (as_utc(r.collected_at), r.id))
    prev: Reading | None = None
    for r in effective:
        if prev is None:
            r.status = "valid"
            r.usage_from_prev = None  # 基线读数
            r.note = None
            prev = r
            continue
        diff = D(r.cumulative_value) - D(prev.cumulative_value)
        if diff < 0:
            r.status = "anomaly"
            r.usage_from_prev = None
            r.note = (
                f"cumulative value regressed: {q6(D(r.cumulative_value))} < previous valid "
                f"{q6(D(prev.cumulative_value))} (event {prev.event_no}); excluded from billing"
            )
        else:
            r.status = "valid"
            r.usage_from_prev = q6(diff)
            r.note = None
            prev = r

    # 3) 相邻有效读数区间 -> 跨月/跨费率分摊
    valids = [r for r in effective if r.status == "valid"]
    rates = load_rates(session)
    segs_by_month: dict[date, list[dict]] = defaultdict(list)
    for a, b in zip(valids, valids[1:]):
        usage = D(b.usage_from_prev)
        if usage == 0:
            continue
        for s in allocate_interval(rates, as_utc(a.collected_at), as_utc(b.collected_at), usage):
            segs_by_month[s["month"]].append(s)

    _apply_to_bills(session, meter_id, segs_by_month)


def _apply_to_bills(session: Session, meter_id: int, segs_by_month: dict[date, list[dict]]) -> None:
    tenant_id = session.get(Meter, meter_id).tenant_id
    bills = {
        b.month: b
        for b in session.execute(select(Bill).where(Bill.meter_id == meter_id)).scalars().all()
    }
    for month in sorted(set(segs_by_month) | set(bills)):
        segs = segs_by_month.get(month, [])
        usage = sum((s["usage"] for s in segs), ZERO)
        amount = sum((s["amount"] for s in segs), ZERO)
        bill = bills.get(month)
        if bill is None:
            if not segs:
                continue
            bill = Bill(
                tenant_id=tenant_id, meter_id=meter_id, month=month,
                status="open", usage=usage, amount=amount,
            )
            session.add(bill)
            session.flush()
        if bill.status == "open":
            # 未关账：直接刷新用量与费用分段
            bill.usage = usage
            bill.amount = amount
            session.execute(delete(BillSegment).where(BillSegment.bill_id == bill.id))
            for s in segs:
                session.add(
                    BillSegment(
                        bill_id=bill.id,
                        period_start=s["period_start"],
                        period_end=s["period_end"],
                        usage=s["usage"],
                        price_per_unit=s["price"],
                        amount=s["amount"],
                    )
                )
        else:
            # 已关账：快照不可改写，差额（扣除已入账调整）记入下一未关账月份
            d_usage = usage - D(bill.billed_usage)
            d_amount = amount - D(bill.billed_amount)
            posted_u, posted_a = session.execute(
                select(
                    func.coalesce(func.sum(Adjustment.usage_delta), 0),
                    func.coalesce(func.sum(Adjustment.amount_delta), 0),
                ).where(Adjustment.source_bill_id == bill.id)
            ).one()
            need_u = q6(d_usage - D(posted_u))
            need_a = q6(d_amount - D(posted_a))
            if need_u != 0 or need_a != 0:
                target_month = _next_unclosed_month(session, meter_id, month)
                target = _get_or_create_open_bill(session, meter_id, tenant_id, target_month)
                session.add(
                    Adjustment(
                        tenant_id=tenant_id,
                        meter_id=meter_id,
                        bill_id=target.id,
                        source_bill_id=bill.id,
                        source_month=month,
                        usage_delta=need_u,
                        amount_delta=need_a,
                        reason=(
                            f"late/corrected readings changed closed month "
                            f"{month.isoformat()[:7]}: usage {need_u:+}, amount {need_a:+}"
                        ),
                    )
                )
    session.flush()


def _next_unclosed_month(session: Session, meter_id: int, month: date) -> date:
    m = add_months(month)
    for _ in range(2400):
        b = session.execute(
            select(Bill).where(Bill.meter_id == meter_id, Bill.month == m)
        ).scalar_one_or_none()
        if b is None or b.status == "open":
            return m
        m = add_months(m)
    raise RuntimeError("no unclosed month found within 200 years")


def _get_or_create_open_bill(session: Session, meter_id: int, tenant_id: int, month: date) -> Bill:
    bill = session.execute(
        select(Bill).where(Bill.meter_id == meter_id, Bill.month == month)
    ).scalar_one_or_none()
    if bill is None:
        bill = Bill(
            tenant_id=tenant_id, meter_id=meter_id, month=month,
            status="open", usage=ZERO, amount=ZERO,
        )
        session.add(bill)
        session.flush()
    if bill.status != "open":
        raise RuntimeError(f"bill for {month} is closed; cannot post adjustment")
    return bill


# ---------------------------------------------------------------- 关账


def close_month(session: Session, tenant_id: int, month: date) -> tuple[list[Bill], bool]:
    """关闭租户某月全部水表月账。幂等：已关账的保持原样，返回 already_closed=True。

    关账 = 把当前用量/费用固化为 billed_* 快照，之后该月不可改写；
    重复关账与并发关账由租户锁 + 状态判断保证不重复入账。
    """
    lock_tenant_close(session, tenant_id)
    meters = (
        session.execute(select(Meter).where(Meter.tenant_id == tenant_id).order_by(Meter.id))
        .scalars()
        .all()
    )
    bills: list[Bill] = []
    already = True
    now = datetime.now(timezone.utc)
    for m in meters:
        lock_meter(session, m.id)
        bill = session.execute(
            select(Bill).where(Bill.meter_id == m.id, Bill.month == month)
        ).scalar_one_or_none()
        if bill is None:
            bill = Bill(
                tenant_id=tenant_id, meter_id=m.id, month=month,
                status="open", usage=ZERO, amount=ZERO,
            )
            session.add(bill)
            session.flush()
        if bill.status == "open":
            bill.status = "closed"
            bill.billed_usage = bill.usage
            bill.billed_amount = bill.amount
            bill.closed_at = now
            already = False
        bills.append(bill)
    session.flush()
    return bills, already
