"""几何与时间核验核心（不依赖数据库/HTTP，便于单元测试）。

约定
----
* 平面坐标为简单的笛卡尔坐标；区域为简单多边形（无自交、无孔洞）。
* 轨迹由按时刻严格递增的点组成，相邻点之间直线匀速运动。
* 区域生效时段为左闭右开 ``[valid_from, valid_to)``：
  恰好在 ``valid_to`` 的接触不算穿越；恰好在 ``valid_from`` 的接触算。
* 边界接触算穿越；单点接触返回起止时刻相同的区间。
* 同一区域相接或重叠的穿越段合并；不同区域分别列出。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable, List, Sequence

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    Point,
    Polygon,
    shape,
)
from shapely.geometry import mapping
from shapely.validation import explain_validity

# 浮点比较与钳制容差
_EPS = 1e-12


class GeometryError(ValueError):
    """区域多边形非法（含非有限坐标）。"""


def ensure_aware_utc(t: datetime) -> datetime:
    """要求带时区信息，并规范为 UTC。"""
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("时刻必须带时区偏移（建议直接使用 UTC，如 2026-10-01T00:00:00Z）")
    return t.astimezone(timezone.utc)


def to_epoch(t: datetime) -> float:
    return ensure_aware_utc(t).timestamp()


def from_epoch(ts: float) -> datetime:
    # 微秒取整，避免二进制浮点尾差导致序列化结果不稳定
    return datetime.fromtimestamp(round(ts, 6), tz=timezone.utc)


def build_polygon(vertices: Sequence[tuple[float, float]]) -> Polygon:
    """根据顶点列表构造并校验简单多边形。

    顶点按顺序给出（首尾自动闭合，不要重复给出末点）。
    以下情况抛出 :class:`GeometryError`：
    * 少于 3 个点；坐标非有限（NaN/±inf）；
    * 存在重复顶点（相邻或整体重复）；
    * 自交或其它非法情形；多边形面积为 0。
    """
    if len(vertices) < 3:
        raise GeometryError("多边形至少需要 3 个顶点")

    cleaned: List[tuple[float, float]] = []
    for i, v in enumerate(vertices):
        try:
            x, y = float(v[0]), float(v[1])
        except (TypeError, ValueError, IndexError):
            raise GeometryError(f"第 {i + 1} 个顶点坐标无法解析为数值")
        if not (math.isfinite(x) and math.isfinite(y)):
            raise GeometryError(f"第 {i + 1} 个顶点坐标必须为有限数值（不允许 NaN 或无穷）")
        cleaned.append((x, y))

    # 自动去掉调用方重复给出的闭合点（v_n == v_0）
    if cleaned[0] == cleaned[-1]:
        cleaned = cleaned[:-1]
        if len(cleaned) < 3:
            raise GeometryError("闭合后有效顶点不足 3 个")

    seen = set()
    for p in cleaned:
        if p in seen:
            raise GeometryError(f"多边形存在重复顶点 {p!r}，区域必须为简单多边形")
        seen.add(p)

    try:
        polygon = Polygon(cleaned)
    except Exception as exc:  # pragma: no cover - shapely 一般不抛异常
        raise GeometryError(f"多边形构造失败：{exc}")

    if not polygon.is_valid:
        raise GeometryError(f"非法多边形：{explain_validity(polygon)}")
    if polygon.area <= 0.0:
        raise GeometryError("多边形面积为 0，不是有效的面")
    return polygon


def polygon_geojson(polygon: Polygon) -> dict:
    return mapping(polygon)


def polygon_from_geojson(geom: dict) -> Polygon:
    polygon = shape(geom)
    if not isinstance(polygon, Polygon):
        raise GeometryError("存储的几何不是 Polygon")
    if not polygon.is_valid or polygon.area <= 0.0:
        raise GeometryError("存储的多边形非法")
    return polygon


def _param_u(p0: tuple[float, float], p1: tuple[float, float], x: float, y: float) -> float:
    """交点在线段 p0->p1 上的归一化参数 u∈[0,1]（用于时刻插值）。"""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    if abs(dx) >= abs(dy):
        u = (x - p0[0]) / dx if abs(dx) > _EPS else 0.0
    else:
        u = (y - p0[1]) / dy if abs(dy) > _EPS else 0.0
    # 交点可能因浮点误差略微越界
    return min(1.0, max(0.0, u))


def _clip_half_open(lo: float, hi: float, wlo: float, whi: float) -> tuple[float, float] | None:
    """把闭区间 [lo, hi] 裁剪到左闭右开窗口 [wlo, whi)。"""
    a, b = max(lo, wlo), min(hi, whi)
    if a < b:
        return a, b
    if a == b and wlo <= a < whi:
        return a, a
    return None


def _collect_pieces(geom, p0, p1) -> Iterable[tuple[float, float]]:
    """把 shapely 交集几何展开为参数区间 (u0, u1)，u0<=u1。"""
    if geom.is_empty:
        return
    if geom.geom_type == "Point":
        u = _param_u(p0, p1, geom.x, geom.y)
        yield u, u
    elif geom.geom_type == "MultiPoint":
        for g in geom.geoms:
            yield from _collect_pieces(g, p0, p1)
    elif geom.geom_type == "LineString":
        coords = list(geom.coords)
        if len(coords) == 1:
            u = _param_u(p0, p1, coords[0][0], coords[0][1])
            yield u, u
        else:
            u0 = _param_u(p0, p1, coords[0][0], coords[0][1])
            u1 = _param_u(p0, p1, coords[-1][0], coords[-1][1])
            yield min(u0, u1), max(u0, u1)
    elif geom.geom_type in ("MultiLineString", "GeometryCollection", "MultiPolygon"):
        for g in geom.geoms:
            yield from _collect_pieces(g, p0, p1)
    # Polygon（理论上线段与多边形的交集不会产生面）忽略


def merge_intervals(
    intervals: Sequence[tuple[float, float]], eps: float = 1e-9
) -> List[tuple[float, float]]:
    """合并重叠或相接（首尾时刻相同）的区间。"""
    if not intervals:
        return []
    ordered = sorted((min(a, b), max(a, b)) for a, b in intervals)
    merged: List[tuple[float, float]] = [ordered[0]]
    for a, b in ordered[1:]:
        la, lb = merged[-1]
        if a <= lb + eps:
            merged[-1] = (la, max(lb, b))
        else:
            merged.append((a, b))
    return merged


def crossings_for_region(
    polygon: Polygon,
    times: Sequence[float],
    points: Sequence[tuple[float, float]],
    wlo: float,
    whi: float,
) -> List[tuple[float, float]]:
    """计算单个区域在生效窗口 [wlo, whi) 内的穿越时间段（epoch 秒）。

    交点时刻严格按相邻轨迹点线性插值计算，而非只检查采样点。
    返回已合并的左闭右开裁剪结果。
    """
    raw: List[tuple[float, float]] = []

    for i in range(len(times) - 1):
        t0, t1 = times[i], times[i + 1]
        p0, p1 = points[i], points[i + 1]

        # 整段位于窗口右侧（左端点>=窗口终点，左闭右开不算）；后续时刻更大，直接结束
        if t0 >= whi:
            break
        # 整段位于窗口左侧（含右端点恰好等于窗口起点：该端点不由本段计入）
        if t1 <= wlo:
            continue

        if p0 == p1:
            # 零长段：匀速运动退化为静止点
            if polygon.covers(Point(p0)):
                hit = _clip_half_open(t0, t1, wlo, whi)
                if hit:
                    raw.append(hit)
            continue

        segment = LineString([p0, p1])
        inter = polygon.intersection(segment)
        for u0, u1 in _collect_pieces(inter, p0, p1):
            lo = t0 + u0 * (t1 - t0)
            hi = t0 + u1 * (t1 - t0)
            hit = _clip_half_open(lo, hi, wlo, whi)
            if hit:
                raw.append(hit)

    return merge_intervals(raw)
