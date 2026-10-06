# 园区分表用水结算服务（Park Water Billing Service）

基于 **Python (FastAPI) + PostgreSQL** 的园区分表用水结算服务。水表绑定租户，支持读数幂等提交、乱序到达、更正、累计值回退异常标记、跨 UTC 月 / 跨费率按时长比例分摊、月账关账与关账后差额调整入账。

## 快速开始

```bash
docker compose up --build
```

一条命令即可启动服务、PostgreSQL 数据库并完成初始化（建表由应用启动时自动完成）。

| 组件 | 地址 |
| --- | --- |
| HTTP API | http://localhost:8000 |
| 交互式 API 文档 (Swagger UI) | http://localhost:8000/docs |
| OpenAPI JSON | http://localhost:8000/openapi.json |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432（用户/密码/库：`water`/`water`/`waterdb`） |

停止并清空数据：`docker compose down -v`

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://water:water@localhost:5432/waterdb` | SQLAlchemy 连接串，Compose 中已注入指向 `db` 容器 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库就绪的重试次数 |
| `DB_CONNECT_INTERVAL` | `1.0` | 重试间隔（秒） |

## 业务规则

- **水表绑定租户**：提交读数时校验租户与水表归属，不匹配返回 400，不存在返回 404。
- **幂等提交**：`event_no` 在同一水表内唯一。同事件同内容重复提交返回原结果（HTTP 200，`duplicate: true`）；同事件异内容返回 **409 冲突**。
- **同时刻异值**：同表同采集时刻已存在不同累计值时，仅接受以新事件号 + `ref_event_no` 引用原事件号的**更正**，否则 409。更正与原读数必须同采集时刻；同一时刻可多次更正，最新更正生效，被替代的读数标记 `superseded`。
- **乱序到达**：按采集时间（统一换算 UTC）排序计算相邻**有效**读数差额，与提交顺序无关。
- **累计值回退**：回退读数标记 `anomaly`，不参与计费；后续有效读数跳过异常读数，与上一有效读数衔接。
- **费率分段**：费率按 `effective_from` 生效至下一条费率。相邻有效读数区间的用量，按**时长比例**分摊到 UTC 自然月与费率分段，产生费用分段明细。区间起点无已生效费率时提交失败并明确报错（400）。
- **关账**：`POST /tenants/{id}/bills/close` 将租户该月全部水表月账固化为快照（`billed_*`），之后不可改写。重复关账幂等（返回 `already_closed: true`），并发关账不会重复入账。
- **迟到/更正调整**：迟到或更正读数改变已关账月用量时，差额（扣除已入账部分）记入**下一未关账月份**的月账，生成关联原月账的调整记录（`adjustments`，含 `source_month` / `source_bill_id`）。
- **并发安全**：同一水表的提交/重算、同一租户的关账通过 PostgreSQL 事务级咨询锁串行化；事件号唯一约束兜底，保证并发下幂等且不重复入账。
- **时间校验**：`collected_at`、`effective_from` 必须带时区偏移（如 `2026-03-01T00:00:00+08:00`），否则 400；月份参数必须是 `YYYY-MM`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/tenants` | 创建租户 `{name}` |
| GET | `/tenants` | 租户列表 |
| POST | `/meters` | 创建水表 `{tenant_id, code}` |
| GET | `/tenants/{tenant_id}/meters` | 租户水表列表 |
| POST | `/rates` | 新增费率 `{effective_from, price_per_unit}`（同时刻同价幂等，异价 409） |
| GET | `/rates` | 费率列表 |
| POST | `/readings` | 提交读数（见下） |
| GET | `/tenants/{tenant_id}/meters/{meter_id}/readings` | 水表读数列表（含状态与异常原因） |
| POST | `/tenants/{tenant_id}/bills/close` | 关账 `{month: "YYYY-MM"}` |
| GET | `/tenants/{tenant_id}/bills?month=YYYY-MM` | 查询月账（用量、费用分段、调整、异常明细；`month` 可省略查全部） |

### 提交读数 `POST /readings`

```json
{
  "tenant_id": 1,
  "meter_id": 1,
  "event_no": "e2",
  "collected_at": "2026-03-02T12:00:00+08:00",
  "cumulative_value": 160,
  "ref_event_no": null
}
```

- 新事件：HTTP 201，返回读数及其 `status`（`valid` / `anomaly` / `superseded` / `duplicate`）与 `usage_from_prev`。
- 重复提交（同事件同内容）：HTTP 200 且 `duplicate: true`。
- 更正：携带 `ref_event_no`（原事件号），`collected_at` 须与原读数一致。

### 月账查询响应（示例字段）

```json
{
  "tenant_id": 1,
  "bills": [
    {
      "meter_id": 1, "meter_code": "WM-001", "month": "2026-03", "status": "open",
      "usage": 87.5, "amount": 262.5,
      "segments": [{"period_start": "...", "period_end": "...", "usage": 37.5, "price_per_unit": 3.0, "amount": 112.5}],
      "adjustments": [{"source_month": "2026-02", "usage_delta": 5.0, "amount_delta": 10.0, "reason": "..."}],
      "adjustments_total": 10.0, "total_amount": 272.5,
      "anomalies": [{"event_no": "e6", "collected_at": "...", "cumulative_value": 80, "note": "cumulative value regressed..."}]
    }
  ]
}
```

## 调用示例

```bash
# curl 全流程示例（建租户/水表/费率 -> 读数 -> 冲突/更正/异常 -> 关账 -> 调整）
BASE=http://localhost:8000 bash examples/demo.sh

