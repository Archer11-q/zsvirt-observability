"""诊断与工作负载的响应模型。

契约：`docs/API_CONTRACT.md` §4.3（workloads）、§4.6/§4.7（diagnoses）。

**三个必须同时满足的约束**（都来自 C 的明确要求，D-075 / D-036）：

1. `affectedResources` 与 `potentiallyAffected` 是**两个独立字段**，
   前端分开渲染（受影响 = 有证据支持；潜在 = 结构相关）
2. `confidence` 必须能由 `confidenceBreakdown` **逐项复算**
3. 必须携带 `ruleSetVersion`，否则结论不可复现

**证据不得为空**：无证据时 `rootCause` 只能是 `UNKNOWN`（`DATA_MODEL.md` §5.3 红线）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.api.schemas.events import EventData


class ConfidenceContribution(BaseModel):
    """单条规则对置信分的贡献。

    暴露 `observed` 是关键：运维要能看到"为什么这条规则命中了"，
    而不是只拿到一个数字。这是可解释性的最小单位。
    """

    ruleId: str
    contribution: float
    observed: str


class DiagnosisEvidence(BaseModel):
    """一条诊断证据。"""

    #: metric | event | alert | log
    type: str
    name: str
    resourceId: str | None = None
    value: str | None = None
    count: int | None = None
    at: str | None = None
    #: 指向具体事件（可点击跳转）
    eventIds: list[str] = Field(default_factory=list)
    #: 证据来源：zsvirt | zwatch | probe | simulated | derived。
    #: **模拟数据必须可识别**（`DATA_MODEL.md` §4.2.1 的诚实性要求）
    source: str | None = None


class DiagnosisRecommendation(BaseModel):
    code: str
    #: 中文文案。前端不硬编码映射（C 的 Q14）
    text: str


class DiagnosisData(BaseModel):
    """一次诊断结论（`docs/DATA_MODEL.md` §5.3）。"""

    id: str
    createdAt: str

    #: 触发方式：{"alertId": ...} 或 {"window": {...}, "anchorResourceId": ...}
    trigger: dict[str, Any]

    rootCause: str
    #: 规则加权评分（非统计学概率）。必须可由 confidenceBreakdown 复算。
    confidence: float
    confidenceBreakdown: list[ConfidenceContribution] = Field(default_factory=list)

    #: 受影响资源（有证据支持 + 其下游）
    affectedResources: list[str] = Field(default_factory=list)
    #: 潜在受影响（结构相关但无证据）—— **独立字段，不得混入上面那个**
    potentiallyAffected: list[str] = Field(default_factory=list)
    #: 证据链上但无证据的过渡层
    onChain: list[str] = Field(default_factory=list)

    evidence: list[DiagnosisEvidence] = Field(default_factory=list)
    recommendation: list[DiagnosisRecommendation] = Field(default_factory=list)

    #: 产出该结论的规则集版本 —— 结论可复现的前提
    ruleSetVersion: str
    notes: list[str] = Field(default_factory=list)

    #: 诊断耗时（毫秒）。便于前端提示"诊断较快/较慢"，也便于性能排查
    durationMs: int | None = None

    #: `?includeEvidence=true` 时**内联**证据指向的事件原文。
    #: 默认不返回：证据可能指向几十条事件，把它们全部展开会把详情页撑成
    #: 一次大查询；但演示与排障时"点开就看到原始事件"比再发 N 个请求好用。
    evidenceEvents: list[EventData] = Field(default_factory=list)


class DiagnosisListData(BaseModel):
    items: list[DiagnosisData]
    #: 按根因码的分布计数，前端可做"根因 Top N"
    rootCauseCounts: dict[str, int] = Field(default_factory=dict)


class TriggerDiagnosisRequest(BaseModel):
    """手动触发诊断的请求体（C 的 Q13）。

    两种触发方式二选一：

    - `{alertId}`：从告警出发（演示"一键诊断"最自然）
    - `{window, anchorResourceId}`：从资源 + 时间窗出发（联调时构造）
    """

    alertId: str | None = None
    window: dict[str, str] | None = None
    anchorResourceId: str | None = None
    #: 诊断后是否回写到关联告警的 diagnosisId
    linkToAlert: bool = True


# ---------------------------------------------------------------- 工作负载


class WorkloadResourceUsage(BaseModel):
    """工作负载的底层资源占用。

    **字段可为 `null` 且附 `note`** —— 当 GPU 指标渠道不可用时（无 ZWatch、
    探针未上报、模拟模式未开），B **不编造数字**（`DATA_MODEL.md` §1 的
    "不编造"原则）。前端据此显示"指标不可用"而不是 0。
    """

    gpuMemoryUsedBytes: int | None = None
    gpuMemoryTotalBytes: int | None = None
    gpuUtilizationPct: float | None = None
    note: str | None = None


class WorkloadData(BaseModel):
    """一个 AI 服务工作负载（C 的 Q10 定义的聚合视图）。

    C 的定义：**`ai_service` 层级的业务聚合视图**，不是容器、不是 Agent/Task。
    "负载总览"面向"哪个 AI 服务在跑、健不健康"；容器属基础设施细节（看拓扑），
    Agent/Task 属更细粒度（看服务详情）。
    """

    id: str
    name: str | None = None

    #: 业务状态（来源系统给出）
    status: str
    #: B 的观测状态（与 status 独立，D-032）
    observability: str
    staleness: str | None = None

    #: framework / modelName / endpoint / concurrency / qps 等
    attributes: dict[str, Any] = Field(default_factory=dict)

    resourceUsage: WorkloadResourceUsage = Field(default_factory=WorkloadResourceUsage)

    #: 统计窗口内的计数（与 `since` 参数对应）
    eventCount: int = 0
    alertCount: int = 0
    firingAlertCount: int = 0

    #: 关联诊断（取该服务最近一次）
    diagnosisId: str | None = None
    rootCause: str | None = None

    #: 底层资源链的 ID（供前端点进拓扑下钻）
    parentId: str | None = None


class WorkloadListData(BaseModel):
    items: list[WorkloadData]
    #: 统计窗口，前端展示"近 N 小时"
    windowFrom: str
    windowTo: str
    #: GPU 指标渠道。**必须暴露** —— 前端要能区分"真实指标"与"无指标"
    gpuProviderMode: str
