from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from .db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Meter(Base):
    __tablename__ = "meters"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    code = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_meter_tenant_code"),)


class Rate(Base):
    """费率，按 effective_from 生效，直到下一条费率生效为止。"""

    __tablename__ = "rates"

    id = Column(Integer, primary_key=True)
    effective_from = Column(DateTime(timezone=True), nullable=False, unique=True)
    price_per_unit = Column(Numeric(20, 6), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Reading(Base):
    """水表读数事件。event_no 在同一水表内唯一（幂等键）。

    status: valid / anomaly / superseded / duplicate
    """

    __tablename__ = "readings"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    meter_id = Column(Integer, ForeignKey("meters.id"), nullable=False, index=True)
    event_no = Column(String(128), nullable=False)
    ref_event_no = Column(String(128), nullable=True)  # 更正时引用原事件号
    collected_at = Column(DateTime(timezone=True), nullable=False)
    cumulative_value = Column(Numeric(20, 6), nullable=False)
    status = Column(String(20), nullable=False, default="valid")
    usage_from_prev = Column(Numeric(20, 6), nullable=True)  # 与上一有效读数的差额
    note = Column(Text, nullable=True)  # 异常/被替代等原因说明
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("meter_id", "event_no", name="uq_reading_meter_event"),
        Index("ix_reading_meter_collected", "meter_id", "collected_at"),
    )


class Bill(Base):
    """月账：每水表每 UTC 月一条。关账后 billed_* 快照不可改写。"""

    __tablename__ = "bills"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    meter_id = Column(Integer, ForeignKey("meters.id"), nullable=False)
    month = Column(Date, nullable=False)  # UTC 月，存每月 1 日
    status = Column(String(10), nullable=False, default="open")  # open / closed
    usage = Column(Numeric(20, 6), nullable=False, default=0)
    amount = Column(Numeric(20, 6), nullable=False, default=0)
    billed_usage = Column(Numeric(20, 6), nullable=True)  # 关账时快照
    billed_amount = Column(Numeric(20, 6), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("meter_id", "month", name="uq_bill_meter_month"),)


class BillSegment(Base):
    """费用分段：跨月/跨费率区间按时长比例拆分后的明细。"""

    __tablename__ = "bill_segments"

    id = Column(Integer, primary_key=True)
    bill_id = Column(Integer, ForeignKey("bills.id"), nullable=False, index=True)
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    usage = Column(Numeric(20, 6), nullable=False)
    price_per_unit = Column(Numeric(20, 6), nullable=False)
    amount = Column(Numeric(20, 6), nullable=False)


class Adjustment(Base):
    """调整：迟到/更正读数改变已关账月用量时，记入下一未关账月份的差额。"""

    __tablename__ = "adjustments"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    meter_id = Column(Integer, ForeignKey("meters.id"), nullable=False)
    bill_id = Column(Integer, ForeignKey("bills.id"), nullable=False, index=True)  # 入账目标月账
    source_bill_id = Column(Integer, ForeignKey("bills.id"), nullable=False)  # 被调整的已关账月账
    source_month = Column(Date, nullable=False)
    usage_delta = Column(Numeric(20, 6), nullable=False)
    amount_delta = Column(Numeric(20, 6), nullable=False)
    reason = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
