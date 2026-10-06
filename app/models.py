"""Pydantic v2 请求/响应模型。时间字段强制要求带时区。"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _require_aware(dt: datetime, field_name: str) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field_name} 必须带时区偏移，例如 2026-01-31T08:00:00+08:00")
    return dt


# ---------------------------------------------------------------- 租户 / 水表

class TenantCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="租户编码，全局唯一")
    name: str = Field(min_length=1, max_length=128)


class TenantOut(BaseModel):
    id: int
    code: str
    name: str


class MeterCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="水表编码，全局唯一")
    tenant_code: str = Field(description="绑定的租户编码")
    name: str = Field(min_length=1, max_length=128)


class MeterOut(BaseModel):
    id: int
    code: str
    tenant_code: str
    name: str
    active: bool


# ---------------------------------------------------------------- 费率

class RateCreate(BaseModel):
    effective_at: datetime = Field(description="生效时间，必须带时区，按 UTC 比较")
    price: Decimal = Field(ge=0, decimal_places=4)

    @field_validator("effective_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "effective_at")


class RateOut(BaseModel):
    id: int
    effective_at: datetime
    price: Decimal


# ---------------------------------------------------------------- 读数

class ReadingCreate(BaseModel):
    tenant_code: str
    meter_code: str
    event_id: str = Field(min_length=1, max_length=128, description="事件号，全局唯一幂等键")
    read_at: datetime = Field(description="带时区的采集时间")
    cumulative: Decimal = Field(ge=0, decimal_places=6, description="累计用量（表显总量）")
    corrects_event: str | None = Field(
        default=None,
        description="更正时引用的**原事件号**，且必须使用新 event_id；更正值的采集时刻须与原读数相同",
    )

    @field_validator("read_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "read_at")


class ReadingOut(BaseModel):
    event_id: str
    tenant_code: str
    meter_code: str
    read_at: datetime
    cumulative: Decimal
    corrects_event: str | None
    state: str
    derived_status: str = Field(description="active=参与计费，superseded=已被更正，anomaly=累计回退异常")
    superseded_by: str | None = None


# ---------------------------------------------------------------- 关账

class CloseRequest(BaseModel):
    tenant_code: str
    period: str = Field(description="UTC 月份 YYYY-MM")

    @field_validator("period")
    @classmethod
    def _period(cls, v: str) -> str:
        if not PERIOD_RE.match(v):
            raise ValueError("期间格式必须为 YYYY-MM")
        return v


# ---------------------------------------------------------------- 通用

class OkMessage(BaseModel):
    message: str


class HealthOut(BaseModel):
    status: str
    service: str
