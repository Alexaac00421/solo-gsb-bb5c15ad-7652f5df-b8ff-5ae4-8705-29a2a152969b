from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal, engine, init_db, wait_for_db
from .models import Adjustment, Bill, BillSegment, Meter, Rate, Reading, Tenant
from .schemas import CloseIn, MeterIn, RateIn, ReadingIn, TenantIn
from .services import readings as readings_svc
from .services.billing import D, NoRateError, close_month
from .services.errors import ApiError, BadRequest, Conflict, NotFound
from .timeutil import add_months, as_utc, parse_month


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时等待数据库就绪并完成建表初始化
    wait_for_db()
    init_db()
    yield


app = FastAPI(
    title="Park Water Billing Service",
    version="1.0.0",
    description="园区分表用水结算服务：读数幂等提交、乱序/更正、跨月跨费率分摊、月账关账与调整。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 异常处理


@app.exception_handler(ApiError)
async def api_error_handler(_request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content={"error": exc.message, **exc.extra})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request, exc: RequestValidationError):
    details = [
        {"loc": [str(x) for x in e.get("loc", [])], "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=400, content={"error": "invalid request", "details": details})


# ---------------------------------------------------------------- 工具


@contextmanager
def tx():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def num(x) -> Optional[float]:
    if x is None:
        return None
    return float(D(x))


def iso(dt) -> Optional[str]:
    if dt is None:
        return None
    return as_utc(dt).isoformat()


def month_str(d: date) -> str:
    return d.isoformat()[:7]


def parse_month_or_400(s: str) -> date:
    try:
        return parse_month(s)
    except ValueError as e:
        raise BadRequest(str(e)) from e


# ---------------------------------------------------------------- 响应组装


def reading_out(r: Reading, duplicate: bool = False) -> dict:
    return {
        "id": r.id,
        "tenant_id": r.tenant_id,
        "meter_id": r.meter_id,
        "event_no": r.event_no,
        "ref_event_no": r.ref_event_no,
        "collected_at": iso(r.collected_at),
        "cumulative_value": num(r.cumulative_value),
        "status": r.status,
        "usage_from_prev": num(r.usage_from_prev),
        "note": r.note,
        "duplicate": duplicate,
    }


def rate_out(r: Rate) -> dict:
    return {
        "id": r.id,
        "effective_from": iso(r.effective_from),
        "price_per_unit": num(r.price_per_unit),
    }


def bill_out(session, bill: Bill, meter_code: str) -> dict:
    segments = (
        session.execute(
            select(BillSegment).where(BillSegment.bill_id == bill.id).order_by(BillSegment.period_start)
        )
        .scalars()
        .all()
    )
    adjustments = (
        session.execute(
            select(Adjustment).where(Adjustment.bill_id == bill.id).order_by(Adjustment.id)
        )
        .scalars()
        .all()
    )
    next_m = add_months(bill.month)
    anomalies = (
        session.execute(
            select(Reading)
            .where(
                Reading.meter_id == bill.meter_id,
                Reading.status == "anomaly",
                Reading.collected_at >= bill.month,
                Reading.collected_at < next_m,
            )
            .order_by(Reading.collected_at)
        )
        .scalars()
        .all()
    )
    adj_total = sum((D(a.amount_delta) for a in adjustments), Decimal("0"))
    return {
        "bill_id": bill.id,
        "meter_id": bill.meter_id,
        "meter_code": meter_code,
        "month": month_str(bill.month),
        "status": bill.status,
        "usage": num(bill.usage),
        "amount": num(bill.amount),
        "billed_usage": num(bill.billed_usage),
        "billed_amount": num(bill.billed_amount),
        "closed_at": iso(bill.closed_at),
        "segments": [
            {
                "period_start": iso(s.period_start),
                "period_end": iso(s.period_end),
                "usage": num(s.usage),
                "price_per_unit": num(s.price_per_unit),
                "amount": num(s.amount),
            }
            for s in segments
        ],
        "adjustments": [
            {
                "source_month": month_str(a.source_month),
                "source_bill_id": a.source_bill_id,
                "usage_delta": num(a.usage_delta),
                "amount_delta": num(a.amount_delta),
                "reason": a.reason,
                "created_at": iso(a.created_at),
            }
            for a in adjustments
        ],
        "adjustments_total": num(adj_total),
        "total_amount": num(D(bill.amount) + adj_total),
        "anomalies": [
            {
                "event_no": r.event_no,
                "collected_at": iso(r.collected_at),
                "cumulative_value": num(r.cumulative_value),
                "note": r.note,
            }
            for r in anomalies
        ],
    }


# ---------------------------------------------------------------- 基础接口


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/tenants", status_code=201)
def create_tenant(p: TenantIn):
    with tx() as s:
        t = Tenant(name=p.name)
        s.add(t)
        try:
            with s.begin_nested():
                s.flush()
        except IntegrityError:
            raise Conflict(f"tenant name '{p.name}' already exists")
        return {"id": t.id, "name": t.name}


@app.get("/tenants")
def list_tenants():
    with tx() as s:
        rows = s.execute(select(Tenant).order_by(Tenant.id)).scalars().all()
        return {"tenants": [{"id": t.id, "name": t.name} for t in rows]}


@app.post("/meters", status_code=201)
def create_meter(p: MeterIn):
    with tx() as s:
        if s.get(Tenant, p.tenant_id) is None:
            raise NotFound(f"tenant {p.tenant_id} not found")
        m = Meter(tenant_id=p.tenant_id, code=p.code)
        s.add(m)
        try:
            with s.begin_nested():
                s.flush()
        except IntegrityError:
            raise Conflict(f"meter code '{p.code}' already exists for tenant {p.tenant_id}")
        return {"id": m.id, "tenant_id": m.tenant_id, "code": m.code}


@app.get("/tenants/{tenant_id}/meters")
def list_meters(tenant_id: int):
    with tx() as s:
        if s.get(Tenant, tenant_id) is None:
            raise NotFound(f"tenant {tenant_id} not found")
        rows = (
            s.execute(select(Meter).where(Meter.tenant_id == tenant_id).order_by(Meter.id))
            .scalars()
            .all()
        )
        return {"meters": [{"id": m.id, "tenant_id": m.tenant_id, "code": m.code} for m in rows]}


@app.post("/rates", status_code=201)
def create_rate(p: RateIn, response: Response):
    with tx() as s:
        eff = as_utc(p.effective_from)
        existing = s.execute(select(Rate).where(Rate.effective_from == eff)).scalar_one_or_none()
        if existing is not None:
            if D(existing.price_per_unit) == p.price_per_unit:
                response.status_code = 200
                return rate_out(existing)
            raise Conflict("a different rate already exists at this effective_from")
        r = Rate(effective_from=eff, price_per_unit=p.price_per_unit)
        s.add(r)
        s.flush()
        return rate_out(r)


@app.get("/rates")
def list_rates():
    with tx() as s:
        rows = s.execute(select(Rate).order_by(Rate.effective_from)).scalars().all()
        return {"rates": [rate_out(r) for r in rows]}


# ---------------------------------------------------------------- 读数


@app.post("/readings", status_code=201)
def submit_reading(p: ReadingIn, response: Response):
    with tx() as s:
        try:
            reading, duplicate = readings_svc.submit_reading(s, p)
        except NoRateError as e:
            raise BadRequest(
                f"no rate effective at {e.at.isoformat()}; "
                "please add a rate covering this period first"
            ) from e
        if duplicate:
            response.status_code = 200
        return reading_out(reading, duplicate)


@app.get("/tenants/{tenant_id}/meters/{meter_id}/readings")
def list_readings(tenant_id: int, meter_id: int):
    with tx() as s:
        meter = s.get(Meter, meter_id)
        if meter is None:
            raise NotFound(f"meter {meter_id} not found")
        if meter.tenant_id != tenant_id:
            raise BadRequest(f"meter {meter_id} does not belong to tenant {tenant_id}")
        rows = (
            s.execute(
                select(Reading).where(Reading.meter_id == meter_id).order_by(Reading.collected_at, Reading.id)
            )
            .scalars()
            .all()
        )
        return {"readings": [reading_out(r) for r in rows]}


# ---------------------------------------------------------------- 月账


@app.post("/tenants/{tenant_id}/bills/close")
def close_bills(tenant_id: int, p: CloseIn):
    month = parse_month_or_400(p.month)
    with tx() as s:
        if s.get(Tenant, tenant_id) is None:
            raise NotFound(f"tenant {tenant_id} not found")
        bills, already = close_month(s, tenant_id, month)
        codes = {m.id: m.code for m in s.execute(select(Meter)).scalars().all()}
        return {
            "tenant_id": tenant_id,
            "month": p.month,
            "already_closed": already,
            "bills": [bill_out(s, b, codes.get(b.meter_id, "")) for b in bills],
        }


@app.get("/tenants/{tenant_id}/bills")
def get_bills(tenant_id: int, month: Optional[str] = Query(default=None)):
    with tx() as s:
        if s.get(Tenant, tenant_id) is None:
            raise NotFound(f"tenant {tenant_id} not found")
        stmt = select(Bill).where(Bill.tenant_id == tenant_id)
        if month is not None:
            stmt = stmt.where(Bill.month == parse_month_or_400(month))
        bills = s.execute(stmt.order_by(Bill.month, Bill.meter_id)).scalars().all()
        codes = {m.id: m.code for m in s.execute(select(Meter)).scalars().all()}
        return {
            "tenant_id": tenant_id,
            "bills": [bill_out(s, b, codes.get(b.meter_id, "")) for b in bills],
        }
