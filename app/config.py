import os

# SQLAlchemy 连接串，Compose 中通过环境变量注入
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://water:water@localhost:5432/waterdb",
)
# 启动时等待数据库就绪的重试参数
DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "30"))
DB_CONNECT_INTERVAL = float(os.getenv("DB_CONNECT_INTERVAL", "1.0"))
