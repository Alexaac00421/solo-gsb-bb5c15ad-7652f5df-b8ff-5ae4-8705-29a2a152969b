"""PostgreSQL 连接池与启动初始化。

使用 psycopg3 连接池；应用启动时建表、写入初始费率（幂等）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.errors import OperationalError, UniqueViolation
from psycopg.rows import dict_row
from psycopg.pool import ConnectionPool

from .config import get_settings

logger = logging.getLogger("water_billing")

_pool: ConnectionPool | None = None
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _wait_for_database(dsn: str, attempts: int = 60, delay: float = 1.0) -> None:
    """compose 启动时数据库可能尚未就绪，做有限重试。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except OperationalError as exc:  # pragma: no cover - 仅启动路径
            last_error = exc
            logger.info("等待数据库就绪 (%s/%s): %s", attempt, attempts, exc)
            time.sleep(delay)
    raise RuntimeError(f"数据库不可达: {last_error}")


def init_pool() -> ConnectionPool:
    global _pool
    if _pool is not None and not _pool.closed:
        return _pool

    settings = get_settings()
    _wait_for_database(settings.database_url)

    def _configure(conn) -> None:
        conn.row_factory = dict_row
        conn.autocommit = False

    pool = ConnectionPool(
        settings.database_url,
        min_size=2,
        max_size=10,
        configure=_configure,
        open=True,
    )
    _pool = pool
    initialize_schema(pool)
    return pool


def initialize_schema(pool: ConnectionPool) -> None:
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    settings = get_settings()
    with pool.connection() as conn:
        conn.execute(ddl)
        # 初始费率幂等写入：保证最早的采集时刻也能取到费率
        try:
            conn.execute(
                "INSERT INTO rates (effective_at, price) VALUES (%s, %s)",
                (settings.seed_rate_at, settings.seed_rate),
            )
            logger.info("写入初始费率: %s @ %s", settings.seed_rate, settings.seed_rate_at)
        except UniqueViolation:
            conn.rollback()

        if settings.seed_demo:
            conn.execute(
                """
                INSERT INTO tenants (code, name) VALUES ('DEMO', '演示园区租户')
                ON CONFLICT (code) DO NOTHING
                """
            )
            conn.execute(
                """
                INSERT INTO meters (code, name, tenant_id)
                SELECT 'DEMO-M01', '演示总表', t.id FROM tenants t WHERE t.code = 'DEMO'
                ON CONFLICT (code) DO NOTHING
                """
            )
        conn.commit()


def get_connection() -> Iterator[Connection[dict[str, Any]]]:
    """FastAPI 依赖：从池里取连接，请求结束归还。"""
    pool = init_pool()
    with pool.connection() as conn:
        yield conn


@contextmanager
def tenant_lock(conn: Connection[dict[str, Any]], tenant_id: int):
    """以租户维度加事务级咨询锁，串行化该租户的提交/更正/关账。

    重复关账与并发提交因此不会重复入账。
    """
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (9157, tenant_id))
    yield
