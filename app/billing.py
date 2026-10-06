"""月账关账、差额调整与报表组装。

差额调整模型
------------
设某已关账月 P，按**当前数据**重算得到的自然用量/费用为 cur，
关账快照为 frozen，delta = cur - frozen。
每次关账月 M 时，对每个 P < M 的已关账月，只把“尚未入账的增量”
（delta 减去该来源月之前已调整的合计）作为 adjustment 段记入 M：

* 第一个晚于 P 关账的月份吃掉全部 delta；
* 之后再来的更正只补增量，避免重复入账。

调整段按差额来源月分行，kind='adjustment'，origin_period=P。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import Connection

from .settlement import (
    AMOUNT_ZERO,
    USAGE_ZERO,
    SettlementError,
    compute_tenant,
    quantize_amount,
    quantize_usage,
)


def parse_period(text: str) -> date:
    try:
        parsed = datetime.strptime(text, "%Y-%m")
    except ValueError:
        raise SettlementError("invalid_period", "期间格式必须为 YYYY-MM")
    return date(parsed.year, parsed.month, 1)


# ---------------------------------------------------------------- 持久化辅助

def _insert_segment(conn: Connection, bill_id: int, seg: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO bill_segments
          (bill_id, kind, start_at, end_at, rate_effective_at,
           price, usage, amount, origin_period, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            bill_id,
            seg["kind"],
            seg.get("start_at"),
            seg.get("end_at"),
            seg.get("rate_effective_at"),
            quantize_amount(seg["price"]),
            quantize_usage(seg["usage"]),
            quantize_amount(seg["amount"]),
            seg.get("origin_period"),
            seg.get("note"),
        ),
    )


def _insert_anomaly(conn: Connection, bill_id: int, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO bill_anomalies
          (bill_id, reading_event, meter_code, read_at, cumulative,
           anchor_event, anchor_value, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            bill_id,
            row["reading_event"],
            row["meter_code"],
            row["read_at"],
            row["cumulative"],
            row.get("anchor_event"),
            row.get("anchor_value"),
            row["reason"],
        ),
    )


def _posted_adjustments(conn: Connection, tenant_id: int) -> dict[date, dict[str, Decimal]]:
    """每个已关账来源月此前已经入账的调整合计。"""
    rows = conn.execute(
        """
        SELECT b.period AS target_period, s.origin_period AS origin_period,
               SUM(s.usage) AS usage, SUM(s.amount) AS amount
        FROM bill_segments s
        JOIN bills b ON b.id = s.bill_id
        WHERE b.tenant_id = %s AND s.kind = 'adjustment'
        GROUP BY b.period, s.origin_period
        """,
        (tenant_id,),
    ).fetchall()
    result: dict[date, dict[str, Decimal]] = {}
    for row in rows:
        origin = row["origin_period"]
        entry = result.setdefault(origin, {"usage": USAGE_ZERO, "amount": AMOUNT_ZERO})
        entry["usage"] += row["usage"]
        entry["amount"] += row["amount"]
    return result


# ---------------------------------------------------------------- 关账

@dataclass
class CloseResult:
    period: date
    status: str
    bill: dict[str, Any]
    segments: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]


def close_month(conn: Connection, tenant_id: int, period: date) -> CloseResult:
    # 已关账：重复关账幂等返回原快照（FOR UPDATE 串行并发关账）
    existing = conn.execute(
        "SELECT * FROM bills WHERE tenant_id = %s AND period = %s FOR UPDATE",
        (tenant_id, period),
    ).fetchone()
    if existing and existing["status"] == "closed":
        segs = _load_segments(conn, existing["id"])
        anoms = _load_anomalies(conn, existing["id"])
        return CloseResult(
            period=period, status="closed", bill=dict(existing),
            segments=segs, anomalies=anoms,
        )
    if existing:
        # 理论不可达（月账行只以 closed 创建），防御性处理
        raise SettlementError("bill_bad_state", "月账状态异常，请联系管理员")

    computed = compute_tenant(conn, tenant_id)
    posted = _posted_adjustments(conn, tenant_id)

    natural_segments: list[dict[str, Any]] = []
    natural_usage = USAGE_ZERO
    natural_amount = AMOUNT_ZERO
    nat = computed["natural"].get(period)
    if nat:
        for seg in nat["segments"]:
            stored = dict(seg)
            stored["kind"] = "natural"
            natural_segments.append(stored)
        natural_usage = nat["usage"]
        natural_amount = nat["amount"]

    # 已关账来源月的增量差额全部计入本月。
    # 基准用关账时冻结的 **natural**（不含历史调整），避免多期滚动时重复计算。
    adjustments: list[dict[str, Any]] = []
    adj_usage = USAGE_ZERO
    adj_amount = AMOUNT_ZERO
    closed_rows = conn.execute(
        """
        SELECT id, period, natural_usage, natural_amount
        FROM bills
        WHERE tenant_id = %s AND status = 'closed' AND period < %s
        ORDER BY period
        """,
        (tenant_id, period),
    ).fetchall()
    for closed in closed_rows:
        origin = closed["period"]
        cur = computed["natural"].get(origin)
        cur_usage = cur["usage"] if cur else USAGE_ZERO
        cur_amount = cur["amount"] if cur else AMOUNT_ZERO
        d_usage = quantize_usage(cur_usage - closed["natural_usage"])
        d_amount = quantize_amount(cur_amount - closed["natural_amount"])
        already = posted.get(origin, {"usage": USAGE_ZERO, "amount": AMOUNT_ZERO})
        inc_usage = quantize_usage(d_usage - already["usage"])
        inc_amount = quantize_amount(d_amount - already["amount"])
        if inc_usage == 0 and inc_amount == 0:
            continue
        adjustments.append(
            {
                "kind": "adjustment",
                "price": AMOUNT_ZERO,
                "usage": inc_usage,
                "amount": inc_amount,
                "origin_period": origin,
                "note": (
                    f"已关账月 {origin.strftime('%Y-%m')} 因迟到/更正读数产生的差额"
                ),
            }
        )
        adj_usage += inc_usage
        adj_amount += inc_amount

    total_usage = quantize_usage(natural_usage + adj_usage)
    total_amount = quantize_amount(natural_amount + adj_amount)
    anomalies = list(computed["anomalies"].get(period, []))

    # 咨询锁已保证同租户关账串行，直接插入关账快照行
    row = conn.execute(
        """
        INSERT INTO bills
          (tenant_id, period, status,
           natural_usage, natural_amount, total_usage, total_amount, closed_at)
        VALUES (%s, %s, 'closed', %s, %s, %s, %s, now())
        RETURNING id
        """,
        (tenant_id, period, natural_usage, natural_amount, total_usage, total_amount),
    ).fetchone()
    bill_id = row["id"]

    for seg in natural_segments:
        _insert_segment(conn, bill_id, seg)
    for seg in adjustments:
        _insert_segment(conn, bill_id, seg)
    for anom in anomalies:
        _insert_anomaly(conn, bill_id, anom)

    bill = conn.execute("SELECT * FROM bills WHERE id = %s", (bill_id,)).fetchone()
    return CloseResult(
        period=period, status="closed", bill=dict(bill),
        segments=natural_segments + adjustments,
        anomalies=anomalies,
    )


# ---------------------------------------------------------------- 读取与报表

def _load_segments(conn: Connection, bill_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM bill_segments WHERE bill_id = %s "
            "ORDER BY kind, start_at NULLS LAST, origin_period NULLS FIRST",
            (bill_id,),
        ).fetchall()
    ]


def _load_anomalies(conn: Connection, bill_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM bill_anomalies WHERE bill_id = %s ORDER BY read_at",
            (bill_id,),
        ).fetchall()
    ]


def _pending_adjustments(
    conn: Connection, tenant_id: int, computed: dict[str, Any]
) -> list[dict[str, Any]]:
    """截至当下，所有已关账月尚未被任何月账吃掉的差额（挂在下一个未关账月）。"""
    posted = _posted_adjustments(conn, tenant_id)
    rows = conn.execute(
        """
        SELECT id, period, natural_usage, natural_amount
        FROM bills WHERE tenant_id = %s AND status = 'closed' ORDER BY period
        """,
        (tenant_id,),
    ).fetchall()
    pending: list[dict[str, Any]] = []
    for row in rows:
        origin = row["period"]
        cur = computed["natural"].get(origin)
        cur_usage = cur["usage"] if cur else USAGE_ZERO
        cur_amount = cur["amount"] if cur else AMOUNT_ZERO
        d_usage = quantize_usage(cur_usage - row["natural_usage"])
        d_amount = quantize_amount(cur_amount - row["natural_amount"])
        already = posted.get(origin, {"usage": USAGE_ZERO, "amount": AMOUNT_ZERO})
        inc_usage = quantize_usage(d_usage - already["usage"])
        inc_amount = quantize_amount(d_amount - already["amount"])
        if inc_usage == 0 and inc_amount == 0:
            continue
        pending.append(
            {
                "kind": "adjustment",
                "price": AMOUNT_ZERO,
                "usage": inc_usage,
                "amount": inc_amount,
                "origin_period": origin,
                "note": f"关联已关账月 {origin.strftime('%Y-%m')} 的差额，待下一未关账月入账",
                "pending": True,
            }
        )
    return pending


def build_report(conn: Connection, tenant_id: int) -> dict[str, Any]:
    """各月用量、费用分段、调整与异常明细（关账月取快照，未关账月实时算）。"""
    bills = {
        row["period"]: row
        for row in conn.execute(
            "SELECT * FROM bills WHERE tenant_id = %s ORDER BY period", (tenant_id,)
        ).fetchall()
    }
    computed = compute_tenant(conn, tenant_id)
    pending = _pending_adjustments(conn, tenant_id, computed)

    closed_periods = {p for p, b in bills.items() if b["status"] == "closed"}
    open_periods = [p for p in set(bills) | set(computed["periods"]) if p not in closed_periods]
    # 已关账月产生了差额但还没有任何后续未关账月时，合成一个月用于展示挂账
    synthetic_period: date | None = None
    if pending and not open_periods and closed_periods:
        latest_closed = max(closed_periods)
        synthetic_period = (latest_closed + timedelta(days=32)).replace(day=1)
    attach_period = max(open_periods) if open_periods else synthetic_period

    months: list[dict[str, Any]] = []
    all_periods = sorted(set(bills) | set(computed["periods"]) | ({synthetic_period} if synthetic_period else set()))
    for period in all_periods:
        bill = bills.get(period)
        if bill and bill["status"] == "closed":
            segments = _load_segments(conn, bill["id"])
            anomalies = _load_anomalies(conn, bill["id"])
            natural_segments = [s for s in segments if s["kind"] == "natural"]
            adjustments = [s for s in segments if s["kind"] == "adjustment"]
            months.append(
                {
                    "period": period.strftime("%Y-%m"),
                    "status": "closed",
                    "closed_at": bill["closed_at"],
                    "natural_usage": bill["natural_usage"],
                    "natural_amount": bill["natural_amount"],
                    "adjustment_usage": sum(
                        (s["usage"] for s in adjustments), USAGE_ZERO
                    ),
                    "adjustment_amount": sum(
                        (s["amount"] for s in adjustments), AMOUNT_ZERO
                    ),
                    "total_usage": bill["total_usage"],
                    "total_amount": bill["total_amount"],
                    "segments": segments,
                    "anomalies": anomalies,
                }
            )
        else:
            nat = computed["natural"].get(period)
            natural_segments = list(nat["segments"]) if nat else []
            natural_usage = nat["usage"] if nat else USAGE_ZERO
            natural_amount = nat["amount"] if nat else AMOUNT_ZERO
            # 未关账的实时视图：挂账调整归属“最新一个未关账月”展示
            attach_pending = period == attach_period
            adjustments = pending if attach_pending else []
            months.append(
                {
                    "period": period.strftime("%Y-%m"),
                    "status": "open",
                    "closed_at": None,
                    "natural_usage": natural_usage,
                    "natural_amount": natural_amount,
                    "adjustment_usage": sum(
                        (s["usage"] for s in adjustments), USAGE_ZERO
                    ),
                    "adjustment_amount": sum(
                        (s["amount"] for s in adjustments), AMOUNT_ZERO
                    ),
                    "total_usage": quantize_usage(
                        natural_usage
                        + sum((s["usage"] for s in adjustments), USAGE_ZERO)
                    ),
                    "total_amount": quantize_amount(
                        natural_amount
                        + sum((s["amount"] for s in adjustments), AMOUNT_ZERO)
                    ),
                    "segments": natural_segments + adjustments,
                    "anomalies": computed["anomalies"].get(period, []),
                }
            )
    return {"tenant_id": tenant_id, "months": months}


def get_month(conn: Connection, tenant_id: int, period: date) -> dict[str, Any] | None:
    for month in build_report(conn, tenant_id)["months"]:
        if month["period"] == period.strftime("%Y-%m"):
            return month
    return None
