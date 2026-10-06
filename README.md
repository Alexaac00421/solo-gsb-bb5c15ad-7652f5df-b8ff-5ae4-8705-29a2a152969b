# 园区分表用水结算服务

基于 **Python (FastAPI) + PostgreSQL** 的园区分租户水表结算服务，提供 HTTP API：

- 水表绑定租户，提交读数校验租户/水表匹配；
- 读数以**事件号（event_id）全局唯一**为幂等键：同事件同内容重复提交返回原结果，异内容报 `409`；
- 同表同采集时刻异值，只能以**新事件号引用原事件号**提交更正，否则报 `409`；
- 读数可乱序到达，服务端按采集时间归序结算；
- 相邻有效读数差额计费；**累计值回退**的读数标记异常、不参与计费，后续有效读数仍与回退前的锚点衔接；
- 费率按生效时间分段；区间用量**跨 UTC 月、跨费率时按时长比例分摊**；
- 月账关账后不可改写；迟到/更正读数改变已关账用量时，差额记入**下一未关账月**，并关联原月账；
- 重复关账、并发提交均不重复入账（租户级事务咨询锁 + 唯一约束 + 增量差额）。

---

## 1. 配置

所有配置通过环境变量注入（见 `.env.example`，复制为 `.env` 后修改）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_USER` | `water` | 数据库用户 |
| `POSTGRES_PASSWORD` | `waterpass` | 数据库密码 |
| `POSTGRES_DB` | `water_billing` | 数据库名 |
| `POSTGRES_PORT_PUBLISH` | `5432` | 映射到本机的数据库端口（仅排查用，可在 compose 中删除） |
| `HTTP_PORT` | `8080` | 映射到本机的 API 端口（容器内固定监听 8080） |
| `SEED_RATE` | `3.50` | 初始费率（元/吨），首次启动幂等写入 |
| `SEED_RATE_AT` | `2025-01-01T00:00:00Z` | 初始费率生效时间（保证最早读数也有费率可用） |
| `SEED_DEMO` | `true` | 是否创建演示租户 `DEMO` 与水表 `DEMO-M01` |

> 所有月账切分、费率边界与时间比较一律使用 **UTC**。提交读数时时间**必须带时区偏移**
> （如 `2026-01-31T08:00:00+08:00` 会换算为 UTC），不带时区返回 `422 invalid ...`。

## 2. 一键启动

前置：已安装 Docker 与 Docker Compose（v2）。

```bash
cp .env.example .env          # 按需修改
docker compose up -d --build # 构建并启动 API + PostgreSQL，自动建表/初始化
```

启动后：

- API 地址：`http://localhost:8080`
- 健康检查：<http://localhost:8080/health>
- 交互式 API 文档（Swagger UI）：<http://localhost:8080/docs>
- OpenAPI Schema：<http://localhost:8080/openapi.json>
- PostgreSQL：`localhost:5432`（可用 `psql` 直连排查）

停止 / 清空：

```bash
docker compose down          # 停止
docker compose down -v       # 停止并删除数据库卷（清空全部数据）
```

本地不使用 Docker 时：

```bash
pip install -r requirements.txt
# 先准备好一个 PostgreSQL，并设置 DATABASE_URL
export DATABASE_URL=postgresql://water:waterpass@localhost:5432/water_billing
uvicorn app.main:app --host 0.0.0.0 --port 8080
# 首次启动会自动执行 app/schema.sql 并写入初始费率
```

## 3. 一键自检 / 调用示例

服务启动后运行端到端示例（仅用 Python 标准库，无需安装依赖）：

```bash
python examples/self_test.py
# 或指定地址
BASE_URL=http://localhost:8080 python examples/self_test.py
```

脚本会创建独立的随机租户/水表，完整演示：乱序读数、回退异常、幂等、冲突、更正、
跨 UTC 月/跨费率分摊、关账、重复/并发关账、已关账差额挂账、错误报文，并有数值断言。
另有 `examples/api_examples.md` 给出等价的 `curl` 调用序列。

## 4. API 一览

