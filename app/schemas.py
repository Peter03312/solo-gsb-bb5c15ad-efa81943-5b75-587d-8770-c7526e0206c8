"""Pydantic 请求/响应模型与输入校验。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .geometry import GeometryError, build_polygon, ensure_aware_utc


class Vertex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("坐标必须为有限数值（不允许 NaN 或无穷）")
        return v


class RegionIn(BaseModel):
    """登记区域。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, description="区域名称")
    vertices: List[Vertex] = Field(min_length=3, description="多边形顶点（首尾自动闭合，不要重复末点）")
    valid_from: datetime = Field(description="生效起始时刻（UTC，含）")
    valid_to: datetime = Field(description="生效结束时刻（UTC，不含）")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware_utc(v)

    @model_validator(mode="after")
    def _validate(self) -> "RegionIn":
        if self.valid_from >= self.valid_to:
            raise ValueError("生效起始时刻必须严格早于结束时刻")
        try:
            build_polygon([(v.x, v.y) for v in self.vertices])
        except GeometryError as exc:
            raise ValueError(str(exc))
        return self


class RegionUpdateIn(BaseModel):
    """修订区域（全量替换字段）。expected_revision 用于乐观并发控制。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    vertices: List[Vertex] = Field(min_length=3)
    valid_from: datetime
    valid_to: datetime
    expected_revision: Optional[int] = Field(
        default=None, ge=1, description="客户端上次读取到的版本号；提供时做乐观锁校验"
    )

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware_utc(v)

    @model_validator(mode="after")
    def _validate(self) -> "RegionUpdateIn":
        if self.valid_from >= self.valid_to:
            raise ValueError("生效起始时刻必须严格早于结束时刻")
        try:
            build_polygon([(v.x, v.y) for v in self.vertices])
        except GeometryError as exc:
            raise ValueError(str(exc))
        return self


class TrajectoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: datetime = Field(description="采样时刻（带时区，内部按 UTC 处理）")
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("坐标必须为有限数值（不允许 NaN 或无穷）")
        return v

    @field_validator("t")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware_utc(v)


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: List[TrajectoryPoint] = Field(min_length=2, description="至少两个轨迹点")
    expected_version: Optional[str] = Field(
        default=None,
        description="可选：客户端期望的区域集合版本（region_set_version）；不一致时返回 409",
    )

    @model_validator(mode="after")
    def _strictly_increasing(self) -> "VerificationRequest":
        prev = None
        for i, p in enumerate(self.points):
            if prev is not None and p.t <= prev:
                raise ValueError(f"轨迹时刻必须严格递增：第 {i} 个点重复或早于前一个点")
            prev = p.t
        return self


class CrossingOut(BaseModel):
    entry_time: datetime
    exit_time: datetime
    single_point: bool = Field(description="是否为单点接触（起止时刻相同）")


class RegionCrossingOut(BaseModel):
    region_id: str
    name: str
    revision: int
    crossings: List[CrossingOut]


class VerificationResponse(BaseModel):
    safe: bool = Field(description="总体是否安全（未在任何区域生效时段内穿越即为 true）")
    region_set_version: str
    considered_regions: int = Field(description="参与本次核验的已提交区域数量")
    regions: List[RegionCrossingOut] = Field(description="存在穿越的区域，分别列出（按区域名排序）")


class RegionOut(BaseModel):
    id: str
    name: str
    vertices: List[Vertex]
    valid_from: datetime
    valid_to: datetime
    revision: int
    created_at: datetime
    updated_at: datetime


class RegionListOut(BaseModel):
    region_set_version: str
    regions: List[RegionOut]


class MessageOut(BaseModel):
    detail: str
