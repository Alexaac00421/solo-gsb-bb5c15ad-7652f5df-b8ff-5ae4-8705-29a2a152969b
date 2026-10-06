#!/usr/bin/env bash
# 园区分表用水结算服务 —— curl 调用示例
# 用法: BASE=http://localhost:8000 bash examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

req() {  # req <method> <path> [json]
  local method="$1" path="$2" body="${3:-}"
  echo ""
  echo "### $method $BASE$path"
  [ -n "$body" ] && echo ">>> $body"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo ""
}

echo "== 0. 健康检查 =="
req GET /health

echo "== 1. 创建租户 / 水表 / 费率 =="
TENANT=$(curl -sS -X POST "$BASE/tenants" -H 'Content-Type: application/json' -d "{\"name\":\"park-demo-sh-$RANDOM$RANDOM\"}")
echo "$TENANT"
TENANT_ID=$(echo "$TENANT" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
METER=$(curl -sS -X POST "$BASE/meters" -H 'Content-Type: application/json' -d "{\"tenant_id\":$TENANT_ID,\"code\":\"WM-001\"}")
echo "$METER"
METER_ID=$(echo "$METER" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
req POST /rates '{"effective_from":"2026-01-01T00:00:00+00:00","price_per_unit":2.0}'
req POST /rates '{"effective_from":"2026-03-01T00:00:00+00:00","price_per_unit":3.0}'

echo "== 2. 提交读数（跨 UTC 月、跨费率，按时长比例分摊）=="
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e1\",\"collected_at\":\"2026-02-28T12:00:00+00:00\",\"cumulative_value\":100}"
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e2\",\"collected_at\":\"2026-03-02T12:00:00+08:00\",\"cumulative_value\":160}"

echo "== 3. 幂等与冲突 =="
echo "-- 同事件同内容 -> 200 返回原结果"
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e2\",\"collected_at\":\"2026-03-02T12:00:00+08:00\",\"cumulative_value\":160}"
echo "-- 同事件异内容 -> 409"
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e2\",\"collected_at\":\"2026-03-02T12:00:00+08:00\",\"cumulative_value\":999}" || true
echo "-- 同表同时刻异值（非更正）-> 409"
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e3\",\"collected_at\":\"2026-03-02T04:00:00+00:00\",\"cumulative_value\":170}" || true
echo "-- 引用原事件号的更正 -> 接受"
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e4\",\"ref_event_no\":\"e2\",\"collected_at\":\"2026-03-02T12:00:00+08:00\",\"cumulative_value\":150}"

echo "== 4. 乱序 / 异常 / 衔接 =="
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e5\",\"collected_at\":\"2026-02-20T00:00:00+00:00\",\"cumulative_value\":90}"
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e6\",\"collected_at\":\"2026-03-10T00:00:00+00:00\",\"cumulative_value\":80}"
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e7\",\"collected_at\":\"2026-03-20T00:00:00+00:00\",\"cumulative_value\":200}"

echo "== 5. 查询月账（用量/费用分段/异常明细）=="
req GET "/tenants/$TENANT_ID/bills?month=2026-02"
req GET "/tenants/$TENANT_ID/bills?month=2026-03"

echo "== 6. 关账 2026-02，迟到更正 -> 差额记入下一未关账月 =="
req POST "/tenants/$TENANT_ID/bills/close" '{"month":"2026-02"}'
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"e9\",\"ref_event_no\":\"e5\",\"collected_at\":\"2026-02-20T00:00:00+00:00\",\"cumulative_value\":85}"
req GET "/tenants/$TENANT_ID/bills?month=2026-03"

echo "== 7. 重复关账（幂等，不重复入账）=="
req POST "/tenants/$TENANT_ID/bills/close" '{"month":"2026-02"}'

echo "== 8. 错误示例 =="
echo "-- 无时区时间 -> 400"
req POST /readings "{\"tenant_id\":$TENANT_ID,\"meter_id\":$METER_ID,\"event_no\":\"bad1\",\"collected_at\":\"2026-03-01 12:00:00\",\"cumulative_value\":1}" || true
echo "-- 水表与租户不匹配 -> 400/404"
req POST /readings "{\"tenant_id\":99999,\"meter_id\":$METER_ID,\"event_no\":\"bad2\",\"collected_at\":\"2026-03-01T00:00:00Z\",\"cumulative_value\":1}" || true
echo "-- 非法月份 -> 400"
req GET "/tenants/$TENANT_ID/bills?month=2026-13" || true

echo ""
echo "DONE"
