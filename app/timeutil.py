import re
from datetime import date, datetime, timezone

UTC = timezone.utc

_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def as_utc(dt: datetime) -> datetime:
    """把时间统一成带时区的 UTC；对数据库读出的 naive 时间按 UTC 处理。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def month_of(dt: datetime) -> date:
    """UTC 账期月份（每月 1 日）。"""
    dt = as_utc(dt)
    return date(dt.year, dt.month, 1)


def add_months(d: date, n: int = 1) -> date:
    i = d.year * 12 + (d.month - 1) + n
    return date(i // 12, i % 12 + 1, 1)


def next_month_dt(dt: datetime) -> datetime:
    i = dt.year * 12 + (dt.month - 1) + 1
    return datetime(i // 12, i % 12 + 1, 1, tzinfo=UTC)


def parse_month(s: str) -> date:
    """解析 'YYYY-MM'，非法格式抛 ValueError。"""
    m = _MONTH_RE.match(s or "")
    if not m:
        raise ValueError(f"invalid month {s!r}, expected format YYYY-MM")
    return date(int(m.group(1)), int(m.group(2)), 1)
