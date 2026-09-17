"""API 路由汇总。

版本策略见 `docs/API_CONTRACT.md` §2.1：
  - 业务接口带 `/api/v1` 前缀；
  - `/api/health` 不带版本前缀（运维探针，永远可用）。

实现说明：各模块的 Router 自带完整路径（如 `/api/v1/topology`），
在 `main.py` 里平铺挂载。这比 `APIRouter(prefix=...)` 嵌套更不易出错，
也让 grep 路径时一目了然。

**路由声明顺序有语义**：`/api/v1/alerts/actions`（批量）必须注册在
`/api/v1/alerts/{alert_id}`（详情）**之前**，否则 FastAPI 会把 `actions`
当成 `alert_id` 匹配掉。同理 `/api/v1/events/{event_id}` 与其子路径。
"""

from fastapi import APIRouter

from app.api import alerts as alerts_module
from app.api import diagnoses as diagnoses_module
from app.api import dict as dict_module
from app.api import events as events_module
from app.api import health, ingest, topology
from app.api import workloads as workloads_module

api_router = APIRouter()
api_router.include_router(health.router)

# alerts：批量动作先于详情，避免 "actions" 被当作 alert_id
api_router.include_router(alerts_module.router)
api_router.include_router(events_module.router)
# diagnoses：详情 `/diagnosis/{id}` 与集合 `/diagnoses` 路径不同形，
# 不存在 shadowing 风险；先详情后集合只是保持"详情的依赖更基础"的阅读顺序
api_router.include_router(diagnoses_module.router)
api_router.include_router(ingest.router)
api_router.include_router(topology.router)
api_router.include_router(workloads_module.router)
api_router.include_router(dict_module.router)


def build_v1_router() -> APIRouter:
    """v1 业务路由的**占位挂载点（刻意为空）**。

    **不要往这里加路由**。所有业务端点都自带完整路径（`/api/v1/...`）并通过
    上面的 `api_router` 挂载；本函数带 `prefix="/api/v1"`，一旦返回真实路由，
    `main.py` 会把它们再挂一次，路径变成 `/api/v1/api/v1/...` —— 真实路径
    反而 404。这不是假设：本函数曾短暂返回过 `api_router`，正是这个后果。

    保留它的唯一原因是让 `main.py` 的装配结构（两个 include_router）保持
    稳定，将来若确实需要拆分版本前缀，改动点集中在这里。

    **契约测试请断言 `app.routes`**，不要依赖本函数。
    """
    return APIRouter(prefix="/api/v1")
