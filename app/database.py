"""PostgreSQL 访问层（asyncpg 连接池）。"""
from __future__ import annotations

import json
from typing import List, Optional

import asyncpg

from .config import DATABASE_URL, DB_POOL_MAX, DB_POOL_MIN

_pool: Optional[asyncpg.Pool] = None


async def connect() -> None:
    global _pool

    async def _init(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    import asyncio

    last_exc: Exception | None = None
    for attempt in range(1, 31):
        try:
            _pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=DB_POOL_MIN,
                max_size=DB_POOL_MAX,
                init=_init,
            )
            await init_schema()
            return
        except (OSError, asyncpg.PostgresError) as exc:
            last_exc = exc
            print(f"[startup] 等待数据库就绪（第 {attempt}/30 次）：{exc}")
            await asyncio.sleep(1.0)
    raise RuntimeError(f"无法连接数据库：{last_exc}")


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("数据库尚未初始化")
    return _pool


async def init_schema() -> None:
    """幂等建表（容器首次启动时由应用自身完成初始化）。"""
    async with pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regions (
                id          UUID PRIMARY KEY,
                name        TEXT NOT NULL,
                polygon     JSONB NOT NULL,
                valid_from  TIMESTAMPTZ NOT NULL,
                valid_to    TIMESTAMPTZ NOT NULL,
                revision    INTEGER NOT NULL DEFAULT 1,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT regions_period_order CHECK (valid_from < valid_to)
            );
            CREATE INDEX IF NOT EXISTS idx_regions_validity
                ON regions (valid_from, valid_to);
            """
        )


async def insert_region(
    region_id: str,
    name: str,
    polygon_geojson: dict,
    valid_from,
    valid_to,
) -> dict:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO regions (id, name, polygon, valid_from, valid_to)
            VALUES ($1::uuid, $2, $3, $4, $5)
            RETURNING id, name, polygon, valid_from, valid_to,
                      revision, created_at, updated_at
            """,
            region_id,
            name,
            polygon_geojson,
            valid_from,
            valid_to,
        )
        return dict(row)


async def update_region(
    region_id: str,
    name: str,
    polygon_geojson: dict,
    valid_from,
    valid_to,
    expected_revision: Optional[int],
) -> Optional[dict]:
    """全量修订。返回更新后的行；区域不存在返回 None；版本冲突返回 False。"""
    async with pool().acquire() as conn:
        if expected_revision is None:
            row = await conn.fetchrow(
                """
                UPDATE regions
                   SET name = $2, polygon = $3, valid_from = $4, valid_to = $5,
                       revision = revision + 1, updated_at = now()
                 WHERE id = $1::uuid
                RETURNING id, name, polygon, valid_from, valid_to,
                          revision, created_at, updated_at
                """,
                region_id,
                name,
                polygon_geojson,
                valid_from,
                valid_to,
            )
        else:
            row = await conn.fetchrow(
                """
                UPDATE regions
                   SET name = $2, polygon = $3, valid_from = $4, valid_to = $5,
                       revision = revision + 1, updated_at = now()
                 WHERE id = $1::uuid AND revision = $6
                RETURNING id, name, polygon, valid_from, valid_to,
                          revision, created_at, updated_at
                """,
                region_id,
                name,
                polygon_geojson,
                valid_from,
                valid_to,
                expected_revision,
            )
            if row is None:
                exists = await conn.fetchval(
                    "SELECT 1 FROM regions WHERE id = $1::uuid", region_id
                )
                return False if exists else None
        return dict(row) if row else None


async def delete_region(region_id: str, expected_revision: Optional[int]) -> object:
    async with pool().acquire() as conn:
        if expected_revision is None:
            deleted = await conn.fetchval(
                "DELETE FROM regions WHERE id = $1::uuid RETURNING id", region_id
            )
            return True if deleted is not None else None

        deleted = await conn.fetchval(
            "DELETE FROM regions WHERE id = $1::uuid AND revision = $2 RETURNING id",
            region_id,
            expected_revision,
        )
        if deleted is not None:
            return True
        exists = await conn.fetchval(
            "SELECT 1 FROM regions WHERE id = $1::uuid", region_id
        )
        return False if exists else None


async def get_region(region_id: str) -> Optional[dict]:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, polygon, valid_from, valid_to,
                   revision, created_at, updated_at
              FROM regions WHERE id = $1::uuid
            """,
            region_id,
        )
        return dict(row) if row else None


async def list_regions_consistent() -> tuple[str, List[dict]]:
    """在单个 REPEATABLE READ 只读事务中读取全部区域。

    一次核验始终使用同一已提交版本的区域集合（快照隔离），
    核验进行中并发提交的登记/修订不会混入本次结果。
    版本号为快照内容的哈希，供调用方做并发判断。
    """
    async with pool().acquire() as conn:
        tx = conn.transaction(
            isolation="repeatable_read", readonly=True, deferrable=True
        )
        await tx.start()
        try:
            rows = await conn.fetch(
                """
                SELECT id, name, polygon, valid_from, valid_to,
                       revision, created_at, updated_at
                  FROM regions
                 ORDER BY id
                """
            )
        finally:
            await tx.commit()
    data = [dict(r) for r in rows]
    return region_set_version_of(data), data


def region_set_version_of(rows: List[dict]) -> str:
    import hashlib

    h = hashlib.sha256()
    for r in rows:
        rid = r["id"]
        rid = str(rid) if not isinstance(rid, str) else rid
        h.update(f"{rid}:{r['revision']}\n".encode())
    return h.hexdigest()[:16]