基址 `http://localhost:8080`，请求/响应均为 JSON。金额/用量以字符串形式返回（Decimal，避免浮点误差）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/tenants` | 创建租户 `{code, name}` |
| GET | `/tenants` | 租户列表 |
| POST | `/meters` | 登记水表并绑定租户 `{code, tenant_code, name}` |
| GET | `/meters` | 水表列表（含绑定租户） |
| POST | `/rates` | 新增费率版本 `{effective_at, price}`，按生效时间分段 |
| GET | `/rates` | 费率分段列表 |
| POST | `/readings` | 提交读数（幂等 / 冲突 / 更正） |
| GET | `/readings?tenant_code=&meter_code=` | 查询读数及派生状态 |
| POST | `/bills/close` | 月账关账 `{tenant_code, period}`（`YYYY-MM`，UTC 月） |
| GET | `/bills?tenant_code=` | 各月用量、费用分段、调整、异常总览 |
| GET | `/bills/{period}?tenant_code=` | 单个 UTC 月明细 |

### 4.1 提交读数

```json
POST /readings
{
  "tenant_code": "DEMO",
  "meter_code": "DEMO-M01",
  "event_id": "evt-20260131-001",
  "read_at": "2026-01-31T08:00:00+08:00",
  "cumulative": "1234.50",
  "corrects_event": null
}
```

- `event_id`：全局唯一事件号，幂等键。
- `read_at`：**必须带时区**的采集时间。
- `cumulative`：表显累计用量（非本期增量），非负，最多 6 位小数。
- `corrects_event`：可选；更正时填写**原事件号**，且本请求必须使用新的 `event_id`，
  `read_at` 必须与原读数完全相同。

行为：

| 情形 | 结果 |
| --- | --- |
| 同 `event_id` + 完全相同内容 | `200`，响应 `deduplicated: true`，返回原结果 |
| 同 `event_id` + 内容不同 | `409 event_content_conflict` |
| 同表同 `read_at` 已有有效读数且未走更正（含同值重发） | `409 same_timestamp_conflict` |
| 更正但采集时刻与原读数不同 | `422 correction_time_mismatch` |
| 更正值与原值相同 | `422 correction_unchanged` |
| 原事件已被更早的更正取代 | `409 original_already_corrected`（须引用当前有效事件号） |
| 引用不存在的原事件号 / 跨表更正 | `404 original_event_not_found` / `422 correction_meter_mismatch` |
| 租户与水表不匹配 | `422 meter_tenant_mismatch` |
| 时间不带时区 / 非法 | `422`（响应体给出字段原因） |
| 租户或水表不存在 | `404 tenant_not_found` / `404 meter_not_found` |

读数查询中 `state` 为存储态（`active`/`superseded`），`derived_status` 为结算派生态
（`active` / `anomaly` 累计回退 / `superseded` 已被更正），`superseded_by` 指向取代它的事件号。

### 4.2 结算口径

- 仅 `active` 读数参与；按 `read_at` 升序（同时刻按录入顺序）。
- 首读数为初始锚点，不产生用量。
- 相邻有效读数差额 `d = cumulative_now - cumulative_anchor`：
  - `d < 0`：异常（累计回退），不参与计费，锚点不变；
  - `d = 0`：零用量，读数成为新锚点；
  - `d > 0`：区间用量在切点（UTC 月界、费率生效时刻）之间按时长比例分摊，
    费率取**子区间中点时刻**的生效价；同一区间分摊用量做残差修正，保证分段之和恰为差额。
- 用量精度 `1e-6` 吨，金额精度 `1e-4` 元。

### 4.3 关账与差额调整

`POST /bills/close {"tenant_code": "...", "period": "2026-01"}`

- 关账生成该 UTC 月的不可变快照：自然用量/费用分段、调整段、异常明细。
- **重复关账幂等**：返回原快照。
- 已关账月之后到达、且改变该月自然用量的迟到/更正读数：
  - 不改动已关账快照；
  - 在**下一个关账月份**生成 `kind=adjustment` 分段，`origin_period` 指向原月账，
    金额/用量为相对关账时自然值的差额；
  - 每个来源月只入账“尚未入账的增量”，多期滚动、重复关账、并发关账均不重复计算。
- 查询时未关账月实时计算；若已关账月存在尚未落账的差额，会挂在“最新未关账月”展示
  （`adjustment_*` 字段），`segments` 中以 `kind=adjustment` 标识。

月对象字段：

```jsonc
{
  "period": "2026-02",
  "status": "closed",
  "natural_usage": "51.411765",      // 当月读段自然用量
  "natural_amount": "179.9412",
  "adjustment_usage": "0.000000",   // 挂入本月的已关账月差额
  "adjustment_amount": "0.0000",
  "total_usage": "51.411765",
  "total_amount": "179.9412",
  "segments": [
    {
      "kind": "natural",            // natural=自然分段；adjustment=历史差额
      "meter_code": "DEMO-M01",
      "start_at": "2026-02-01T00:00:00+00:00",
      "end_at": "2026-02-02T00:00:00+00:00",
      "rate_effective_at": "2025-01-01T00:00:00+00:00",
      "price": "3.5000",
      "usage": "1.411765",
      "amount": "4.9412",
      "origin_period": null,
      "note": null
    }
  ],
  "anomalies": [
    {
      "reading_event": "evt-...",
      "meter_code": "DEMO-M01",
      "read_at": "2026-02-03T00:00:00+00:00",
      "cumulative": "5.000000",
      "anchor_event": "evt-...",
      "anchor_value": "44.000000",
      "reason": "累计值回退：读数 5 小于锚点 44（事件 ...），该读数标记异常且不参与计费"
    }
  ]
}
```

错误响应统一形如：

```json
{ "error": { "code": "meter_tenant_mismatch", "message": "...", "details": { } } }
```

## 5. 目录结构

```
app/
  main.py        # FastAPI 路由
  models.py      # 请求/响应模型（Pydantic v2，强制时区感知）
  services.py    # 读数提交：幂等、冲突、更正
  settlement.py  # 归序、异常识别、跨月/跨费率按时长分摊
  billing.py     # 关账、增量差额调整、报表
  db.py          # psycopg3 连接池、建表初始化、租户级咨询锁
  schema.sql     # 表结构/唯一约束/索引
  config.py      # 环境变量配置
examples/
  self_test.py       # 端到端自检（标准库）
  api_examples.md    # curl 调用示例
Dockerfile
docker-compose.yml
.env.example
```
