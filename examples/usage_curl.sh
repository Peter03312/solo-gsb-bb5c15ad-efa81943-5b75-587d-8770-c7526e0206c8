#!/usr/bin/env bash
# 临时限行核验服务调用示例（curl）。
# 用法：BASE_URL=http://localhost:8000 ./examples/usage_curl.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
j() { python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin),ensure_ascii=False,indent=2))" 2>/dev/null || cat; }

echo "== 1) 健康检查 =="
curl -sS "$BASE_URL/health"; echo

echo "== 2) 登记一个正方形限行区域（生效一整天，UTC）=="
REGION=$(curl -sS -X POST "$BASE_URL/regions" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "1号仓库禁行区",
    "vertices": [
      {"x": 0, "y": 0},
      {"x": 10, "y": 0},
      {"x": 10, "y": 10},
      {"x": 0, "y": 10}
    ],
    "valid_from": "2026-10-01T00:00:00Z",
    "valid_to":   "2026-10-02T00:00:00Z"
  }')
echo "$REGION" | j
RID=$(echo "$REGION" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "== 3) 列出全部区域（含区域集合版本 region_set_version）=="
curl -sS "$BASE_URL/regions" | j

echo "== 4) 提交轨迹核验：采样点都在区域外，但直线匀速穿过区域 =="
curl -sS -X POST "$BASE_URL/verify" \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
      {"t": "2026-10-01T00:00:10Z", "x": 15, "y": 5}
    ]
  }' | j
# 期望：crossings = 00:00:02.5 ~ 00:00:07.5（交点按匀速插值，非采样点），safe=false

echo "== 5) 单点擦过区域顶点：起止时刻相同 =="
curl -sS -X POST "$BASE_URL/verify" \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
      {"t": "2026-10-01T00:00:10Z", "x": 5, "y": 15}
    ]
  }' | j

echo "== 6) 非法输入示例（自交蝴蝶结多边形，返回 422，且不会保存）=="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE_URL/regions" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "非法区域",
    "vertices": [{"x":0,"y":0},{"x":10,"y":10},{"x":10,"y":0},{"x":0,"y":10}],
    "valid_from": "2026-10-01T00:00:00Z",
    "valid_to":   "2026-10-02T00:00:00Z"
  }'

echo "== 7) 修订区域（乐观锁 expected_revision）=="
curl -sS -X PUT "$BASE_URL/regions/$RID" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "1号仓库禁行区(扩大)",
    "vertices": [
      {"x": 0, "y": 0}, {"x": 20, "y": 0},
      {"x": 20, "y": 20}, {"x": 0, "y": 20}
    ],
    "valid_from": "2026-10-01T00:00:00Z",
    "valid_to":   "2026-10-02T00:00:00Z",
    "expected_revision": 1
  }' | j

echo "== 8) 删除区域 =="
curl -sS -X DELETE "$BASE_URL/regions/$RID"; echo
