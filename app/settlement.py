"""结算引擎：乱序读数归序、异常识别、跨 UTC 月/跨费率按时长分摊。

规则要点
--------
* 仅 ``state = active`` 的读数参与结算；同表同时刻只保留最新更正后的值。
* 按采集时间排序，首读数为初始锚点（不产生用量）。
* 相邻读数差额 = 当前累计值 - 锚点累计值：
    - 差额 < 0：累计值回退，标记异常、不参与计费，锚点保持不变
      （后续有效读数仍与该锚点衔接）；
    - 差额 = 0：用量为 0，该读数成为新锚点；
    - 差额 > 0：区间用量按子区间时长比例分摊。
* 子区间切点 = UTC 月界 + 费率生效时刻；费率取子区间中点时刻的生效价。
* 用量精度 1e-6（吨），金额精度 1e-4（元）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from psycopg import Connection

USAGE_Q = Decimal("0.000001")
AMOUNT_Q = Decimal("0.0001")
USAGE_ZERO = Decimal("0.000000")
AMOUNT_ZERO = Decimal("0.0000")


class SettlementError(Exception):
    """结算期不可结算（如某时刻没有任何生效费率）。"""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise SettlementError("naive_datetime", "时间必须带时区")
    return dt.astimezone(timezone.utc)


def month_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)


def period_of(dt: datetime) -> date:
    return month_start(dt).date()


def next_month_start(d: datetime.date) -> datetime:
    if d.month == 12:
        return datetime(d.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(d.year, d.month + 1, 1, tzinfo=timezone.utc)


def quantize_usage(value: Decimal) -> Decimal:
    return value.quantize(USAGE_Q)


def quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_Q)


@dataclass
class RateSchedule:
    rows: list[dict[str, Any]]  # 按 effective_at 升序

    def price_at(self, at: datetime) -> tuple[Decimal, datetime]:
        """返回 at 时刻的生效费率及其生效时间；无可用费率时报错。"""
        chosen: dict[str, Any] | None = None
        for row in self.rows:
            if row["effective_at"] <= at:
                chosen = row
            else:
                break
        if chosen is None:
            raise SettlementError(
                "no_applicable_rate",
                f"时刻 {at.isoformat()} 没有任何生效费率，请先补录费率",
                {"at": at.isoformat()},
            )
        return chosen["price"], chosen["effective_at"]


@dataclass
class MeterSettlement:
    segments: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)


def _load_rate_schedule(conn: Connection) -> RateSchedule:
    rows = conn.execute(
        "SELECT effective_at, price FROM rates ORDER BY effective_at"
    ).fetchall()
    return RateSchedule(rows=rows)


def _cuts_between(start: datetime, end: datetime, rates: RateSchedule) -> list[datetime]:
    """返回 (start, end) 内部的全部切点：UTC 月界 + 费率生效时刻。"""
    cuts: list[datetime] = []
    m = next_month_start(month_start(start).date())
    # m 初始即下一个月界，只要严格落在区间内部就计入
    while m <= start:
        m = next_month_start(m.date())
    while m < end:
        cuts.append(m)
        m = next_month_start(m.date())
    for row in rates.rows:
        eff = row["effective_at"]
        if start < eff < end:
            cuts.append(eff)
    cuts.sort()
    return cuts


def split_interval(
    start: datetime,
    end: datetime,
    usage: Decimal,
    meter_code: str,
    rates: RateSchedule,
) -> list[dict[str, Any]]:
    """把一个读表区间的用量按 UTC 月界/费率界按时长比例拆分。"""
    total_seconds = Decimal((end - start).total_seconds())
    if total_seconds <= 0:
        # 同表同时刻有唯一约束，正常不会走到这里
        return []

    cuts = _cuts_between(start, end, rates)
    boundaries = [start, *cuts, end]

    pieces: list[dict[str, Any]] = []
    for s, e in zip(boundaries, boundaries[1:]):
        seconds = Decimal((e - s).total_seconds())
        share = quantize_usage(usage * seconds / total_seconds)
        midpoint = s + (e - s) / 2
        price, rate_effective_at = rates.price_at(midpoint)
        pieces.append(
            {
                "meter_code": meter_code,
                "start_at": s,
                "end_at": e,
                "rate_effective_at": rate_effective_at,
                "price": price,
                "usage": share,
                "amount": quantize_amount(share * price),
            }
        )

    # 修正四舍五入残差，保证同一区间分摊用量之和恰好等于差额
    residual = usage - sum((p["usage"] for p in pieces), Decimal(0))
    if residual and pieces:
        pieces[-1]["usage"] = quantize_usage(pieces[-1]["usage"] + residual)
        pieces[-1]["amount"] = quantize_amount(pieces[-1]["usage"] * pieces[-1]["price"])
    return pieces


def compute_tenant(conn: Connection, tenant_id: int) -> dict[str, Any]:
    """计算租户当前全部水表的自然用量分段与异常（不含已关账调整）。

    返回::

        {
          "natural": {period: {"segments": [...], "usage": d, "amount": d}},
          "anomalies": {period: [anomaly, ...]},
          "periods": [date, ...],
        }
    """
    rates = _load_rate_schedule(conn)
    meters = conn.execute(
        "SELECT id, code FROM meters WHERE tenant_id = %s ORDER BY code",
        (tenant_id,),
    ).fetchall()

    natural: dict[Any, dict[str, Any]] = {}
    anomalies: dict[Any, list[dict[str, Any]]] = {}

    def bucket(period):
        return natural.setdefault(
            period, {"segments": [], "usage": USAGE_ZERO, "amount": AMOUNT_ZERO}
        )

    for meter in meters:
        rows = conn.execute(
            """
            SELECT event_id, read_at, cumulative
            FROM readings
            WHERE meter_id = %s AND state = 'active'
            ORDER BY read_at, id
            """,
            (meter["id"],),
        ).fetchall()

        anchor: dict[str, Any] | None = None
        for row in rows:
            read_at = as_utc(row["read_at"])
            if anchor is None:
                anchor = row
                continue
            anchor_at = as_utc(anchor["read_at"])
            delta = row["cumulative"] - anchor["cumulative"]

            if delta < 0:
                period = period_of(read_at)
                anomalies.setdefault(period, []).append(
                    {
                        "reading_event": row["event_id"],
                        "meter_code": meter["code"],
                        "read_at": read_at,
                        "cumulative": row["cumulative"],
                        "anchor_event": anchor["event_id"],
                        "anchor_value": anchor["cumulative"],
                        "reason": (
                            f"累计值回退：读数 {row['cumulative']} 小于锚点 "
                            f"{anchor['cumulative']}（事件 {anchor['event_id']}），"
                            "该读数标记异常且不参与计费"
                        ),
                    }
                )
                # 锚点保持不变：后续有效读数仍须与回退前的有效读数衔接
                continue

            if delta == 0:
                anchor = row
                continue

            pieces = split_interval(
                anchor_at, read_at, delta, meter["code"], rates
            )
            for piece in pieces:
                period = period_of(piece["start_at"])
                b = bucket(period)
                b["segments"].append(piece)
                b["usage"] += piece["usage"]
                b["amount"] += piece["amount"]
            anchor = row

    for data in natural.values():
        data["usage"] = quantize_usage(data["usage"])
        data["amount"] = quantize_amount(data["amount"])
        data["segments"].sort(key=lambda p: (p["start_at"], p["meter_code"]))

    periods = sorted(set(natural) | set(anomalies))
    return {"natural": natural, "anomalies": anomalies, "periods": periods}
