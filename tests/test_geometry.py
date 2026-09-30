"""几何核验核心的单元测试（不需要数据库）。"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from app.geometry import (
    GeometryError,
    build_polygon,
    crossings_for_region,
    ensure_aware_utc,
)

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
# U 形凹多边形（顶部中间开缺口）
U_SHAPE = [
    (0.0, 0.0),
    (20.0, 0.0),
    (20.0, 20.0),
    (15.0, 20.0),
    (15.0, 5.0),
    (5.0, 5.0),
    (5.0, 20.0),
    (0.0, 20.0),
]
ALL_TIME = (0.0, 1e9)


def verify(polygon_points, times, pts, window=ALL_TIME):
    poly = build_polygon(polygon_points)
    return crossings_for_region(poly, times, pts, window[0], window[1])


def assert_interval(got, expected, tol=1e-9):
    assert len(got) == len(expected), f"区间数不符: {got} != {expected}"
    for (a, b), (ea, eb) in zip(got, expected):
        assert a == pytest.approx(ea, abs=tol) and b == pytest.approx(eb, abs=tol), (
            f"{(a, b)} != {(ea, eb)}"
        )


def test_straight_cross_interpolated_not_sampled():
    """采样点全部在区域外，但直线穿过区域：交点时刻必须插值得到。"""
    times = [0.0, 10.0]
    pts = [(-5.0, 5.0), (15.0, 5.0)]
    out = verify(SQUARE, times, pts)
    assert_interval(out, [(2.5, 7.5)])


def test_multiple_segments_with_interior_waypoint():
    """采样点落在内部：整段核验，区间在采样时刻处相接并合并。"""
    times = [0.0, 5.0, 10.0]
    pts = [(-5.0, 5.0), (5.0, 5.0), (15.0, 5.0)]
    out = verify(SQUARE, times, pts)
    assert_interval(out, [(2.5, 7.5)])


def test_boundary_edge_contact_counts_as_crossing():
    """沿边界运动属于穿越（边界接触算穿越）。"""
    times = [0.0, 10.0]
    pts = [(-5.0, 10.0), (15.0, 10.0)]  # 与顶边重合
    out = verify(SQUARE, times, pts)
    assert_interval(out, [(2.5, 7.5)])


def test_single_vertex_touch_same_entry_exit():
    """单点擦过一个凸顶点：返回起止时刻相同的区间。

    直线 y = x + 10 仅在 (0,10) 与正方形接触，两侧都在区域外。
    """
    times = [0.0, 10.0]
    pts = [(-5.0, 5.0), (5.0, 15.0)]
    out = verify(SQUARE, times, pts)
    assert_interval(out, [(5.0, 5.0)])
    assert out[0][0] == out[0][1]


def test_touching_intervals_merge_across_segment():
    """两段各产生的穿越区间在分界点相接（含端点重合），必须合并。"""
    times = [0.0, 10.0, 20.0]
    pts = [(-5.0, 10.0), (0.0, 10.0), (15.0, 10.0)]
    out = verify(SQUARE, times, pts)
    # 第二段 x: 0->15，区域内 0..10，对应 t=10 .. 10+10/15*10
    assert_interval(out, [(10.0, 10.0 + 100.0 / 15.0)])


def test_concave_polygon_two_separate_crossings():
    """凹多边形：水平线穿过两翼，中间缺口不属于穿越，区间不合并。"""
    times = [0.0, 10.0]
    pts = [(-5.0, 10.0), (25.0, 10.0)]  # 速度 3/秒
    out = verify(U_SHAPE, times, pts)
    assert_interval(
        out,
        [
            (5.0 / 30.0 * 10, 10.0 / 30.0 * 10),
            (20.0 / 30.0 * 10, 25.0 / 30.0 * 10),
        ],
    )


def test_start_inside_region():
    """轨迹起点就在区域内：穿越起点为轨迹起点。"""
    times = [0.0, 10.0]
    pts = [(5.0, 5.0), (15.0, 5.0)]
    out = verify(SQUARE, times, pts)
    assert_interval(out, [(0.0, 5.0)])


def test_entirely_inside():
    times = [2.0, 8.0]
    pts = [(1.0, 1.0), (9.0, 9.0)]
    out = verify(SQUARE, times, pts)
    assert_interval(out, [(2.0, 8.0)])


def test_stationary_point_inside_zero_length_segments():
    """零长段（相邻点坐标相同、时刻仍递增）按静止点处理。"""
    times = [0.0, 10.0, 20.0]
    pts = [(5.0, 5.0), (5.0, 5.0), (5.0, 5.0)]
    out = verify(SQUARE, times, pts)
    assert_interval(out, [(0.0, 20.0)])


def test_stationary_outside():
    times = [0.0, 10.0]
    pts = [(50.0, 50.0), (50.0, 50.0)]
    assert verify(SQUARE, times, pts) == []


def test_valid_window_half_open_end():
    """生效时段左闭右开：恰好落在 valid_to 的接触不算穿越。"""
    times = [0.0, 10.0]
    pts = [(-5.0, 5.0), (15.0, 5.0)]
    # 出口 7.5 恰好等于窗口结束 → 区间裁到 7.5 且不含该点
    out = verify(SQUARE, times, pts, window=(0.0, 7.5))
    assert_interval(out, [(2.5, 7.5)])

    # 单点接触恰好在窗口结束时刻 → 无穿越
    pts2 = [(-5.0, 5.0), (5.0, 15.0)]  # 接触在 t=5
    assert verify(SQUARE, times, pts2, window=(0.0, 5.0)) == []


def test_valid_window_closed_start():
    """恰好落在 valid_from 的接触算穿越。"""
    times = [0.0, 10.0]
    pts = [(-5.0, 5.0), (5.0, 15.0)]  # 接触在 t=5
    out = verify(SQUARE, times, pts, window=(5.0, 100.0))
    assert_interval(out, [(5.0, 5.0)])


def test_window_clips_interval():
    times = [0.0, 10.0]
    pts = [(-5.0, 5.0), (15.0, 5.0)]
    out = verify(SQUARE, times, pts, window=(4.0, 6.0))
    assert_interval(out, [(4.0, 6.0)])


def test_region_outside_trajectory_time_skipped():
    times = [0.0, 10.0]
    pts = [(-5.0, 5.0), (15.0, 5.0)]
    assert verify(SQUARE, times, pts, window=(100.0, 200.0)) == []
    assert verify(SQUARE, times, pts, window=(-100.0, 0.0)) == []


def test_no_contact():
    times = [0.0, 10.0]
    pts = [(-5.0, 50.0), (15.0, 50.0)]
    assert verify(SQUARE, times, pts) == []


def test_build_polygon_rejects_invalid():
    with pytest.raises(GeometryError, match="至少需要 3"):
        build_polygon([(0, 0), (1, 1)])
    with pytest.raises(GeometryError, match="重复顶点"):
        build_polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 0.0)])
    with pytest.raises(GeometryError, match="有限"):
        build_polygon([(0.0, 0.0), (float("nan"), 1.0), (1.0, 1.0)])
    with pytest.raises(GeometryError, match="有限"):
        build_polygon([(0.0, 0.0), (float("inf"), 1.0), (1.0, 1.0)])
    # 蝴蝶结自交
    with pytest.raises(GeometryError, match="非法多边形"):
        build_polygon([(0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0)])
    # 共线面积为 0（GEOS 可能报自交，也可能报面积为 0）
    with pytest.raises(GeometryError, match="非法多边形|面积"):
        build_polygon([(0.0, 0.0), (5.0, 5.0), (10.0, 10.0)])


def test_closing_vertex_tolerated():
    """调用方若重复给出首尾闭合点，应被接受。"""
    poly = build_polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
    assert poly.area == pytest.approx(100.0)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        ensure_aware_utc(datetime(2026, 10, 1, 0, 0, 0))
    aware = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
    assert ensure_aware_utc(aware) == aware
