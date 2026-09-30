FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# 仅依赖预编译 wheel（shapely/asyncpg 均带 manylinux wheel），无需系统编译工具
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# 应用启动时自动建表初始化；HOST/PORT 默认 0.0.0.0:8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
