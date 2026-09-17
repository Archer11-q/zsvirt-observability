"""接入层 Schema（Pydantic）。

契约真源：`docs/API_CONTRACT.md` §3（A → B 上报契约，成员 A 已逐条确认）。

设计要点：

1. **部分接受**：整批不因单条非法而整体拒收。条目级错误进入 `rejected`，
   带 `index` 便于成员 A 定位探针 bug（A 的明确诉求）。
2. **类型名比对**：`event.type` 必须来自已冻结的 `EventType`（F-01）。
   非枚举值**不拒收**，而是标记为 `accepted_unknown_type` —— 拒收会让
   探针为了一个新事件类型而全面失败，代价过高；标注则保留数据同时让
   契约违约可见。
3. **`severity` 可省略**：省略时按 `EVENT_TYPE_DEFAULT_SEVERITY` 兜底。
   但成员 C 要求"每个 Event 必须携带 severity"，因此兜底后一定写入。
4. **不编造字段**：来源没给的一律留 `None`，不用默认值伪装（`DATA_MODEL.md` §1）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import (
    EVENT_TYPE_DEFAULT_SEVERITY,
    EventType,
    ResourceKind,
    Severity,
)

#: 批量上限（成员 A 的 Q4 已确认）
DEFAULT_MAX_EVENTS = 1000
DEFAULT_MAX_RESOURCES = 500
DEFAULT_MAX_PAYLOAD_BYTES = 1024 * 1024  # 1MB


class ResourcePayload(BaseModel):
    """探针上报的资源。

    注意：探针只给 `sourceId`（自己命名空间内的稳定标识），
    **全局 ID 由 B 拼装**（`app.normalize.ids.probe_resource_id`）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    sourceId: str = Field(min_length=1)
    name: str | None = None
    parentSourceId: str | None = None
    #: 业务状态。省略时 B 写 `unknown`，不猜。
    status: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, Any] = Field(default_factory=dict)
    firstSeenAt: datetime | None = None
    lastSeenAt: datetime | None = None

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in {m.value for m in ResourceKind}:
            raise ValueError(f"unknown resource kind: {value!r}")
        if value == ResourceKind.UNRESOLVED.value:
            raise ValueError("探针不得上报 `unresolved` —— 它是 B 内部的占位类型，不是真实资源")
        return value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {"running", "stopped", "error", "unknown"}
        if value not in allowed:
            raise ValueError(f"unknown status: {value!r}, allowed: {sorted(allowed)}")
        return value

    # 注意：**不在此处校验 `sourceId` 是否含冒号**。
    #
    # 若在这里拒绝，Pydantic 会让**整批**校验失败 → HTTP 422，整批被拒。
    # 但成员 A 的 Q1 要求部分接受：单个坏条目应只影响它自己，
    # 并在 `rejected` 里带 `index` 便于 A 定位探针 bug。
    #
    # 因此该形态校验放在 `app.ingest.service` 的条目级处理里
    # （表现为 `INVALID_RESOURCE_ID`），而不是 Schema 层。


class ResourceRef(BaseModel):
    """事件对资源的引用（用 `sourceId`，不含全局 ID）。"""

    model_config = ConfigDict(extra="forbid")

    kind: str
    sourceId: str = Field(min_length=1)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in {m.value for m in ResourceKind}:
            raise ValueError(f"unknown resource kind: {value!r}")
        return value


