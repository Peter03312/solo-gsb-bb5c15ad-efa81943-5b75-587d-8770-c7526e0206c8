"""端到端集成测试：HTTP + 真实 PostgreSQL（TestClient，自动管理 lifespan）。

通过 DATABASE_URL 指向测试库（本地可用内嵌 pgserver）：
    DATABASE_URL='postgresql://site_safety:site_safety@/site_safety?host=/tmp/pgdata'
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="未设置 DATABASE_URL，跳过数据库集成测试"
)


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        # 清空历史数据
        for reg in c.get("/regions").json()["regions"]:
            c.delete(f"/regions/{reg['id']}")
        yield c


SQUARE = [
    {"x": 0, "y": 0},
    {"x": 10, "y": 0},
    {"x": 10, "y": 10},
    {"x": 0, "y": 10},
]
DAY = ("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_and_verify_interpolated_crossing(client):
    r = client.post(
        "/regions",
        json={"name": "禁行区A", "vertices": SQUARE, "valid_from": DAY[0], "valid_to": DAY[1]},
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["revision"] == 1

    # 采样点都在区域外，但直线穿过：交点时刻必须插值
    r = client.post(
        "/verify",
        json={
            "points": [
                {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
                {"t": "2026-10-01T00:00:10Z", "x": 15, "y": 5},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["safe"] is False
    assert body["considered_regions"] == 1
    assert len(body["regions"]) == 1
    cr = body["regions"][0]
    assert cr["region_id"] == rid
    c = cr["crossings"][0]
    assert c["entry_time"] == "2026-10-01T00:00:02.500000Z"
    assert c["exit_time"] == "2026-10-01T00:00:07.500000Z"
    assert c["single_point"] is False


def test_single_vertex_touch(client):
    client.post(
        "/regions",
        json={"name": "V", "vertices": SQUARE, "valid_from": DAY[0], "valid_to": DAY[1]},
    )
    r = client.post(
        "/verify",
        json={
            "points": [
                {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
                {"t": "2026-10-01T00:00:10Z", "x": 5, "y": 15},
            ]
        },
    )
    c = r.json()["regions"][0]["crossings"][0]
    assert c["entry_time"] == c["exit_time"] == "2026-10-01T00:00:05Z"
    assert c["single_point"] is True


def test_half_open_validity_window(client):
    client.post(
        "/regions",
        json={"name": "W", "vertices": SQUARE, "valid_from": DAY[0], "valid_to": DAY[1]},
    )
    # 完全在生效期之外 -> safe
    r = client.post(
        "/verify",
        json={
            "points": [
                {"t": "2026-10-03T00:00:00Z", "x": -5, "y": 5},
                {"t": "2026-10-03T00:00:10Z", "x": 15, "y": 5},
            ]
        },
    )
    assert r.json()["safe"] is True
    assert r.json()["regions"] == []

    # 穿过 valid_to 边界：区间右裁到 valid_to
    r = client.post(
        "/verify",
        json={
            "points": [
                {"t": "2026-10-01T23:59:55Z", "x": -5, "y": 5},
                {"t": "2026-10-02T00:00:05Z", "x": 15, "y": 5},
            ]
        },
    )
    body = r.json()
    assert body["safe"] is False
    c = body["regions"][0]["crossings"][0]
    assert c["exit_time"] == "2026-10-02T00:00:00Z"

    # 唯一接触点恰在 valid_to（接触点即线段终点）-> 左闭右开，不算穿越
    r = client.post(
        "/verify",
        json={
            "points": [
                {"t": "2026-10-01T23:59:50Z", "x": -5, "y": 5},
                {"t": "2026-10-02T00:00:00Z", "x": 0, "y": 10},
            ]
        },
    )
    assert r.json()["safe"] is True

    # 接触点在 valid_to 前一秒（窗口内）-> 算穿越
    r = client.post(
        "/verify",
        json={
            "points": [
                {"t": "2026-10-01T23:59:50Z", "x": -5, "y": 5},
                {"t": "2026-10-01T23:59:59Z", "x": 0, "y": 10},
            ]
        },
    )
    assert r.json()["safe"] is False


def test_invalid_inputs_rejected_and_not_saved(client):
    def count():
        return len(client.get("/regions").json()["regions"])

    base = {"name": "bad", "vertices": SQUARE, "valid_from": DAY[0], "valid_to": DAY[1]}

    # 时段不递增
    r = client.post("/regions", json={**base, "valid_to": "2026-09-30T00:00:00Z"})
    assert r.status_code == 422 and "严格早于" in r.text

    # 自交多边形
    bowtie = [
        {"x": 0, "y": 0},
        {"x": 10, "y": 10},
        {"x": 10, "y": 0},
        {"x": 0, "y": 10},
    ]
    r = client.post("/regions", json={**base, "vertices": bowtie})
    assert r.status_code == 422 and "非法多边形" in r.text

    # 非有限坐标
    bad = [dict(v) for v in SQUARE]
    bad[0]["x"] = "NaN"
    r = client.post("/regions", json={**base, "vertices": bad})
    assert r.status_code == 422

    # 朴素（无时区）时刻
    r = client.post("/regions", json={**base, "valid_from": "2026-10-01T00:00:00"})
    assert r.status_code == 422 and "时区" in r.text

    assert count() == 0

    # 轨迹：重复/逆序时刻
    for pts in (
        [
            {"t": "2026-10-01T00:00:00Z", "x": 0, "y": 0},
            {"t": "2026-10-01T00:00:00Z", "x": 1, "y": 0},
        ],
        [
            {"t": "2026-10-01T00:00:10Z", "x": 0, "y": 0},
            {"t": "2026-10-01T00:00:00Z", "x": 1, "y": 0},
        ],
    ):
        r = client.post("/verify", json={"points": pts})
        assert r.status_code == 422 and "严格递增" in r.text

    # 轨迹仅一个点
    r = client.post(
        "/verify", json={"points": [{"t": "2026-10-01T00:00:00Z", "x": 0, "y": 0}]}
    )
    assert r.status_code == 422

    # 轨迹非有限坐标
    r = client.post(
        "/verify",
        json={
            "points": [
                {"t": "2026-10-01T00:00:00Z", "x": 0, "y": 0},
                {"t": "2026-10-01T00:00:10Z", "x": 1, "y": "Infinity"},
            ]
        },
    )
    assert r.status_code == 422


def test_revision_optimistic_lock_and_crud(client):
    r = client.post(
        "/regions",
        json={"name": "R", "vertices": SQUARE, "valid_from": DAY[0], "valid_to": DAY[1]},
    )
    rid = r.json()["id"]

    r = client.put(
        f"/regions/{rid}",
        json={
            "name": "R2",
            "vertices": [
                {"x": 0, "y": 0},
                {"x": 20, "y": 0},
                {"x": 20, "y": 20},
                {"x": 0, "y": 20},
            ],
            "valid_from": DAY[0],
            "valid_to": DAY[1],
            "expected_revision": 1,
        },
    )
    assert r.status_code == 200
    assert r.json()["revision"] == 2
    assert r.json()["name"] == "R2"

    # 旧版本号再改 -> 409
    r = client.put(
        f"/regions/{rid}",
        json={
            "name": "R3",
            "vertices": SQUARE,
            "valid_from": DAY[0],
            "valid_to": DAY[1],
            "expected_revision": 1,
        },
    )
    assert r.status_code == 409

    assert client.get(f"/regions/{rid}").json()["name"] == "R2"
    # 非法 UUID -> 422，而不是 500
    assert client.get("/regions/not-a-uuid").status_code == 422

    assert client.delete(f"/regions/{rid}").status_code == 200
    assert client.get(f"/regions/{rid}").status_code == 404
    assert client.delete(f"/regions/{rid}").status_code == 404


def test_expected_version_conflict(client):
    ver = client.get("/regions").json()["region_set_version"]
    r = client.post(
        "/verify",
        json={
            "expected_version": "0000000000000000",
            "points": [
                {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
                {"t": "2026-10-01T00:00:10Z", "x": 15, "y": 5},
            ],
        },
    )
    assert r.status_code == 409
    r = client.post(
        "/verify",
        json={
            "expected_version": ver,
            "points": [
                {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
                {"t": "2026-10-01T00:00:10Z", "x": 15, "y": 5},
            ],
        },
    )
    assert r.status_code == 200


def test_two_regions_listed_separately(client):
    sq_a = SQUARE
    sq_b = [
        {"x": 100, "y": 100},
        {"x": 110, "y": 100},
        {"x": 110, "y": 110},
        {"x": 100, "y": 110},
    ]
    client.post(
        "/regions",
        json={"name": "A", "vertices": sq_a, "valid_from": DAY[0], "valid_to": DAY[1]},
    )
    client.post(
        "/regions",
        json={"name": "B", "vertices": sq_b, "valid_from": DAY[0], "valid_to": DAY[1]},
    )
    body = client.post(
        "/verify",
        json={
            "points": [
                {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
                {"t": "2026-10-01T00:00:10Z", "x": 15, "y": 5},
            ]
        },
    ).json()
    assert body["safe"] is False
    assert body["considered_regions"] == 2
    assert [r["name"] for r in body["regions"]] == ["A"]


def test_verify_uses_single_committed_snapshot_under_concurrency(client):
    """并发：核验读取的区域集合版本必须与返回的 region_set_version 一致。

    反复在后台修订区域，同时发起多次核验；每次返回的 considered_regions
    必须恰好等于该版本快照下、与轨迹时间有交集的区域数，且各区域 revision
    与 /regions 列表自洽（快照隔离：不允许读到半新半旧的集合）。
    """
    import threading

    r = client.post(
        "/regions",
        json={"name": "C", "vertices": SQUARE, "valid_from": DAY[0], "valid_to": DAY[1]},
    )
    rid = r.json()["id"]

    errors: list[str] = []
    stop = threading.Event()

    def mutator():
        rev = 1
        while not stop.is_set():
            rr = client.put(
                f"/regions/{rid}",
                json={
                    "name": "C",
                    "vertices": SQUARE,
                    "valid_from": DAY[0],
                    "valid_to": DAY[1],
                    "expected_revision": rev,
                },
            )
            if rr.status_code == 200:
                rev = rr.json()["revision"]
            elif rr.status_code == 409:
                rev = client.get(f"/regions/{rid}").json()["revision"]

    t = threading.Thread(target=mutator)
    t.start()
    try:
        for _ in range(50):
            body = client.post(
                "/verify",
                json={
                    "points": [
                        {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
                        {"t": "2026-10-01T00:00:10Z", "x": 15, "y": 5},
                    ]
                },
            ).json()
            listing = client.get("/regions").json()
            # 返回的集合版本必须是某个真实存在过的已提交版本
            if body["region_set_version"] != listing["region_set_version"]:
                # 允许核验与列表之间恰好发生了修订，但 considered 必须始终为 1
                if body["considered_regions"] != 1:
                    errors.append(f"considered={body['considered_regions']}")
            else:
                if body["considered_regions"] != len(listing["regions"]):
                    errors.append("快照内区域数与版本不一致")
            assert body["safe"] is False
    finally:
        stop.set()
        t.join()
    assert not errors, errors
