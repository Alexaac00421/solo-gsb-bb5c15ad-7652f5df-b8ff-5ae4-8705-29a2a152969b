from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _require_tz(v: datetime, field: str) -> datetime:
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        raise ValueError(f"{field} must include a timezone offset (e.g. 2026-03-01T00:00:00+08:00)")
    return v


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class MeterIn(BaseModel):
    tenant_id: int
    code: str = Field(min_length=1, max_length=100)


class RateIn(BaseModel):
    effective_from: datetime
    price_per_unit: Decimal = Field(ge=0)

    @field_validator("effective_from")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _require_tz(v, "effective_from")


class ReadingIn(BaseModel):
    tenant_id: int
    meter_id: int
    event_no: str = Field(min_length=1, max_length=128)
    collected_at: datetime
    cumulative_value: Decimal = Field(ge=0)
    ref_event_no: Optional[str] = Field(default=None, max_length=128)

    @field_validator("collected_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _require_tz(v, "collected_at")


class CloseIn(BaseModel):
    month: str  # YYYY-MM，在路由里严格校验
