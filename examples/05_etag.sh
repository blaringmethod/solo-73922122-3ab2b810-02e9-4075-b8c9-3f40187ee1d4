#!/usr/bin/env bash
# 示例5: 站日观测 ETag —— 整日查询条件请求(304 复用)与更正后重取(200 新标签)
# 依赖 curl 与 jq
set -euo pipefail

API=${API_URL:-http://localhost:8000}
SID=${SID:-ST-ETAG-$(date +%Y%m%d%H%M%S)}
DAY_FROM="2021-06-15T00:00:00Z"
DAY_TO="2021-06-16T00:00:00Z"

echo "== 站点: $SID =="

echo "-- 1. 建站 + 归属时段 + 当日两条观测"
curl -sS -X POST "$API/stations" -H 'Content-Type: application/json' \
  -d "{\"station_id\":\"$SID\",\"name\":\"ETag示例站\"}" | jq .
curl -sS -X POST "$API/changesets" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\":\"insert_period\",\"station_id\":\"$SID\",\"watershed\":\"WS-OLD\",
     \"valid_from\":\"2020-01-01T00:00:00Z\",\"valid_to\":\"2023-01-01T00:00:00Z\"},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2021-06-15T08:00:00Z\",\"value\":10.0},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2021-06-15T20:00:00Z\",\"value\":12.5}
  ]
}" | jq .

echo
echo "-- 2. 首次整日查询: 200 + ETag + 明细"
HDRS=$(mktemp)
BODY=$(curl -sS -D "$HDRS" -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO")
ETAG=$(grep -i '^etag:' "$HDRS" | tr -d '\r' | awk '{print $2}')
echo "$BODY" | jq .
echo "ETag: $ETAG"

echo
echo "-- 3. 带 If-None-Match 重取(数据未变): 304, 无响应体, 不查明细"
CODE=$(curl -sS -o /dev/null -w '%{http_code}' -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO" \
  -H "If-None-Match: $ETAG")
echo "HTTP $CODE (预期 304)"
[ "$CODE" = "304" ] || { echo "FAIL: expected 304"; exit 1; }

echo
echo "-- 4. 历史更正: 当日所在子区间改属 WS-NEW(提交后该站日换标签)"
curl -sS -X POST "$API/stations/$SID/correct" -H 'Content-Type: application/json' -d "{
  \"valid_from\":\"2021-01-01T00:00:00Z\",
  \"valid_to\":\"2022-01-01T00:00:00Z\",
  \"watershed\":\"WS-NEW\"
}" | jq .

echo
echo "-- 5. 仍带旧标签重取: 200 + 新 ETag + 更正后明细(watershed 已变)"
HDRS2=$(mktemp)
BODY2=$(curl -sS -D "$HDRS2" -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO" \
  -H "If-None-Match: $ETAG")
CODE2=$(grep -i '^HTTP/' "$HDRS2" | tail -1 | awk '{print $2}')
ETAG2=$(grep -i '^etag:' "$HDRS2" | tr -d '\r' | awk '{print $2}')
echo "HTTP $CODE2 (预期 200)"
echo "$BODY2" | jq .
echo "旧 ETag: $ETAG"
echo "新 ETag: $ETAG2"
[ "$CODE2" = "200" ] || { echo "FAIL: expected 200"; exit 1; }
[ "$ETAG2" != "$ETAG" ] || { echo "FAIL: ETag did not change after correction"; exit 1; }

echo
echo "-- 6. 带新标签再取: 304"
CODE3=$(curl -sS -o /dev/null -w '%{http_code}' -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO" \
  -H "If-None-Match: $ETAG2")
echo "HTTP $CODE3 (预期 304)"
[ "$CODE3" = "304" ] || { echo "FAIL: expected 304"; exit 1; }

echo
echo "-- 7. 删空当天: 标签再换, 空日仍可 304"
OBS_IDS=$(echo "$BODY2" | jq -r '.[].observation_id')
OPS=$(echo "$OBS_IDS" | jq -R '{"op":"delete_observation","observation_id":(.|tonumber)}' | jq -s .)
curl -sS -X POST "$API/changesets" -H 'Content-Type: application/json' \
  -d "$(jq -n --argjson ops "$OPS" '{operations:$ops}')" >/dev/null
HDRS3=$(mktemp)
curl -sS -D "$HDRS3" -o /dev/null -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO"
ETAG3=$(grep -i '^etag:' "$HDRS3" | tr -d '\r' | awk '{print $2}')
echo "空日 ETag: $ETAG3 (与 $ETAG2 不同)"
[ "$ETAG3" != "$ETAG2" ] || { echo "FAIL: empty-day ETag unchanged after delete"; exit 1; }
CODE4=$(curl -sS -o /dev/null -w '%{http_code}' -G "$API/stations/$SID/observations" \
  --data-urlencode "from=$DAY_FROM" --data-urlencode "to=$DAY_TO" \
  -H "If-None-Match: $ETAG3")
[ "$CODE4" = "304" ] || { echo "FAIL: expected 304 on empty day, got $CODE4"; exit 1; }
echo "HTTP $CODE4 (预期 304)"

echo
echo "== ETag 示例通过 =="
