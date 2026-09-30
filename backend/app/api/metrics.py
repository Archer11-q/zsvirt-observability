"""`GET /api/v1/metrics` —— 事件内指标的时间序列（**最小可用版**）。

## 为什么需要它，以及为什么它不是 TSDB

赛题要求「统一组织指标、日志、链路、配置与告警证据」，而告警文案天然是时间序列
语义："延迟**飙升至** 800ms"、"显存**持续**上升"。在此之前，数值只作为事件的
附属字段存在**单点快照**（`value: 98`、`io_wait_pct: 35.1`、`exitCode: 137`），
既没有曲线，也没有 `/api/v1/metrics` 端点（返回 404）。值班时无法回答
"这是阶跃（配置变更）还是缓升（资源耗尽）"，只能切去别的 APM 工具。

**但它不引入 TSDB。** `ADR-0003` 明确不引入 Prometheus / 时序库。这里做的事
只是**把已经入库的数据换一种聚合方式读出来**：事件表里 `metrics` 是一个
`{名称: 数值}` 字典，本模块把它摊平成 `(resourceId, name, at, value)` 的点序列。
没有新存储、没有新写入路径、没有新依赖 —— 所以它不违反最小依赖纪律。

## 边界（如实标注，不假装是完整指标系统）

- **只覆盖"随事件携带的数值"**。聚合型指标（日均、P95）不在这里算 ——
  算出来的数字没有对应的观测证据，而本项目拒绝输出不可复算的结论。
- **点来自事件**，因此**事件被保留策略清理后曲线会变短**。这是诚实的代价：
  曲线与证据同生共死，不会出现"有曲线但没有证据"的状态。
- **两级上限都显式暴露**：`scanEvents` 限制扫描的事件数，`MAX_POINTS_PER_SERIES`
  限制单序列点数。任一触发都会在响应里置 `truncated` —— 而不是静默给一条看起来
  完整、其实被截断的曲线。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.common import build_meta, invalid_argument
from app.api.schemas.common import PageMeta
from app.api.schemas.metrics import MetricListData, MetricPointData, MetricSeriesData
from app.db import get_db
from app.events import repository as events_repo

router = APIRouter(tags=["metrics"])

DbSession = Annotated[Session, Depends(get_db)]

#: 单次扫描的事件数上限。比 `/events` 的 1000 小：这里每个事件可能展开出多个点。
DEFAULT_SCAN_LIMIT = 500
MAX_SCAN_LIMIT = 2000

#: 单个序列返回的点数上限。超出时取**最新的** N 个 ——
#: 曲线右侧（当前状态）比左侧（历史）更值得看，而 sparkline 也只有几百像素宽。
MAX_POINTS_PER_SERIES = 300


def _parse_instant(value: str) -> datetime | None:
    """解析 ISO8601 时间点。

    解析失败返回 `None`，由调用方给出 400 —— **不静默忽略**：忽略会让调用方
    拿到一条覆盖全时段的曲线，却以为它被过滤过。
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 契约要求带时区（ARCHITECTURE.md §4.3）。不带就按 UTC 解释 ——
        # 这是唯一的合理默认，且不改变绝对时刻的语义。
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@router.get("/api/v1/metrics")
def query_metrics(
    session: DbSession,
    resourceId: list[str] | None = Query(default=None, description="可重复传参"),
    name: list[str] | None = Query(default=None, description="指标名，可重复传参"),
    occurred_from: str | None = Query(default=None, alias="from"),
    occurred_to: str | None = Query(default=None, alias="to"),
    scanEvents: int = Query(default=DEFAULT_SCAN_LIMIT, ge=1, le=MAX_SCAN_LIMIT),
) -> Any:
    """按 `(resourceId, name)` 取指标点序列（`from` / `to` 为闭区间）。"""
    occurred_from_dt = _parse_instant(occurred_from) if occurred_from else None
    occurred_to_dt = _parse_instant(occurred_to) if occurred_to else None
    if occurred_from and occurred_from_dt is None:
        return invalid_argument(f"from 不是合法的 ISO8601 时间：{occurred_from!r}")
    if occurred_to and occurred_to_dt is None:
        return invalid_argument(f"to 不是合法的 ISO8601 时间：{occurred_to!r}")
    if occurred_from_dt and occurred_to_dt and occurred_from_dt > occurred_to_dt:
        return invalid_argument("时间窗非法：from 晚于 to")

    result = events_repo.query_metric_series(
        session,
        scan_limit=scanEvents,
        points_per_series=MAX_POINTS_PER_SERIES,
        occurred_from=occurred_from_dt,
        occurred_to=occurred_to_dt,
        resource_ids=resourceId,
        names=name,
    )

    series = [
        MetricSeriesData(
            resourceId=s.resource_id,
            name=s.name,
            unit=s.unit,
            points=[
                MetricPointData(at=p.at.astimezone(UTC).isoformat(), value=p.value)
                for p in s.points
            ],
            truncated=s.truncated,
        )
        for s in result.series
    ]

    # 扫描被截断时，序列级的 truncated 也必须置位：调用方看的是**曲线是否完整**，
    # 而不是"哪一级上限被触发"，后者是实现细节。
    if result.scan_truncated:
        for item in series:
            item.truncated = True

    data = MetricListData(
        series=series,
        eventsScanned=result.events_scanned,
        truncated=result.scan_truncated or any(s.truncated for s in series),
        names=sorted({s.name for s in series}),
    )

    return {
        "data": data.model_dump(mode="json"),
        "meta": build_meta(
            page=PageMeta(limit=scanEvents, nextCursor=None, count=len(series))
        ).model_dump(mode="json"),
    }
