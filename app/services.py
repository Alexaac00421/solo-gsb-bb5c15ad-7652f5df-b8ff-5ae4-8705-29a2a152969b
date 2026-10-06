"""读数提交：幂等、冲突判定与同表同时刻更正。

冲突矩阵
--------
* 事件号已存在：
    - 内容完全一致（租户/水表/时刻/累计值/更正引用）→ 返回原结果（幂等）；
    - 内容不一致 → 409 event_content_conflict。
* 同表同采集时刻已有 active 读数：
    - 只有“以新事件号引用原事件号、值不同、时刻相同”的更正允许通过；
    - 未引用原事件号（含相同值重复提交）→ 409 same_timestamp_conflict；
    - 更正时刻与原读数不同 → 422 correction_time_mismatch；
    - 更正值与原值相同 → 422 correction_unchanged；
    - 原事件已被更早的更正取代 → 409 original_already_corrected。
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.errors import UniqueViolation

from .errors import ApiError
from .settlement import as_utc, compute_tenant


def _get_tenant(conn: Connection, code: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tenants WHERE code = %s", (code,)).fetchone()
    if row is None:
        raise ApiError(404, "tenant_not_found", f"租户不存在: {code}")
    return dict(row)


def _get_meter(conn: Connection, code: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT m.*, t.code AS tenant_code
        FROM meters m JOIN tenants t ON t.id = m.tenant_id
        WHERE m.code = %s
        """,
        (code,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "meter_not_found", f"水表不存在: {code}")
    return dict(row)


