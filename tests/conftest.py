import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://water:water@localhost:5432/waterdb"
)

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(scope="session")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def rates(client):
    """全局费率：2026-01-01 起 2.0，2026-03-01 起 3.0。"""
    client.post("/rates", json={"effective_from": "2026-01-01T00:00:00Z", "price_per_unit": 2.0})
    client.post("/rates", json={"effective_from": "2026-03-01T00:00:00Z", "price_per_unit": 3.0})


@pytest.fixture()
def wm(client, rates):
    """每个用例独立的租户 + 水表（累计值从 0 起，互不影响）。"""
    t = client.post("/tenants", json={"name": f"t-{uuid.uuid4().hex[:10]}"}).json()
    m = client.post("/meters", json={"tenant_id": t["id"], "code": "WM"}).json()
    return t["id"], m["id"]
