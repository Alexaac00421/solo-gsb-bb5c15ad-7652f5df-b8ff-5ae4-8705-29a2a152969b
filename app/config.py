"""运行期配置，全部来自环境变量，集中定义默认值。"""
from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.app_name = "园区分表用水结算服务"
        # 读取 DATABASE_URL（compose 中注入），或用分项变量拼装
        self.database_url = os.getenv("DATABASE_URL") or (
            "postgresql://"
            f"{os.getenv('POSTGRES_USER', 'water')}:"
            f"{os.getenv('POSTGRES_PASSWORD', 'waterpass')}@"
            f"{os.getenv('POSTGRES_HOST', 'db')}:"
            f"{os.getenv('POSTGRES_PORT', '5432')}/"
            f"{os.getenv('POSTGRES_DB', 'water_billing')}"
        )
        self.http_host = os.getenv("HTTP_HOST", "0.0.0.0")
        self.http_port = int(os.getenv("HTTP_PORT", "8080"))

        # 全局默认费率：服务首次初始化时写入
        self.seed_rate = Decimal(os.getenv("SEED_RATE", "3.50"))
        self.seed_rate_at = os.getenv("SEED_RATE_AT", "2025-01-01T00:00:00Z")
        # 是否顺带写入一个演示租户 + 水表
        self.seed_demo = _as_bool(os.getenv("SEED_DEMO"), True)

        # 账期/时间均按 UTC 切月
        self.timezone = ZoneInfo("UTC")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
