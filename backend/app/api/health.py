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
from app.ingest.limits import metrics
from app.zsvirt import ZWatchProvider

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


def probe_gpu_provider(settings: object) -> ComponentStatus:
    """真实探活 GPU 数据渠道。

    **早先这里硬编码 `status="ok"`**，只把配置里的 mode 字符串回显出来。
    那意味着一个读不到任何 GPU 指标的渠道也会报 ok —— 而"读不到"正是
    最需要被暴露的缺口（`docs/DATA_MODEL.md` §4.2.1 的诚实性要求）。

    探活失败**不降级整体状态**：模拟渠道兜底是有意设计（赛题要求的降级模式），
    降级会让 `/api/health` 在整个演示期间都是 degraded，反而失去信号价值。
    但 `available` 与 `note` 必须如实说明当前用的是哪条路。
    """
    configured = getattr(settings, "gpu_provider", "auto")
    endpoint = getattr(settings, "zsvirt_endpoint", "")
    token = getattr(settings, "zsvirt_auth_token", "")

    detail: dict = {
        "mode": getattr(settings, "gpu_provider_mode", configured),
        "configuredMode": configured,
        "simulated": getattr(settings, "simulated_data_enabled", False),
    }

    if detail["simulated"]:
        detail["note"] = "模拟渠道（SIMULATED_DATA_ENABLED=true）：这些数字不是真实采集"
        return ComponentStatus(status="ok", detail=detail)

    if not endpoint:
        # 未配置是**预期状态**（等命题方资源），不是故障。
        detail["available"] = False
        detail["error"] = "ZSVIRT_ENDPOINT_NOT_CONFIGURED"
        return ComponentStatus(status="ok", detail=detail)

    provider = ZWatchProvider(
        endpoint=endpoint,
        auth_token=token,
        auth_style=getattr(settings, "zwatch_auth_style", "oauth"),
        access_key=getattr(settings, "zsvirt_access_key", ""),
        secret_key=getattr(settings, "zsvirt_secret_key", ""),
        timeout_sec=float(getattr(settings, "zwatch_timeout_sec", 8.0)),
        verify_tls=bool(getattr(settings, "zwatch_verify_tls", False)),
        cache_ttl_sec=int(getattr(settings, "zwatch_cache_ttl_sec", 10)),
    )
    available = provider.is_available()
    detail["available"] = available
    detail.update(provider.describe())
    if not available:
        detail["error"] = "ZWATCH_UNREACHABLE_OR_UNAUTHORIZED"
        detail["note"] = (
            "ZWatch 渠道不可达或未授权：GPU 指标将缺失，"
            "`/api/v1/workloads` 的 GPU 单元格会显示缺口说明而不是 0"
        )
    return ComponentStatus(status="ok", detail=detail)


@router.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()

    # 接入指标取自真实运行状态（`app.ingest.limits`），不再是占位符。
    ingest_detail: dict = metrics.snapshot()
    ingest_status = "ok"
    if metrics.clock_drift_exceeds_warn:
        # 时钟漂移超阈值会让跨层关联的时间窗失效 → 必须显式降级
        ingest_status = "degraded"
        ingest_detail["error"] = "CLOCK_DRIFT_EXCEEDS_THRESHOLD"
    if metrics.rejected_items:
        # 有拒收条目说明探针版本与契约不符，值得暴露但不必然降级
        ingest_detail["note"] = "存在被拒条目，请核对探针版本与 API_CONTRACT"

    components: dict[str, ComponentStatus] = {
        "database": probe_database(settings.database_url),
        "zsvirt": ComponentStatus(
            status="degraded" if not settings.zsvirt_endpoint else "ok",
            detail={
                "endpoint": settings.zsvirt_endpoint or None,
                "error": None if settings.zsvirt_endpoint else "ZSVIRT_ENDPOINT_NOT_CONFIGURED",
            },
        ),
        "ingest": ComponentStatus(status=ingest_status, detail=ingest_detail),
        "gpuProvider": probe_gpu_provider(settings),
    }

    return HealthResponse(
        status=_overall(components),
        components=components,
        version={"api": "v1", "build": __version__},
        generatedAt=datetime.now(UTC).isoformat(),
    )
