#!/usr/bin/env python3
"""临时限行核验服务调用示例（Python，仅依赖标准库）。

运行：python3 examples/usage_python.py [BASE_URL]
默认 BASE_URL=http://localhost:8000
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n== {title} ==  HTTP {status}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    show("健康检查", *call("GET", "/health"))

    # 1. 登记区域
    status, region = call(
        "POST",
        "/regions",
        {
            "name": "1号仓库禁行区",
            "vertices": [
                {"x": 0, "y": 0},
                {"x": 10, "y": 0},
                {"x": 10, "y": 10},
                {"x": 0, "y": 10},
            ],
            "valid_from": "2026-10-01T00:00:00Z",
            "valid_to": "2026-10-02T00:00:00Z",
        },
    )
    show("登记区域", status, region)
    rid = region["id"]

    # 2. 轨迹核验：直线匀速穿过；交点时刻由插值得到
    status, result = call(
        "POST",
        "/verify",
        {
            "points": [
                {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
                {"t": "2026-10-01T00:00:10Z", "x": 15, "y": 5},
            ]
        },
    )
    show("核验轨迹（插值交点 2.5s / 7.5s）", status, result)

    # 3. 区域未生效 -> safe=true
    status, result = call(
        "POST",
        "/verify",
        {
            "points": [
                {"t": "2026-10-03T00:00:00Z", "x": -5, "y": 5},
                {"t": "2026-10-03T00:00:10Z", "x": 15, "y": 5},
            ]
        },
    )
    show("区域未生效（应 safe=true）", status, result)

    # 4. 非法输入：逆序轨迹时刻 -> 422，且不保存
    status, err = call(
        "POST",
        "/verify",
        {
            "points": [
                {"t": "2026-10-01T00:00:10Z", "x": 0, "y": 0},
                {"t": "2026-10-01T00:00:00Z", "x": 10, "y": 0},
            ]
        },
    )
    show("逆序轨迹（应 422）", status, err)

    # 5. 乐观锁修订
    status, updated = call(
        "PUT",
        f"/regions/{rid}",
        {
            "name": "1号仓库禁行区(修订)",
            "vertices": [
                {"x": 0, "y": 0},
                {"x": 10, "y": 0},
                {"x": 10, "y": 10},
                {"x": 0, "y": 10},
            ],
            "valid_from": "2026-10-01T00:00:00Z",
            "valid_to": "2026-10-02T00:00:00Z",
            "expected_revision": 1,
        },
    )
    show("修订区域（revision 应变为 2）", status, updated)

    show("删除区域", *call("DELETE", f"/regions/{rid}"))


if __name__ == "__main__":
    main()