class EventPayload(BaseModel):
    """探针上报的事件。"""

    model_config = ConfigDict(extra="forbid")

    #: 事件实际发生时间（产生方给出）。跨层关联以此为准。
    occurredAt: datetime
    resourceRef: ResourceRef
    type: str = Field(min_length=1)
    severity: str | None = None
    message: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    #: 原始载荷。**入库前必过脱敏管道**（`app.normalize.sensitive`）。
    raw: dict[str, Any] = Field(default_factory=dict)
    traceId: str | None = None

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {m.value for m in Severity}
        if value not in allowed:
            raise ValueError(f"unknown severity: {value!r}, allowed: {sorted(allowed)}")
        return value

    def resolved_severity(self) -> str:
        """最终严重级别。

        显式给出则用给出的；否则按已冻结的默认表兜底（成员 C 要求
        每个 Event 都带 severity，不能为 None）。
        """
        if self.severity is not None:
            return self.severity
        return EVENT_TYPE_DEFAULT_SEVERITY.get(self.type, Severity.INFO.value)

    def is_known_type(self) -> bool:
        """事件类型是否在已冻结枚举中（F-01）。"""
        return self.type in {m.value for m in EventType}


class IngestBatch(BaseModel):
    """上报批次（`docs/API_CONTRACT.md` §3.2）。"""

    model_config = ConfigDict(extra="forbid")

    agentId: str = Field(min_length=1)
    #: **必填** —— 它是 Push 数据挂到 Pull 资源的唯一锚点（`ARCHITECTURE.md` §4.1）
    vmId: str = Field(min_length=1)
    agentVersion: str = Field(min_length=1)
    batchId: str = Field(min_length=1)
    sentAt: datetime

    resources: list[ResourcePayload] = Field(default_factory=list)
    events: list[EventPayload] = Field(default_factory=list)

    @field_validator("agentId")
    @classmethod
    def _agent_id_no_colon(cls, value: str) -> str:
        if ":" in value:
            raise ValueError(f"agentId must not contain ':' (got {value!r})")
        return value

    @field_validator("vmId")
    @classmethod
    def _vm_id_shape(cls, value: str) -> str:
        """`vmId` 必须是 B 能识别的全局 ID 形态 `vm:zsvirt:<uuid>`。

        校验形态而不是直接信任：探针若把 VM UUID 裸传，B 会把它当作
        未知资源挂占位节点，症状是"所有资源都挂在占位节点下"且难以定位。
        这里提前失败，错误信息直接说明期望格式。
        """
        parts = value.split(":")
        if len(parts) < 3 or parts[0] != ResourceKind.VM.value:
            raise ValueError(
                f"vmId 必须是 'vm:zsvirt:<uuid>' 形态（got {value!r}）。"
                "裸 UUID 会被当作未知资源，导致全部资源挂到占位节点下。"
            )
        return value


# ---------------------------------------------------------------- 响应模型


class RejectedItem(BaseModel):
    """被拒条目。必须带 `index`，便于探针定位问题条目（成员 A 的诉求）。"""

    index: int
    reason: str
    detail: str


class ResourceResult(BaseModel):
    """单个资源的接受结果。

    成员 A 的 Q1 诉求：响应须**逐条**返回每个 resource 的接受结果，
    A 据此维护「已确认资源」缓存，避免后续事件引用未确认资源。
    """

    index: int
    kind: str
    sourceId: str
    globalId: str | None = None
    result: str  # accepted | rejected


class AcceptedCounts(BaseModel):
    resources: int = 0
    events: int = 0


class IngestResult(BaseModel):
    """上报响应体（`docs/API_CONTRACT.md` §3.4）。"""

    batchId: str
    #: 重复批次标记。成员 A 的 Q2：重复批次返回**与首次一致**的结果，
    #: HTTP 仍为 200，**不得判定为脏数据**。
    duplicate: bool = False
    accepted: AcceptedCounts = Field(default_factory=AcceptedCounts)
    resources: list[ResourceResult] = Field(default_factory=list)
    rejected: list[RejectedItem] = Field(default_factory=list)
    #: 事件类型不在冻结枚举中的数量。不拒收（拒收代价过高），但要让违约可见。
    acceptedUnknownTypes: int = 0


class IngestResponse(BaseModel):
    """对外响应包裹（`API_CONTRACT.md` §2.2）。"""

    data: IngestResult
    meta: dict[str, Any] = Field(default_factory=dict)
