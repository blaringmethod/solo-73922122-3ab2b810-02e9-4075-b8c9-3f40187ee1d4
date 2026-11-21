#!/usr/bin/env bash
# ETag 重启校验: 重启数据库与 API 后, 旧标签仍可继续使用(304), 数据不变.
#
# 默认走 docker compose; 本机无 Docker 时可用环境变量注入重启/就绪命令:
#   RESTART_CMD="bash -c '...'"   重启 db+api 的命令
#   READY_CMD="bash -c '...'"     等待服务重新就绪的命令(返回 0)
set -euo pipefail
cd "$(dirname "$0")/.."

API=${API_URL:-http://localhost:8000}
SID="ST-ETAG-RESTART-$(date +%Y%m%d%H%M%S)"
DAY_FROM="2021-06-15T00:00:00Z"
DAY_TO="2021-06-16T00:00:00Z"

echo "== 重启前: 建站并写入当日观测, 取 ETag =="
curl -fsS -X POST "$API/stations" -H 'Content-Type: application/json' \
  -d "{\"station_id\":\"$SID\",\"name\":\"ETag重启校验站\"}" >/dev/null
curl -fsS -X POST "$API/changesets" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\":\"insert_period\",\"station_id\":\"$SID\",\"watershed\":\"WS-A\",
     \"valid_from\":\"2020-01-01T00:00:00Z\",\"valid_to\":\"2023-01-01T00:00:00Z\"},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2021-06-15T08:00:00Z\",\"value\":1.0}
  ]
}" >/dev/null

HDRS=$(mktemp); trap 'rm -f "$HDRS"' EXIT
curl -fsS -D "$HDRS" -o /dev/null -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO"
ETAG=$(grep -i '^etag:' "$HDRS" | tr -d '\r' | awk '{print $2}')
echo "重启前 ETag: $ETAG"

echo "== 重启数据库与 API =="
if [ -n "${RESTART_CMD:-}" ]; then
  bash -c "$RESTART_CMD"
else
  docker compose restart db api
  docker compose up -d --wait db api
fi

echo "== 等待服务就绪 =="
if [ -n "${READY_CMD:-}" ]; then
  bash -c "$READY_CMD"
else
  for _ in $(seq 1 60); do
    curl -fsS "$API/health" >/dev/null 2>&1 && break
    sleep 1
  done
fi
curl -fsS "$API/health"; echo

echo "== 重启后: 数据仍在, 旧 ETag 仍然有效(304) =="
BODY=$(curl -fsS -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO")
echo "$BODY" | jq .
[ "$(echo "$BODY" | jq 'length')" = "1" ] || { echo "FAIL: data missing after restart"; exit 1; }

CODE=$(curl -sS -o /dev/null -w '%{http_code}' -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO" \
  -H "If-None-Match: $ETAG")
echo "旧标签条件请求: HTTP $CODE (预期 304)"
[ "$CODE" = "304" ] || { echo "FAIL: expected 304 after restart, got $CODE"; exit 1; }

echo "== 重启后: 新写入仍会换标签 =="
curl -fsS -X POST "$API/observations" -H 'Content-Type: application/json' \
  -d "{\"station_id\":\"$SID\",\"observed_at\":\"2021-06-15T12:00:00Z\",\"value\":2.0}" >/dev/null
CODE2=$(curl -sS -o /dev/null -w '%{http_code}' -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO" \
  -H "If-None-Match: $ETAG")
echo "旧标签条件请求(写入后): HTTP $CODE2 (预期 200)"
[ "$CODE2" = "200" ] || { echo "FAIL: expected 200 after post-restart write, got $CODE2"; exit 1; }

echo "== ETag 重启校验通过 =="
