"""`GET /api/v1/metrics` 的响应模型。

与其它端点一致用 `{data, meta}` 包裹（`API_CONTRACT.md` §2.2）。

**`unit` 一律为 `null`**：事件契约里 `metrics` 只有 `{名称: 数值}`，没有单位。
从名称猜单位（`_pct` → `%`、`_ms` → `ms`）看起来贴心，但遇到 `io_wait_pct`
（真是百分比）与 `value`（可能是任何东西）这类键时会给出**错误的**单位标注。
本项目一贯的做法是：拿不到就留空，让界面显示裸数值，而不是编一个看起来专业的单位。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MetricPointData(BaseModel):
    """一个数据点。"""

    #: 事件的发生时间（UTC ISO8601）
    at: str
    value: float


class MetricSeriesData(BaseModel):
    """一个 `(resourceId, name)` 序列。"""

    resourceId: str
    name: str
    #: 见模块文档：事件契约未携带单位，因此始终为 `null`。
    unit: str | None = None
    #: 按时间**升序**。曲线要从左到右画。
    points: list[MetricPointData] = Field(default_factory=list)
    #: 该序列是否因点数上限被裁剪（裁剪时保留**最新**的点）。
    truncated: bool = False


class MetricListData(BaseModel):
    """`/api/v1/metrics` 的 `data`。"""

    series: list[MetricSeriesData] = Field(default_factory=list)
    #: 实际扫描的事件数 —— 让调用方判断 `truncated` 是否触发了。
    eventsScanned: int = 0
    #: 扫描是否在上限处被截断。为 `true` 时曲线可能不完整，**必须让调用方看见**。
    truncated: bool = False
    #: 本次结果里出现的全部指标名，供前端渲染选择器。
    names: list[str] = Field(default_factory=list)
