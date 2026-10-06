"""园区分表用水结算服务 · FastAPI 入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timezone
from typing import Any

from fastapi import Depends, FastAPI, Query
from psycopg import Connection
from psycopg.errors import UniqueViolation

from . import billing, serializers
from .billing import close_month, get_month, parse_period
from .db import get_connection, init_pool, tenant_lock
from .errors import ApiError, register_exception_handlers
from .models import (
    CloseRequest,
    HealthOut,
    MeterCreate,
    RateCreate,
    ReadingCreate,
    TenantCreate,
)
from .services import list_readings, submit_reading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield


app = FastAPI(
    title="园区分表用水结算服务",
    version="1.0.0",
    description="水表绑定租户；读数幂等/冲突/更正；跨 UTC 月与跨费率按用量时长比例分摊；月账关账与差额调整。",
    lifespan=lifespan,
)
register_exception_handlers(app)


# ---------------------------------------------------------------- 基础

@app.get("/health", response_model=HealthOut, tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "water-billing"}


@app.post("/tenants", tags=["租户"], summary="创建租户")
def create_tenant(
    body: TenantCreate, conn: Connection = Depends(get_connection)
) -> dict[str, Any]:
    try:
        row = conn.execute(
            "INSERT INTO tenants (code, name) VALUES (%s, %s) RETURNING *",
            (body.code, body.name),
        ).fetchone()
        conn.commit()
    except UniqueViolation:
        conn.rollback()
        raise ApiError(409, "tenant_exists", f"租户编码已存在: {body.code}")
    return serializers.tenant_out(dict(row))


@app.get("/tenants", tags=["租户"], summary="租户列表")
def get_tenants(conn: Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM tenants ORDER BY code").fetchall()
    return [serializers.tenant_out(dict(r)) for r in rows]


@app.post("/meters", tags=["水表"], summary="登记水表并绑定租户")
def create_meter(
    body: MeterCreate, conn: Connection = Depends(get_connection)
) -> dict[str, Any]:
    tenant = conn.execute("SELECT * FROM tenants WHERE code = %s", (body.tenant_code,)).fetchone()
    if tenant is None:
        raise ApiError(404, "tenant_not_found", f"租户不存在: {body.tenant_code}")
    try:
        row = conn.execute(
            "INSERT INTO meters (code, tenant_id, name) VALUES (%s, %s, %s) RETURNING *",
            (body.code, tenant["id"], body.name),
        ).fetchone()
        conn.commit()
    except UniqueViolation:
        conn.rollback()
        raise ApiError(409, "meter_exists", f"水表编码已存在: {body.code}")
    return serializers.meter_out(dict(row), body.tenant_code)


@app.get("/meters", tags=["水表"], summary="水表列表（含绑定租户）")
def get_meters(conn: Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.*, t.code AS tenant_code
        FROM meters m JOIN tenants t ON t.id = m.tenant_id
        ORDER BY t.code, m.code
        """
    ).fetchall()
    return [serializers.meter_out(dict(r), dict(r)["tenant_code"]) for r in rows]


# ---------------------------------------------------------------- 费率

@app.post("/rates", tags=["费率"], summary="新增费率版本（按生效时间分段）")
def create_rate(
    body: RateCreate, conn: Connection = Depends(get_connection)
) -> dict[str, Any]:
    effective_at = body.effective_at.astimezone(timezone.utc)
    # 费率是全局对象，用固定键的咨询锁避免与关账并发
    conn.execute("SELECT pg_advisory_xact_lock(9157, 0)")
    try:
        row = conn.execute(
            "INSERT INTO rates (effective_at, price) VALUES (%s, %s) RETURNING *",
            (effective_at, body.price),
        ).fetchone()
        conn.commit()
    except UniqueViolation:
        conn.rollback()
        raise ApiError(
            409,
            "rate_effective_at_exists",
            f"该生效时刻已存在费率: {effective_at.isoformat()}",
        )
    return serializers.rate_out(dict(row))


