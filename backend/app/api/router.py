"""API 路由汇总。

版本策略见 `docs/API_CONTRACT.md` §2.1：
  - 业务接口带 `/api/v1` 前缀；
  - `/api/health` 不带版本前缀（运维探针，永远可用）。

实现说明：各模块的 Router 自带完整路径（如 `/api/v1/topology`），
在 `main.py` 里平铺挂载。这比 `APIRouter(prefix=...)` 嵌套更不易出错，
也让 grep 路径时一目了然。
"""

from fastapi import APIRouter

from app.api import dict as dict_module
from app.api import health, ingest, topology

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(ingest.router)
api_router.include_router(topology.router)
api_router.include_router(dict_module.router)


def build_v1_router() -> APIRouter:
    """v1 业务路由（占位）。

    端点已自带 `/api/v1` 前缀并通过 `api_router` 挂载；本函数保留为空壳
    以保持 `main.py` 的装配结构稳定，待事件/告警/诊断模块落地后可直接登记。
    """
    return APIRouter(prefix="/api/v1")