def _same_content(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    return (
        existing["meter_id"] == payload["meter_id"]
        and existing["tenant_id"] == payload["tenant_id"]
        and as_utc(existing["read_at"]) == payload["read_at"]
        and existing["cumulative"] == payload["cumulative"]
        and existing["corrects_event"] == payload["corrects_event"]
    )


def _anomaly_event_set(conn: Connection, tenant_id: int) -> set[str]:
    """依据当前全量数据计算异常读数事件集合（与结算引擎同样的锚点规则）。"""
    computed = compute_tenant(conn, tenant_id)
    return {
        a["reading_event"]
        for rows in computed["anomalies"].values()
        for a in rows
    }


def list_readings(
    conn: Connection, tenant_code: str | None = None, meter_code: str | None = None
) -> list[dict[str, Any]]:
    sql = """
        SELECT r.event_id, r.meter_id, r.tenant_id, r.read_at, r.cumulative,
               r.corrects_event, r.state, t.code AS tenant_code, m.code AS meter_code
        FROM readings r
        JOIN tenants t ON t.id = r.tenant_id
        JOIN meters m ON m.id = r.meter_id
    """
    where, params = [], []
    if tenant_code:
        where.append("t.code = %s")
        params.append(tenant_code)
    if meter_code:
        where.append("m.code = %s")
        params.append(meter_code)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.read_at, r.event_id"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # 派生状态：哪些事件当前属于累计回退异常
    anomaly_events: set[str] = set()
    tenant_ids = {r["tenant_id"] for r in rows}
    for tenant_id in tenant_ids:
        anomaly_events |= _anomaly_event_set(conn, tenant_id)
    supersede = {
        r["corrects_event"]: r["event_id"]
        for r in rows
        if r["corrects_event"]
    }

    result = []
    for r in rows:
        if r["state"] == "superseded":
            derived = "superseded"
        elif r["event_id"] in anomaly_events:
            derived = "anomaly"
        else:
            derived = "active"
        result.append(
            {
                "event_id": r["event_id"],
                "tenant_code": r["tenant_code"],
                "meter_code": r["meter_code"],
                "read_at": r["read_at"],
                "cumulative": r["cumulative"],
                "corrects_event": r["corrects_event"],
                "state": r["state"],
                "derived_status": derived,
                "superseded_by": supersede.get(r["event_id"]),
            }
        )
    return result


def submit_reading(conn: Connection, payload_in: Any) -> dict[str, Any]:
    tenant = _get_tenant(conn, payload_in.tenant_code)
    meter = _get_meter(conn, payload_in.meter_code)

    # 租户与水表不匹配必须明确报错
    if meter["tenant_id"] != tenant["id"]:
        raise ApiError(
            422,
            "meter_tenant_mismatch",
            f"水表 {meter['code']} 绑定的是租户 {meter['tenant_code']}，"
            f"与提交租户 {tenant['code']} 不匹配",
            {
                "meter_code": meter["code"],
                "bound_tenant_code": meter["tenant_code"],
                "submitted_tenant_code": tenant["code"],
            },
        )

    read_at = as_utc(payload_in.read_at)  # 无效/无时区时间在模型层/此处拦截
    payload = {
        "meter_id": meter["id"],
        "tenant_id": tenant["id"],
        "read_at": read_at,
        "cumulative": payload_in.cumulative,
        "corrects_event": payload_in.corrects_event,
    }

    # 1) 事件号幂等 / 冲突
    existing = conn.execute(
        "SELECT * FROM readings WHERE event_id = %s",
        (payload_in.event_id,),
    ).fetchone()
    if existing:
        existing = dict(existing)
        if _same_content(existing, payload):
            return {
                "deduplicated": True,
                "reading": _fetch_one(conn, existing["event_id"]),
                "message": "同事件同内容重复提交，返回原结果",
            }
        raise ApiError(
            409,
            "event_content_conflict",
            f"事件号 {payload_in.event_id} 已存在但提交内容不同",
            {
                "event_id": payload_in.event_id,
                "existing": {
                    "meter_code": _meter_code(conn, existing["meter_id"]),
                    "read_at": existing["read_at"].isoformat(),
                    "cumulative": str(existing["cumulative"]),
                    "corrects_event": existing["corrects_event"],
                },
            },
        )

    original: dict[str, Any] | None = None
    if payload_in.corrects_event:
        original = conn.execute(
            "SELECT * FROM readings WHERE event_id = %s",
            (payload_in.corrects_event,),
        ).fetchone()
        if original is None:
            raise ApiError(
                404,
                "original_event_not_found",
                f"被更正的原事件号不存在: {payload_in.corrects_event}",
            )
        original = dict(original)
        if original["meter_id"] != meter["id"]:
            raise ApiError(
                422,
                "correction_meter_mismatch",
                "更正读数必须与原读数属于同一水表",
            )
        if original["state"] == "superseded":
            superseder = conn.execute(
                "SELECT event_id FROM readings WHERE corrects_event = %s",
                (original["event_id"],),
            ).fetchone()
            raise ApiError(
                409,
                "original_already_corrected",
                f"原事件 {original['event_id']} 已被事件 "
                f"{superseder['event_id'] if superseder else '?'} 更正，"
                "请引用当前有效事件号",
                {"superseded_by": superseder["event_id"] if superseder else None},
            )
        if as_utc(original["read_at"]) != read_at:
            raise ApiError(
                422,
                "correction_time_mismatch",
                "更正读数的采集时间必须与原读数完全相同",
                {
                    "original_read_at": as_utc(original["read_at"]).isoformat(),
                    "submitted_read_at": read_at.isoformat(),
                },
            )
        if original["cumulative"] == payload["cumulative"]:
            raise ApiError(
                422,
                "correction_unchanged",
                "更正读数与原读数累计值相同，无需更正",
            )

    # 2) 同表同采集时刻冲突（更正除外）
    same_time = conn.execute(
        """
        SELECT r.*, c.event_id AS superseder
        FROM readings r
        LEFT JOIN readings c ON c.corrects_event = r.event_id
        WHERE r.meter_id = %s AND r.read_at = %s AND r.state = 'active'
        """,
        (meter["id"], read_at),
    ).fetchone()
    if same_time is not None:
        same_time = dict(same_time)
        if original is None or original["event_id"] != same_time["event_id"]:
            raise ApiError(
                409,
                "same_timestamp_conflict",
                f"水表 {meter['code']} 在 {read_at.isoformat()} 已有读数事件 "
                f"{same_time['event_id']}；同时刻异值只能以新事件号引用原事件号提交更正",
                {"existing_event_id": same_time["event_id"]},
            )

    # 3) 落库 + 原读数置 superseded。
    # 部分唯一索引要求同表同时刻仅一条 active，因此必须先把原读数置为
    # superseded，再插入新读数；任一步失败随事务整体回滚。
    try:
        if payload_in.corrects_event:
            updated = conn.execute(
                "UPDATE readings SET state = 'superseded' "
                "WHERE event_id = %s AND state = 'active'",
                (payload_in.corrects_event,),
            )
            if updated.rowcount != 1:
                # 并发窗口中已被其它更正抢先
                raise ApiError(
                    409,
                    "original_already_corrected",
                    f"原事件 {payload_in.corrects_event} 已被并发更正，请重试",
                )
        conn.execute(
            """
            INSERT INTO readings
              (event_id, meter_id, tenant_id, read_at, cumulative, corrects_event)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                payload_in.event_id,
                meter["id"],
                tenant["id"],
                read_at,
                payload["cumulative"],
                payload_in.corrects_event,
            ),
        )
    except ApiError:
        raise
    except UniqueViolation as exc:
        # 并发窗口下兜底：按约束名区分唯一冲突类型
        conn.rollback()
        constraint = (exc.diag.constraint_name or "") if exc.diag else ""
        if constraint == "readings_event_id_key":
            raise ApiError(
                409, "event_content_conflict",
                "事件号已存在且内容不一致（并发提交）",
            )
        raise ApiError(
            409,
            "same_timestamp_conflict",
            "同表同采集时刻已存在有效读数（并发提交）；异值须走更正流程",
        )

    return {"deduplicated": False, "reading": _fetch_one(conn, payload_in.event_id)}


def _meter_code(conn: Connection, meter_id: int) -> str:
    row = conn.execute("SELECT code FROM meters WHERE id = %s", (meter_id,)).fetchone()
    return row["code"] if row else str(meter_id)


def _fetch_one(conn: Connection, event_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT r.event_id, r.meter_id, r.tenant_id, r.read_at, r.cumulative,
               r.corrects_event, r.state, t.code AS tenant_code, m.code AS meter_code
        FROM readings r
        JOIN tenants t ON t.id = r.tenant_id
        JOIN meters m ON m.id = r.meter_id
        WHERE r.event_id = %s
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "reading_not_found", f"读数不存在: {event_id}")
    row = dict(row)
    anomaly_events = _anomaly_event_set(conn, row["tenant_id"])
    superseder = conn.execute(
        "SELECT event_id FROM readings WHERE corrects_event = %s",
        (event_id,),
    ).fetchone()
    if row["state"] == "superseded":
        derived = "superseded"
    elif row["event_id"] in anomaly_events:
        derived = "anomaly"
    else:
        derived = "active"
    return {
        "event_id": row["event_id"],
        "tenant_code": row["tenant_code"],
        "meter_code": row["meter_code"],
        "read_at": row["read_at"],
        "cumulative": row["cumulative"],
        "corrects_event": row["corrects_event"],
        "state": row["state"],
        "derived_status": derived,
        "superseded_by": superseder["event_id"] if superseder else None,
    }
