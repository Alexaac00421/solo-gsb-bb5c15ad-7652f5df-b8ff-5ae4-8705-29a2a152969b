-- 园区分表用水结算服务 · 数据库结构
-- 所有时间均以 timestamptz 存储；月账/时长切分统一按 UTC。

CREATE TABLE IF NOT EXISTS tenants (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meters (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id),
    name        TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 费率全局生效（园区统一水价），按 effective_at 分段
CREATE TABLE IF NOT EXISTS rates (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    effective_at TIMESTAMPTZ NOT NULL UNIQUE,
    price        NUMERIC(12, 4) NOT NULL CHECK (price >= 0)
);

-- 读数：event_id 全局唯一，是幂等键
-- state: active 参与结算 / superseded 被同表同时刻的更正取代
CREATE TABLE IF NOT EXISTS readings (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id        TEXT NOT NULL UNIQUE,
    meter_id        BIGINT NOT NULL REFERENCES meters(id),
    tenant_id       BIGINT NOT NULL REFERENCES tenants(id),
    read_at         TIMESTAMPTZ NOT NULL,
    cumulative      NUMERIC(16, 6) NOT NULL CHECK (cumulative >= 0),
    corrects_event  TEXT REFERENCES readings(event_id),
    state           TEXT NOT NULL DEFAULT 'active'
                        CHECK (state IN ('active', 'superseded')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 同一水表同一采集时刻只允许存在一条 active 读数
CREATE UNIQUE INDEX IF NOT EXISTS uq_readings_meter_time_active
    ON readings (meter_id, read_at)
    WHERE state = 'active';
-- 一条读数最多被一条更正引用（链式更正由 service 层在引用活事件时拦截）
CREATE UNIQUE INDEX IF NOT EXISTS uq_readings_corrects
    ON readings (corrects_event)
    WHERE corrects_event IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_readings_meter_time
    ON readings (meter_id, read_at);

-- 租户月账头表
-- natural_*: 关账时按当月读数重算的自然用量/费用（差额比较的基准）
-- total_*  : natural + 历次挂入的已关账月差额
CREATE TABLE IF NOT EXISTS bills (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id      BIGINT NOT NULL REFERENCES tenants(id),
    period         DATE NOT NULL,           -- 该 UTC 月的 1 号
    status         TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'closed')),
    natural_usage  NUMERIC(18, 6) NOT NULL DEFAULT 0,
    natural_amount NUMERIC(16, 4) NOT NULL DEFAULT 0,
    total_usage    NUMERIC(18, 6) NOT NULL DEFAULT 0,
    total_amount   NUMERIC(16, 4) NOT NULL DEFAULT 0,
    closed_at      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, period)
);

-- 费用分段：natural = 当月读段按费率/跨月时长分摊；adjustment = 已关账期差额
CREATE TABLE IF NOT EXISTS bill_segments (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bill_id         BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('natural', 'adjustment')),
    start_at        TIMESTAMPTZ,
    end_at          TIMESTAMPTZ,
    rate_effective_at TIMESTAMPTZ,
    price           NUMERIC(12, 4) NOT NULL,
    usage           NUMERIC(18, 6) NOT NULL,
    amount          NUMERIC(16, 4) NOT NULL,
    origin_period   DATE,                  -- 调整段：差额来源的已关账月
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_segments_bill ON bill_segments (bill_id);

-- 异常读数快照（回退读数不参与计费；关账时固化）
CREATE TABLE IF NOT EXISTS bill_anomalies (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bill_id         BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    reading_event   TEXT NOT NULL,
    meter_code      TEXT NOT NULL,
    read_at         TIMESTAMPTZ NOT NULL,
    cumulative      NUMERIC(16, 6) NOT NULL,
    anchor_event    TEXT,
    anchor_value    NUMERIC(16, 6),
    reason          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomalies_bill ON bill_anomalies (bill_id);
