import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from . import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def wait_for_db() -> None:
    """启动时等待数据库可连接（Compose 下 db 健康检查之外的兜底）。"""
    last_err: Exception | None = None
    for _ in range(config.DB_CONNECT_RETRIES):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(config.DB_CONNECT_INTERVAL)
    raise RuntimeError(f"database not reachable: {last_err}")


def init_db() -> None:
    """建表（幂等），完成数据库初始化。"""
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(engine)
