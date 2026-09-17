"""API 路由汇总。

版本策略见 docs/API_CONTRACT.md §2.1：
  - 业务接口带 /api/v1 前缀；
  - /api/health 不带版本前缀（运维探针，永远可用）。
"""

from fastapi import APIRouter

from app.api import health

api_router = APIRouter()
api_router.include_router(health.router)


def build_v1_router() -> APIRouter:
    """v1 业务路由。各模块实现后在此挂载。"""
    v1 = APIRouter(prefix="/api/v1")
    # 待实现（契约已冻结）：
    #   POST /ingest/batch     docs/API_CONTRACT.md §3
    #   GET  /topology         §4.2
    #   GET  /workloads        §4.3
    #   GET  /events           §4.4
    #   GET  /alerts           §4.5
    #   GET  /diagnoses        §4.6
    #   POST /diagnoses        §4.6
    #   GET  /dict             §4.6.1
    #   GET  /diagnosis/{id}   §4.7
    return v1
