"""事件查询的响应模型（`docs/API_CONTRACT.md` §4.4）。

**为什么返回原始 `raw` 与 `message`**：C 的承诺是"前端不原样渲染
`Event.raw` 与 `process.cmdline`，展示层再收敛一次"（`DECISIONS.md` §7.1）。
因此 B 必须提供数据，脱敏由入库前的管道保证；前端负责二次收敛。
两者分工明确，不重复做同一件事。

**`metrics` 与 `raw` 的区别**：`metrics` 是随事件携带的数值观测（规则与诊断
直接消费），`raw` 是原始上报载荷（仅用于追溯与排障）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EventData(BaseModel):
    """一条事件（`docs/DATA_MODEL.md` §5.1）。"""

    id: str
    #: 事件实际发生时间 —— 跨层关联以此为准
    occurredAt: str
    #: B 接收时间。与 occurredAt 的差值即网络延迟 + 时钟漂移
    receivedAt: str

    source: str
    resourceId: str
    type: str
    severity: str
    message: str | None = None

    metrics: dict[str, Any] = Field(default_factory=dict)
    #: 原始上报载荷（入库前已过脱敏管道）
    raw: dict[str, Any] = Field(default_factory=dict)
    traceId: str | None = None
    #: 产生本事件的批次，便于按批次追溯
    batchId: str | None = None


class EventListData(BaseModel):
    items: list[EventData]
    #: 会话时区无关的时间边界，前端可据此刻画"数据截止到哪"
    newestOccurredAt: str | None = None
    oldestOccurredAt: str | None = None
