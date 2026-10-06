"""DB 行 -> API JSON 的组装（Decimal/datetime 在此统一转成可序列化类型）。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.astimezone().isoformat() if dt.tzinfo else dt.isoformat()
    return str(dt)


def money(value: Any) -> str:
    return str(value if value is not None else Decimal("0"))


def tenant_out(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "code": row["code"], "name": row["name"]}


def meter_out(row: dict[str, Any], tenant_code: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "tenant_code": tenant_code,
        "name": row["name"],
        "active": row["active"],
    }


def rate_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "effective_at": _iso(row["effective_at"]),
        "price": money(row["price"]),
    }


def segment_out(seg: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": seg["kind"],
        "meter_code": seg.get("meter_code"),
        "start_at": _iso(seg.get("start_at")),
        "end_at": _iso(seg.get("end_at")),
        "rate_effective_at": _iso(seg.get("rate_effective_at")),
        "price": money(seg.get("price")),
        "usage": money(seg.get("usage")),
        "amount": money(seg.get("amount")),
        "origin_period": seg["origin_period"].strftime("%Y-%m")
        if seg.get("origin_period")
        else None,
        "note": seg.get("note"),
    }


def anomaly_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "reading_event": row["reading_event"],
        "meter_code": row["meter_code"],
        "read_at": _iso(row["read_at"]),
        "cumulative": money(row["cumulative"]),
        "anchor_event": row.get("anchor_event"),
        "anchor_value": money(row["anchor_value"])
        if row.get("anchor_value") is not None
        else None,
        "reason": row["reason"],
    }


def month_out(month: dict[str, Any]) -> dict[str, Any]:
    return {
        "period": month["period"],
        "status": month["status"],
        "closed_at": _iso(month.get("closed_at")),
        "natural_usage": money(month.get("natural_usage")),
        "natural_amount": money(month.get("natural_amount")),
        "adjustment_usage": money(month.get("adjustment_usage")),
        "adjustment_amount": money(month.get("adjustment_amount")),
        "total_usage": money(month.get("total_usage")),
        "total_amount": money(month.get("total_amount")),
        "segments": [segment_out(s) for s in month.get("segments", [])],
        "anomalies": [anomaly_out(a) for a in month.get("anomalies", [])],
    }


def report_out(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": report["tenant_id"],
        "months": [month_out(m) for m in report["months"]],
    }
