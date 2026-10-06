# API 调用示例（curl）

基址：`http://localhost:8080`。以下示例全部使用 UTC 偏移时间；
`+08:00` 的本地时间会按同一时刻换算为 UTC。

## 1. 健康检查

```bash
curl -s http://localhost:8080/health
# {"status":"ok","service":"water-billing"}
```

## 2. 租户与水表

```bash
# 创建租户
curl -s -X POST http://localhost:8080/tenants \
  -H 'Content-Type: application/json' \
  -d '{"code":"PARK-A","name":"A 栋产业园"}'

# 登记水表并绑定租户
curl -s -X POST http://localhost:8080/meters \
  -H 'Content-Type: application/json' \
  -d '{"code":"WM-001","tenant_code":"PARK-A","name":"A 栋总表"}'

curl -s http://localhost:8080/meters
```

## 3. 费率分段

```bash
# 初始费率 3.50 已由 SEED_RATE 在 2025-01-01 写入；新增一个生效版本
curl -s -X POST http://localhost:8080/rates \
  -H 'Content-Type: application/json' \
  -d '{"effective_at":"2026-05-01T00:00:00Z","price":"4.2000"}'

curl -s http://localhost:8080/rates
```

## 4. 提交读数（可乱序到达）

```bash
# 首读数（锚点，不计费）
curl -s -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-001",
    "read_at":"2026-01-01T00:00:00Z",
    "cumulative":"0"
  }'

# 后一个读数（即使先/后到达都按采集时间归序）
curl -s -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-002",
    "read_at":"2026-01-16T00:00:00Z",
    "cumulative":"20"
  }'

# 跨 UTC 月的读数（2/2），区间 [1/16, 2/2] 会按月界时长比例分摊
curl -s -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-003",
    "read_at":"2026-02-02T00:00:00Z",
    "cumulative":"54"
  }'

# 累计值回退：5 < 锚点 54 → 标记异常、不参与计费，锚点仍为 evt-003
curl -s -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-004",
    "read_at":"2026-02-03T00:00:00Z",
    "cumulative":"5"
  }'

# 后续有效读数仍与锚点 54 衔接：94 - 54 = 40
curl -s -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-005",
    "read_at":"2026-02-05T00:00:00Z",
    "cumulative":"94"
  }'
```

### 4.1 幂等：同事件同内容重复提交

```bash
curl -s -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-002",
    "read_at":"2026-01-16T00:00:00Z",
    "cumulative":"20"
  }'
# {"deduplicated":true,"message":"同事件同内容重复提交，返回原结果", ...}
```

### 4.2 同事件异内容 → 409

```bash
curl -s -i -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-002",
    "read_at":"2026-01-16T00:00:00Z",
    "cumulative":"999"
  }'
# HTTP 409, error.code = event_content_conflict
```

### 4.3 同表同时刻异值：只允许“新事件号引用原事件号”的更正

```bash
# 未引用原事件号 -> 409（相同值重复提交同样 409）
curl -s -i -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-003x",
    "read_at":"2026-02-02T00:00:00Z",
    "cumulative":"55"
  }'
# HTTP 409, error.code = same_timestamp_conflict

# 合法更正：新事件号 evt-003-fix 引用原事件号 evt-003，时刻必须相同
curl -s -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-003-fix",
    "read_at":"2026-02-02T00:00:00Z",
    "cumulative":"44",
    "corrects_event":"evt-003"
  }'

# 再引用已被取代的 evt-003 -> 409 original_already_corrected
```

### 4.4 明确的错误报文

```bash
# 租户与水表不匹配
curl -s -i -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{"tenant_code":"OTHER","meter_code":"WM-001",
       "event_id":"bad-1","read_at":"2026-01-01T00:00:00Z","cumulative":"1"}'
# HTTP 422, error.code = meter_tenant_mismatch

# 时间不带时区
curl -s -i -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{"tenant_code":"PARK-A","meter_code":"WM-001",
       "event_id":"bad-2","read_at":"2026-01-16 00:00:00","cumulative":"21"}'
# HTTP 422，响应体指出 read_at 必须带时区偏移
```

## 5. 查询读数与派生状态

```bash
curl -s "http://localhost:8080/readings?tenant_code=PARK-A&meter_code=WM-001"
# 每条含 state(active/superseded)、derived_status(active/anomaly/superseded)、superseded_by
```

## 6. 关账与查询月账

```bash
# 关 2026-01（UTC 月，格式 YYYY-MM）
curl -s -X POST http://localhost:8080/bills/close \
  -H 'Content-Type: application/json' \
  -d '{"tenant_code":"PARK-A","period":"2026-01"}'

# 重复关账 -> 幂等返回原快照
curl -s -X POST http://localhost:8080/bills/close \
  -H 'Content-Type: application/json' \
  -d '{"tenant_code":"PARK-A","period":"2026-01"}'

# 关 2026-02
curl -s -X POST http://localhost:8080/bills/close \
  -H 'Content-Type: application/json' \
  -d '{"tenant_code":"PARK-A","period":"2026-02"}'

# 各月用量、费用分段、调整、异常明细
curl -s "http://localhost:8080/bills?tenant_code=PARK-A"

# 单月明细
curl -s "http://localhost:8080/bills/2026-02?tenant_code=PARK-A"
```

## 7. 已关账月的迟到/更正读数 → 差额挂下一未关账月

```bash
# 1、2 月均已关账。更正 1 月首读数（0 -> 10），1 月自然用量减少 10 吨
curl -s -X POST http://localhost:8080/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_code":"PARK-A","meter_code":"WM-001",
    "event_id":"evt-001-fix",
    "read_at":"2026-01-01T00:00:00Z",
    "cumulative":"10",
    "corrects_event":"evt-001"
  }'

# 未关账的 3 月实时视图中出现 adjustment 段：origin_period=2026-01
curl -s "http://localhost:8080/bills?tenant_code=PARK-A"

# 关 3 月时差额正式入账；再次/并发关账不会重复
curl -s -X POST http://localhost:8080/bills/close \
  -H 'Content-Type: application/json' \
  -d '{"tenant_code":"PARK-A","period":"2026-03"}'
```
