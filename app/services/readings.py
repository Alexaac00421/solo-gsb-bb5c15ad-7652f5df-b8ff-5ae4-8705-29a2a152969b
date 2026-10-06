"""读数提交：幂等、冲突检测、更正校验，然后触发重算。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Meter, Reading, Tenant
from ..schemas import ReadingIn
from ..timeutil import as_utc
from .billing import D, lock_meter, recompute_meter
from .errors import BadRequest, Conflict, NotFound


def _same_content(existing: Reading, p: ReadingIn, collected) -> bool:
    return (
        as_utc(existing.collected_at) == collected
        and D(existing.cumulative_value) == p.cumulative_value
        and (existing.ref_event_no or None) == (p.ref_event_no or None)
    )


def _get_by_event(session: Session, meter_id: int, event_no: str) -> Reading | None:
    return session.execute(
        select(Reading).where(Reading.meter_id == meter_id, Reading.event_no == event_no)
    ).scalar_one_or_none()


def submit_reading(session: Session, p: ReadingIn) -> tuple[Reading, bool]:
    """返回 (reading, duplicate)。

    - 同事件同内容 -> 返回原读数（duplicate=True）
    - 同事件异内容 -> 409
    - 同表同时刻异值且未引用原事件号 -> 409
    - 更正（带 ref_event_no）必须与原始读数同采集时刻
    """
    tenant = session.get(Tenant, p.tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {p.tenant_id} not found")
    meter = session.get(Meter, p.meter_id)
    if meter is None:
        raise NotFound(f"meter {p.meter_id} not found")
    if meter.tenant_id != tenant.id:
        raise BadRequest(f"meter {p.meter_id} does not belong to tenant {p.tenant_id}")

    collected = as_utc(p.collected_at)

    # 水表级锁：串行化本水表的提交与重算，保证并发下幂等与不重不漏
    lock_meter(session, meter.id)

    existing = _get_by_event(session, meter.id, p.event_no)
    if existing is not None:
        if _same_content(existing, p, collected):
            return existing, True
        raise Conflict(
            f"event_no '{p.event_no}' already submitted with different content",
            {"existing_event_no": existing.event_no},
        )

    if p.ref_event_no:
        ref = _get_by_event(session, meter.id, p.ref_event_no)
        if ref is None:
            raise BadRequest(
                f"ref_event_no '{p.ref_event_no}' not found for meter {p.meter_id}"
            )
        if as_utc(ref.collected_at) != collected:
            raise BadRequest(
                "correction collected_at must equal the referenced reading's collected_at "
                f"({as_utc(ref.collected_at).isoformat()})"
            )
    else:
        # 同表同时刻已存在原始读数：值不同只能走更正
        same_ts = (
            session.execute(
                select(Reading)
                .where(
                    Reading.meter_id == meter.id,
                    Reading.collected_at == collected,
                    Reading.ref_event_no.is_(None),
                )
                .order_by(Reading.id)
            )
            .scalars()
            .all()
        )
        for r in same_ts:
            if D(r.cumulative_value) != p.cumulative_value:
                raise Conflict(
                    f"a reading already exists at {collected.isoformat()} with value "
                    f"{D(r.cumulative_value)}; to correct it, submit with "
                    f"ref_event_no='{r.event_no}'",
                    {"ref_event_no": r.event_no},
                )

    reading = Reading(
        tenant_id=tenant.id,
        meter_id=meter.id,
        event_no=p.event_no,
        ref_event_no=p.ref_event_no,
        collected_at=collected,
        cumulative_value=p.cumulative_value,
        status="valid",
    )
    session.add(reading)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        # 并发下同 event_no 已被他人插入：按幂等规则处理
        session.expunge(reading)
        existing = _get_by_event(session, meter.id, p.event_no)
        if existing is not None and _same_content(existing, p, collected):
            return existing, True
        raise Conflict(
            f"event_no '{p.event_no}' already submitted with different content",
            {"existing_event_no": p.event_no},
        )

    recompute_meter(session, meter.id)
    session.flush()
    return reading, False