# Python 全流程示例（含断言与并发校验，需 pip install requests）
BASE=http://localhost:8000 python3 examples/demo.py
```

最小调用：

```bash
curl -X POST http://localhost:8000/tenants -H 'Content-Type: application/json' -d '{"name":"park-a"}'
curl -X POST http://localhost:8000/meters  -H 'Content-Type: application/json' -d '{"tenant_id":1,"code":"WM-001"}'
curl -X POST http://localhost:8000/rates   -H 'Content-Type: application/json' -d '{"effective_from":"2026-01-01T00:00:00Z","price_per_unit":2.0}'
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' -d '{"tenant_id":1,"meter_id":1,"event_no":"e1","collected_at":"2026-02-28T12:00:00Z","cumulative_value":100}'
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' -d '{"tenant_id":1,"meter_id":1,"event_no":"e2","collected_at":"2026-03-02T12:00:00Z","cumulative_value":160}'
curl 'http://localhost:8000/tenants/1/bills?month=2026-03'
curl -X POST http://localhost:8000/tenants/1/bills/close -H 'Content-Type: application/json' -d '{"month":"2026-02"}'
```

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx requests

# 需要一个可访问的 PostgreSQL（默认 localhost:5432/waterdb）
export DATABASE_URL="postgresql+psycopg2://water:water@localhost:5432/waterdb"

uvicorn app.main:app --reload --port 8000   # 启动服务
pytest tests/ -q                            # 功能测试（真实数据库）
```

## 项目结构

```
├── app/
│   ├── main.py            # FastAPI 路由、异常处理、响应组装
│   ├── models.py          # SQLAlchemy 模型（租户/水表/费率/读数/月账/分段/调整）
│   ├── db.py              # 引擎、会话、启动等待与建表
│   ├── schemas.py         # 请求校验（时区、非负累计值等）
│   ├── timeutil.py        # UTC 换算、账期月份工具
│   └── services/
│       ├── readings.py    # 读数提交：幂等/冲突/更正校验
│       └── billing.py     # 读数链重算、分摊、月账维护、关账、调整
├── tests/test_api.py      # 功能测试
├── examples/demo.sh       # curl 调用示例
├── examples/demo.py       # Python 调用示例（含并发校验）
├── Dockerfile
└── docker-compose.yml
```
