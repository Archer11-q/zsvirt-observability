"""告警查询与操作的响应模型（`docs/API_CONTRACT.md` §4.5）。

**`evidenceEventIds` 为什么必须暴露**：`DATA_MODEL.md` §5.2 的红线是
"没有证据的告警不允许存在"。前端要能一键跳到触发告警的那几条事件，
否则"可解释"就只是文档里的承诺。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AlertData(BaseModel):
    """一条告警（`docs/DATA_MODEL.md` §5.2）。"""

    id: str
    #: 触发它的规则 ID —— 保证告警可解释（为什么报）
    ruleId: str
    resourceId: str

    severity: str
    #: firing | acked | resolved | silenced
    state: str

    firstFiredAt: str
    lastFiredAt: str
    resolvedAt: str | None = None

    #: 去重后的触发次数
    count: int
    #: 聚合/去重键。同键重复触发累加 count，不新建告警
    aggregationKey: str

    #: **必填**：指向触发本告警的证据事件
    evidenceEventIds: list[str] = Field(default_factory=list)

    #: 关联诊断（若有）。C 可据此跳转诊断详情
    diagnosisId: str | None = None

    title: str | None = None
    labels: dict[str, Any] = Field(default_factory=dict)


class AlertListData(BaseModel):
    items: list[AlertData]
    #: 各状态计数。前端顶部状态条/筛选器需要，避免多次请求
    stateCounts: dict[str, int] = Field(default_factory=dict)
    #: 各严重级别中 firing/acked 的数量（C 的图例与角标需要）
    activeSeverityCounts: dict[str, int] = Field(default_factory=dict)


class AlertActionRequest(BaseModel):
    """告警状态操作请求。

    `silenceSeconds` 只在 `action=silence` 时有意义。用秒数而不是绝对时间：
    前端不需要与 B 对时钟，避免时区与漂移问题。
    """

    action: str  # ack | resolve | silence | unsilence
    silenceSeconds: int | None = Field(default=None, ge=1, le=7 * 24 * 3600)
    comment: str | None = None


class AlertActionData(BaseModel):
    """单个告警的操作结果。"""

    alert: AlertData
    #: 实际是否发生变化。重复 ack 返回 `changed=False` 而不是报错 ——
    #: 运维重复点击不应产生错误弹窗
    changed: bool
    message: str


class BulkAlertActionData(BaseModel):
    """批量操作结果。

    批量接口必须逐条报告结果：一次 20 条里失败 1 条时，运维需要知道是哪条，
    而不是一句"部分失败"。
    """

    requested: int
    changed: int
    unchanged: int
    failed: int
    results: list[dict[str, Any]] = Field(default_factory=list)
