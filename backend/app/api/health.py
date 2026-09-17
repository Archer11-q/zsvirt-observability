"""GET /api/health —— 健康与降级状态（docs/API_CONTRACT.md §4.8）。

要点：
  - degraded 必须说明**具体哪个上游坏了**，不能只给整体状态；
  - gpuProvider.mode 必须暴露当前 GPU 数据渠道（诚实性要求）；
  - 探针时钟漂移超阈值时 ingest 应为 degraded；
  - database 必须**真实探测**，不得硬编码 ok（契约要求可诊断性）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__
from app.config import get_settings

router = APIRouter(tags=["health"])


class ComponentStatus(BaseModel):
    status: str
    detail: dict | None = None


class HealthResponse(BaseModel):
    status: str
    components: dict[str, ComponentStatus]
    version: dict[str, str]
    generatedAt: str


def _overall(components: dict[str, ComponentStatus]) -> str:
    states = {c.status for c in components.values()}
    if "down" in states:
        return "down"
    if "degraded" in states:
        return "degraded"
    return "ok"


def probe_database(url: str) -> ComponentStatus:
    """真实探测数据库连通性。

    走 SQLAlchemy engine —— 与业务代码使用同一套驱动与 URL 解析路径，
    避免"健康检查能过但业务连不上"这类假阳性。

    失败时返回 degraded + 具体错误，**不伪造 ok**。
    """
    if not url:
        return ComponentStatus(status="degraded", detail={"error": "DATABASE_URL_NOT_CONFIGURED"})
    try:
        import time

        from sqlalchemy import create_engine, text

        started = time.perf_counter()
        engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return ComponentStatus(status="ok", detail={"latencyMs": latency_ms})
    except Exception as exc:  # noqa: BLE001 - 健康检查必须吞掉异常并降级
        return ComponentStatus(
            status="degraded",
            detail={"error": type(exc).__name__, "message": str(exc)[:200]},
        )


@router.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()

    components: dict[str, ComponentStatus] = {
        "database": probe_database(settings.database_url),
        "zsvirt": ComponentStatus(
            status="degraded" if not settings.zsvirt_endpoint else "ok",
            detail={
                "endpoint": settings.zsvirt_endpoint or None,
                "error": None if settings.zsvirt_endpoint else "ZSVIRT_ENDPOINT_NOT_CONFIGURED",
            },
        ),
        # 接入层实现后填充真实计数；当前显式标注为未接入。
        "ingest": ComponentStatus(
            status="ok",
            detail={
                "unresolvedEvents": 0,
                "clockDriftMs": 0,
                "discarded": 0,
                "note": "计数尚未接入实际 ingest 模块",
            },
        ),
        "gpuProvider": ComponentStatus(
            status="ok",
            detail={
                "mode": "simulated" if settings.simulated_data_enabled else settings.gpu_provider,
                "simulated": settings.simulated_data_enabled,
            },
        ),
    }

    return HealthResponse(
        status=_overall(components),
        components=components,
        version={"api": "v1", "build": __version__},
        generatedAt=datetime.now(UTC).isoformat(),
    )
