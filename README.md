# 临时限行核验服务

面向**场地安全员**的临时限行区域登记与轨迹核验 HTTP 服务。
安全员在**平面坐标系**中登记/修订简单多边形限行区域及其 **UTC 生效起止时刻**；
系统对一段连续轨迹（相邻点之间直线匀速运动）做整段核验，返回每个区域的穿越起止时刻及总体是否安全。

技术栈：**Python 3.12 · FastAPI · PostgreSQL · Shapely · asyncpg · Uvicorn**。

---

## 1. 一键启动（Docker Compose）

前置：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build
```

该命令会：

1. 启动 PostgreSQL 16（数据持久化到命名卷 `pgdata`）；
2. 构建并启动 API；
3. API 首次启动时**自动建表初始化**（幂等，无需手工迁移）。

启动后：

| 地址 | 说明 |
| --- | --- |
| <http://localhost:8000> | 服务根地址 |
| <http://localhost:8000/docs> | Swagger 交互式 API 文档（可直接发起调用） |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/health> | 健康检查 |

停止并清理：

```bash
docker compose down          # 停止（保留数据）
docker compose down -v       # 停止并删除数据库数据卷
```

### 端口与配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_PORT` | `8000` | 宿主机暴露的 API 端口（容器内固定 8000） |
| `POSTGRES_PORT_PUBLISH` | `5432` | 宿主机暴露的数据库端口 |
| `POSTGRES_USER` | `site_safety` | 数据库用户 |
| `POSTGRES_PASSWORD` | `site_safety` | 数据库密码 |
| `POSTGRES_DB` | `site_safety` | 数据库名 |
| `DATABASE_URL` | （由上面的变量拼接） | 完整连接串，设置后优先级最高 |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 容器内监听地址/端口 |
| `MERGE_EPS` | `1e-9` | 穿越段相接合并的浮点容差（秒） |

例如改用 18000 端口：

```bash
API_PORT=18000 docker compose up --build
# 服务地址变为 http://localhost:18000
```

---

## 2. 本地开发运行（不使用 Docker）

需要一个可连接的 PostgreSQL 14+。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL='postgresql://user:password@localhost:5432/site_safety'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试（单元测试不需要数据库；设置 `DATABASE_URL` 后会额外运行真实数据库集成测试）：

```bash
pip install pytest
pytest -q
DATABASE_URL='postgresql://user:password@localhost:5432/site_safety' pytest -q
```

---

## 3. 核验语义（重要）

- **轨迹**：至少 2 个点；时刻必须**严格递增**（重复或逆序报错）。相邻点之间视为**直线匀速运动**。
- **连续整段核验**：对每条相邻点构成的线段计算与多边形的交集，
  **交点时刻严格按匀速线性插值**得到（`t = t0 + u·(t1−t0)`），
  **不会只检查采样点**——即使所有采样点都在区域外，只要连线穿过区域即判定穿越。
- **边界接触算穿越**：贴边行驶、经过顶点都算。
- **单点接触**：轨迹仅在某一时刻接触区域（如擦过凸顶点）时，返回的 `entry_time == exit_time`，并置 `single_point=true`。
- **穿越段合并**：同一区域**相接或重叠**的穿越段合并为一个区间（跨相邻线段的首尾相接同样合并）。
- **不同区域分别列出**：结果按区域分别给出，互不合并。
- **生效时段左闭右开 `[valid_from, valid_to)`**：
  - 恰在 `valid_from` 的接触**算**穿越；
  - 恰在 `valid_to` 的接触**不算**；区间被裁剪到该窗口。
- **区域为简单多边形**：至少 3 个顶点、无重复顶点、无自交、面积大于 0；坐标必须为有限数（不允许 NaN/±∞）。
- **非法输入不保存**：非法多边形、非有限坐标、起止不递增的时段、重复/逆序轨迹时刻均返回 `422` 并说明原因，区域不会落库。
- **并发一致性**：一次核验在单个 `REPEATABLE READ` 只读事务快照中读取区域集合，
  因此整次核验始终使用**同一已提交版本**的区域集合，核验进行中并发提交的登记/修订不会混入。
  - 响应中的 `region_set_version` 是本次所用集合版本（内容哈希）；
  - 请求可带 `expected_version` 做乐观校验，版本不一致返回 `409`；
  - 修订/删除单个区域可带 `expected_revision` 做乐观锁，冲突返回 `409`。

---

## 4. HTTP API

所有时刻使用 ISO 8601 且**必须带时区**，建议直接用 `Z`（UTC），如 `2026-10-01T00:00:00Z`。

### `POST /regions` — 登记区域

```json
{
  "name": "1号仓库禁行区",
  "vertices": [
    {"x": 0, "y": 0},
    {"x": 10, "y": 0},
    {"x": 10, "y": 10},
    {"x": 0, "y": 10}
  ],
  "valid_from": "2026-10-01T00:00:00Z",
  "valid_to":   "2026-10-02T00:00:00Z"
}
```

顶点按顺序给出，首尾自动闭合（不要重复末点）。成功返回 `201` 与区域（含 `id`、`revision=1`）。

### `PUT /regions/{id}` — 修订区域（全量替换）

请求体同上，额外可选 `"expected_revision": 1`。成功后 `revision` 自增；版本冲突返回 `409`。

### `DELETE /regions/{id}?expected_revision=2` — 删除区域（可选乐观锁）

### `GET /regions` — 列出全部区域及 `region_set_version`
### `GET /regions/{id}` — 查询单个区域

### `POST /verify` — 轨迹核验

```json
{
  "expected_version": "可选，客户端上次 GET /regions 得到的版本",
  "points": [
    {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
    {"t": "2026-10-01T00:00:10Z", "x": 15, "y": 5}
  ]
}
```

响应：

```json
{
  "safe": false,
  "region_set_version": "a1b2c3d4e5f60718",
  "considered_regions": 1,
  "regions": [
    {
      "region_id": "…",
      "name": "1号仓库禁行区",
      "revision": 1,
      "crossings": [
        {"entry_time": "2026-10-01T00:00:02.500000Z",
         "exit_time":  "2026-10-01T00:00:07.500000Z",
         "single_point": false}
      ]
    }
  ]
}
```

- `safe=true` 当且仅当在任何区域的生效时段内都没有穿越；
- `regions` 只包含**存在穿越**的区域，按名称排序；每个区域的 `crossings` 已合并。

---

## 5. 调用示例

```bash
# curl 版（完整端到端脚本）
BASE_URL=http://localhost:8000 bash examples/usage_curl.sh

# Python 版（仅标准库）
python3 examples/usage_python.py http://localhost:8000
```

一段最常见的调用：

```bash
curl -sS -X POST http://localhost:8000/verify \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"t": "2026-10-01T00:00:00Z", "x": -5, "y": 5},
      {"t": "2026-10-01T00:00:10Z", "x": 15, "y": 5}
    ]
  }'
```

---

## 6. 项目结构

```
app/
  config.py     # 环境变量配置
  geometry.py   # Shapely 核验核心：多边形校验、交点插值、左闭右开裁剪、区间合并
  schemas.py    # Pydantic 请求/响应模型与输入校验
  database.py   # asyncpg 连接池、建表、乐观锁、快照读取
  main.py       # FastAPI 路由
examples/       # curl / Python 调用示例
tests/          # 几何/校验单元测试 + HTTP/数据库集成测试
Dockerfile
docker-compose.yml
```
