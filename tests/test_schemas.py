"""Pydantic 输入模型校验测试。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas import RegionIn, VerificationRequest

UTC = timezone.utc


def _region_kwargs():
    return dict(
        name="禁行区A",
        vertices=[{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}, {"x": 0, "y": 10}],
        valid_from=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        valid_to=datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
    )


def test_region_ok():
    r = RegionIn(**_region_kwargs())
    assert len(r.vertices) == 4


def test_region_period_must_increase():
    kw = _region_kwargs()
    kw["valid_to"] = kw["valid_from"]
    with pytest.raises(ValidationError, match="严格早于"):
        RegionIn(**kw)
    kw = _region_kwargs()
    kw["valid_from"], kw["valid_to"] = kw["valid_to"], kw["valid_from"]
    with pytest.raises(ValidationError, match="严格早于"):
        RegionIn(**kw)


def test_region_naive_datetime_rejected():
    kw = _region_kwargs()
    kw["valid_from"] = datetime(2026, 10, 1, 0, 0)
    with pytest.raises(ValidationError, match="时区"):
        RegionIn(**kw)


def test_region_non_finite_coordinate_rejected():
    kw = _region_kwargs()
    kw["vertices"][1]["x"] = float("nan")
    with pytest.raises(ValidationError, match="有限"):
        RegionIn(**kw)


def test_region_self_intersect_rejected_and_not_saved():
    kw = _region_kwargs()
    kw["vertices"] = [
        {"x": 0, "y": 0},
        {"x": 10, "y": 10},
        {"x": 10, "y": 0},
        {"x": 0, "y": 10},
    ]
    with pytest.raises(ValidationError, match="非法多边形"):
        RegionIn(**kw)


def test_trajectory_needs_two_points():
    with pytest.raises(ValidationError):
        VerificationRequest(points=[{"t": "2026-10-01T00:00:00Z", "x": 0, "y": 0}])


def test_trajectory_times_strictly_increasing():
    base = [
        {"t": "2026-10-01T00:00:00Z", "x": 0, "y": 0},
        {"t": "2026-10-01T00:00:10Z", "x": 10, "y": 0},
    ]
    VerificationRequest(points=base)  # 合法

    dup = [
        {"t": "2026-10-01T00:00:00Z", "x": 0, "y": 0},
        {"t": "2026-10-01T00:00:00Z", "x": 10, "y": 0},
    ]
    with pytest.raises(ValidationError, match="严格递增"):
        VerificationRequest(points=dup)

    reverse = [
        {"t": "2026-10-01T00:00:10Z", "x": 0, "y": 0},
        {"t": "2026-10-01T00:00:00Z", "x": 10, "y": 0},
    ]
    with pytest.raises(ValidationError, match="严格递增"):
        VerificationRequest(points=reverse)


def test_trajectory_non_finite_coordinate_rejected():
    bad = [
        {"t": "2026-10-01T00:00:00Z", "x": 0, "y": float("inf")},
        {"t": "2026-10-01T00:00:10Z", "x": 10, "y": 0},
    ]
    with pytest.raises(ValidationError, match="有限"):
        VerificationRequest(points=bad)