@app.get("/rates", tags=["费率"], summary="费率分段列表")
def get_rates(conn: Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM rates ORDER BY effective_at").fetchall()
    return [serializers.rate_out(dict(r)) for r in rows]


# ---------------------------------------------------------------- 读数

@app.post(
    "/readings",
    tags=["读数"],
    summary="提交读数（幂等；冲突 409；同时刻异值须以新事件号更正）",
)
def post_reading(
    body: ReadingCreate, conn: Connection = Depends(get_connection)
) -> dict[str, Any]:
    tenant = conn.execute("SELECT id FROM tenants WHERE code = %s", (body.tenant_code,)).fetchone()
    if tenant is None:
        raise ApiError(404, "tenant_not_found", f"租户不存在: {body.tenant_code}")
    with tenant_lock(conn, tenant["id"]):
        result = submit_reading(conn, body)
        conn.commit()
    out = dict(result["reading"])
    out["read_at"] = out["read_at"].isoformat()
    out["cumulative"] = str(out["cumulative"])
    return {
        "deduplicated": result["deduplicated"],
        "message": result.get("message", "读数已接收"),
        "reading": out,
    }


@app.get("/readings", tags=["读数"], summary="查询读数及派生状态（active/anomaly/superseded）")
def get_readings(
    tenant_code: str | None = Query(default=None),
    meter_code: str | None = Query(default=None),
    conn: Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    rows = list_readings(conn, tenant_code=tenant_code, meter_code=meter_code)
    for row in rows:
        row["read_at"] = row["read_at"].isoformat()
        row["cumulative"] = str(row["cumulative"])
    return rows


# ---------------------------------------------------------------- 月账

@app.post("/bills/close", tags=["月账"], summary="关账（重复关账幂等；并发安全；差额挂下一未关账月）")
def close_bill(
    body: CloseRequest, conn: Connection = Depends(get_connection)
) -> dict[str, Any]:
    tenant = conn.execute("SELECT * FROM tenants WHERE code = %s", (body.tenant_code,)).fetchone()
    if tenant is None:
        raise ApiError(404, "tenant_not_found", f"租户不存在: {body.tenant_code}")
    period = parse_period(body.period)
    with tenant_lock(conn, tenant["id"]):
        result = close_month(conn, tenant["id"], period)
        conn.commit()
    month = {
        "period": result.period.strftime("%Y-%m"),
        "status": result.status,
        "closed_at": result.bill["closed_at"],
        "segments": result.segments,
        "anomalies": result.anomalies,
        "total_usage": result.bill["total_usage"],
        "total_amount": result.bill["total_amount"],
    }
    return serializers.month_out(month)


@app.get("/bills", tags=["月账"], summary="租户各月用量/费用分段/调整/异常总览")
def get_bills(
    tenant_code: str = Query(...),
    conn: Connection = Depends(get_connection),
) -> dict[str, Any]:
    tenant = conn.execute("SELECT * FROM tenants WHERE code = %s", (tenant_code,)).fetchone()
    if tenant is None:
        raise ApiError(404, "tenant_not_found", f"租户不存在: {tenant_code}")
    return serializers.report_out(billing.build_report(conn, tenant["id"]))


@app.get("/bills/{period}", tags=["月账"], summary="单个 UTC 月明细，period=YYYY-MM")
def get_bill(
    period: str,
    tenant_code: str = Query(...),
    conn: Connection = Depends(get_connection),
) -> dict[str, Any]:
    tenant = conn.execute("SELECT * FROM tenants WHERE code = %s", (tenant_code,)).fetchone()
    if tenant is None:
        raise ApiError(404, "tenant_not_found", f"租户不存在: {tenant_code}")
    target = parse_period(period)
    month = get_month(conn, tenant["id"], target)
    if month is None:
        raise ApiError(404, "bill_not_found", f"{tenant_code} 在 {period} 没有任何读数或月账")
    return serializers.month_out(month)
