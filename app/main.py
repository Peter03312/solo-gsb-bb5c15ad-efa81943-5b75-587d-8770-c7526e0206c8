"""FastAPI 应用：临时限行区域登记/修订与轨迹核验。"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import database
from .geometry import (
    GeometryError,
    build_polygon,
    crossings_for_region,
    from_epoch,
    polygon_from_geojson,
    polygon_geojson,
    to_epoch,
)
from .schemas import (
    CrossingOut,
    MessageOut,
    RegionCrossingOut,
    RegionIn,
    RegionListOut,
    RegionOut,
    RegionUpdateIn,
    VerificationRequest,
    VerificationResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    yield
    await database.disconnect()


app = FastAPI(
    title="临时限行核验服务",
    version="1.0.0",
    description="面向场地安全员：登记平面限行区域并对匀速直线运动轨迹做整段连续核验。",
    lifespan=lifespan,
)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    # 兜底：几何层抛出的校验错误统一返回 422
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _vertices_from_row(row: dict) -> List[dict]:
    coords = row["polygon"]["coordinates"][0]
    # GeoJSON 闭合环首尾重复，去掉末点
    return [{"x": float(x), "y": float(y)} for x, y in coords[:-1]]


def _region_out(row: dict) -> RegionOut:
    return RegionOut(
        id=str(row["id"]),
        name=row["name"],
        vertices=_vertices_from_row(row),
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@app.get("/health", tags=["系统"], summary="健康检查")
async def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/regions",
    tags=["区域管理"],
    summary="登记限行区域",
    response_model=RegionOut,
    status_code=201,
    responses={422: {"model": MessageOut, "description": "非法多边形/非有限坐标/时段不递增"}},
)
async def create_region(body: RegionIn) -> RegionOut:
    polygon = build_polygon([(v.x, v.y) for v in body.vertices])
    row = await database.insert_region(
        region_id=str(uuid.uuid4()),
        name=body.name,
        polygon_geojson=polygon_geojson(polygon),
        valid_from=body.valid_from,
        valid_to=body.valid_to,
    )
    return _region_out(row)


@app.get("/regions", tags=["区域管理"], summary="列出全部区域及当前区域集合版本", response_model=RegionListOut)
async def list_regions() -> RegionListOut:
    version, rows = await database.list_regions_consistent()
    return RegionListOut(region_set_version=version, regions=[_region_out(r) for r in rows])


@app.get(
    "/regions/{region_id}",
    tags=["区域管理"],
    summary="查询单个区域",
    response_model=RegionOut,
    responses={404: {"model": MessageOut}, 422: {"model": MessageOut}},
)
async def get_region(region_id: uuid.UUID) -> RegionOut:
    row = await database.get_region(str(region_id))
    if row is None:
        raise HTTPException(status_code=404, detail="区域不存在")
    return _region_out(row)


@app.put(
    "/regions/{region_id}",
    tags=["区域管理"],
    summary="修订区域（全量替换，可选乐观锁）",
    response_model=RegionOut,
    responses={
        404: {"model": MessageOut},
        409: {"model": MessageOut, "description": "expected_revision 与当前版本不一致"},
        422: {"model": MessageOut},
    },
)
async def update_region(region_id: uuid.UUID, body: RegionUpdateIn) -> RegionOut:
    polygon = build_polygon([(v.x, v.y) for v in body.vertices])
    result = await database.update_region(
        region_id=str(region_id),
        name=body.name,
        polygon_geojson=polygon_geojson(polygon),
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        expected_revision=body.expected_revision,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="区域不存在")
    if result is False:
        raise HTTPException(status_code=409, detail="区域已被其他人修订，请重新读取后再提交（版本冲突）")
    return _region_out(result)


@app.delete(
    "/regions/{region_id}",
    tags=["区域管理"],
    summary="删除区域（可选乐观锁）",
    response_model=MessageOut,
    responses={404: {"model": MessageOut}, 409: {"model": MessageOut}, 422: {"model": MessageOut}},
)
async def delete_region(
    region_id: uuid.UUID, expected_revision: int | None = None
) -> MessageOut:
    result = await database.delete_region(str(region_id), expected_revision)
    if result is None:
        raise HTTPException(status_code=404, detail="区域不存在")
    if result is False:
        raise HTTPException(status_code=409, detail="区域已被其他人修订，请重新读取后再提交（版本冲突）")
    return MessageOut(detail="已删除")


@app.post(
    "/verify",
    tags=["轨迹核验"],
    summary="对整段连续轨迹核验限行穿越",
    response_model=VerificationResponse,
    responses={
        409: {"model": MessageOut, "description": "expected_version 与当前区域集合版本不一致"},
        422: {"model": MessageOut, "description": "非法轨迹/区域数据"},
    },
)
async def verify(body: VerificationRequest) -> VerificationResponse:
    times = [to_epoch(p.t) for p in body.points]
    points = [(p.x, p.y) for p in body.points]
    traj_lo, traj_hi = times[0], times[-1]

    # 单事务 REPEATABLE READ 快照：本次核验使用同一已提交版本的区域集合
    version, rows = await database.list_regions_consistent()
    if body.expected_version is not None and body.expected_version != version:
        raise HTTPException(
            status_code=409,
            detail=f"区域集合版本已变化：当前 {version}，期望 {body.expected_version}",
        )

    region_results: List[RegionCrossingOut] = []

    for row in rows:
        wlo = row["valid_from"].timestamp()
        whi = row["valid_to"].timestamp()
        # 左闭右开生效时段与轨迹时间范围无交集则跳过
        if whi <= traj_lo or wlo >= traj_hi:
            continue

        try:
            polygon = polygon_from_geojson(row["polygon"])
        except GeometryError as exc:
            raise HTTPException(status_code=422, detail=f"区域 {row['id']} 数据损坏：{exc}")

        intervals = crossings_for_region(polygon, times, points, wlo, whi)
        if not intervals:
            continue

        crossings = [
            CrossingOut(
                entry_time=from_epoch(a),
                exit_time=from_epoch(b),
                single_point=(a == b),
            )
            for a, b in intervals
        ]
        region_results.append(
            RegionCrossingOut(
                region_id=str(row["id"]),
                name=row["name"],
                revision=row["revision"],
                crossings=crossings,
            )
        )

    region_results.sort(key=lambda r: (r.name, r.region_id))

    return VerificationResponse(
        safe=len(region_results) == 0,
        region_set_version=version,
        considered_regions=len(rows),
        regions=region_results,
    )
