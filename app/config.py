"""配置：全部可通过环境变量覆盖，提供本地开发默认值。"""
from __future__ import annotations

import os


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "site_safety")
    password = os.getenv("POSTGRES_PASSWORD", "site_safety")
    host = os.getenv("POSTGRES_HOST", "db")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "site_safety")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL: str = _database_url()
DB_POOL_MIN: int = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX: int = int(os.getenv("DB_POOL_MAX", "10"))

# 服务监听地址/端口
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

# 数值合并容差（秒）：同一区域相接（首尾端点相同）的穿越段一律合并，
# 此容差用于消除浮点插值误差导致的微小间隙。
MERGE_EPS: float = float(os.getenv("MERGE_EPS", "1e-9"))
