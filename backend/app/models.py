"""ORM 模型（SQLAlchemy 2.0 声明式）。

字段定义的真源是 `docs/DATA_MODEL.md`（已冻结），本文件是它的持久化实现。

**命名约定**：数据库列与 Python 属性用 `snake_case`；对外的 `camelCase`
（`occurredAt` / `resourceId` …）由 API 层序列化时转换。契约约束的是
**JSON 形态**，而契约文档中的 `resourceId` 是 JSON 字段名而非数据库列名 ——
两者不必相同。`tests/test_models.py` 有断言保证序列化结果符合契约。

**已冻结语义的落地**（每条都有单测）：

| 语义 | 位置 | 依据 |
|---|---|---|
| `status` 与 `observability` 是两个独立字段，禁止混用 | `Resource` | D-032 |
| `Alert.evidenceEventIds` **必填** —— 没有证据的告警不允许存在 | `Alert` | `DATA_MODEL.md` §5.2 |
| `Event` 只追加不修改 | `Event`（无 update 路径） | §5.1 |
| 资源不物理删除，只标 `gone` | `Resource`（无 delete 路径） | §7 |
| `Diagnosis` 一次性生成、不被改写 | `Diagnosis` | §5.3 |

**索引策略**：事件表是唯一持续增长的表，按 `(resource_id, occurred_at)`
与 `(occurred_at)` 建索引以支撑按资源/时间窗检索；`type` 与 `severity`
也建索引以支撑过滤。游标分页依赖 `(occurred_at, id)` 的稳定排序。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.enums import (
    AlertState,
    Observability,
    ResourceKind,
    ResourceStatus,
)


def utcnow() -> datetime:
    """带时区的当前时间。全库统一 UTC，禁止 naive datetime。"""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """声明式基类。"""


# JSON 类型：生产用 PostgreSQL JSONB，测试可用 SQLite 的 JSON。
# 用 `with_variant` 让同一份模型在两种数据库上都能建表。
JSONType = JSONB().with_variant(Text(), "sqlite")


# ---------------------------------------------------------------- 资源


class Resource(Base):
    """资源节点（`docs/DATA_MODEL.md` §4）。

    **`status` 与 `observability` 必须分开**（D-032）：

    - `status` 是**业务状态**，由来源系统（ZSvirt 或探针）给出，B 不得推测
    - `observability` 是 **B 的观测状态**，由 B 根据 `last_seen_at` 判定

    举例：VM 被 ZSvirt 报为 `status=running`，但探针断线 20 分钟 →
    `status=running` 且 `observability=stale`。**两者同时为真是正常的**，
    合并成一个字段会让前端无法分别渲染"运行状态"与"数据新鲜度"。
    """

    __tablename__ = "resource"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    name: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("resource.id", ondelete="SET NULL"), index=True
    )

    #: 业务状态 —— 来源系统给出
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ResourceStatus.UNKNOWN.value, index=True
    )
    #: B 的观测状态 —— 与 status 独立，禁止混用
    observability: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Observability.ACTIVE.value, index=True
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    #: 占位节点标记：事件引用了尚未上报的资源时置 True（成员 A 的 Q1 兜底）
    is_placeholder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({', '.join(repr(m.value) for m in ResourceKind)})",
            name="ck_resource_kind",
        ),
        CheckConstraint(
            "status IN ('running','stopped','error','unknown')",
            name="ck_resource_status",
        ),
        CheckConstraint(
            "observability IN ('active','stale','gone')",
            name="ck_resource_observability",
        ),
        Index("ix_resource_kind_observability", "kind", "observability"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<Resource {self.id} kind={self.kind} status={self.status} obs={self.observability}>"
        )


class ResourceEdge(Base):
    """资源关系（邻接表，`docs/DATA_MODEL.md` §6）。

    与 `Resource.parent_id` **冗余但有意为之**：
    `parent_id` 便于单点查父，`resource_edge` 便于整图遍历（避免 N+1）。
    两者由 `app.graph.repository` 在同一事务内维护，不允许只改一处。
    """

    __tablename__ = "resource_edge"

    parent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("resource.id", ondelete="CASCADE"), primary_key=True
    )
    child_id: Mapped[str] = mapped_column(
        Text, ForeignKey("resource.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(String(32), nullable=False, default="hosts")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        CheckConstraint("parent_id <> child_id", name="ck_edge_no_self_loop"),
        Index("ix_edge_child", "child_id"),
    )


# ---------------------------------------------------------------- 事件


class Event(Base):
    """不可变观测记录（`docs/DATA_MODEL.md` §5.1）。

    **只追加、不修改** —— 没有 update 路径。这是可追溯性的基础：
    诊断证据指向具体事件，若事件可被改写，证据链就失去意义。

    `id` 用 `evt_` + ULID：ULID 时间单调有序，因此按 id 排序近似按时间排序，
    可用于稳定的游标分页（C 要求"游标分页必须稳定"）。
    """

    __tablename__ = "event"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: 事件实际发生时间（产生方给出）。跨层关联以此为准。
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: B 接收时间。与 occurredAt 的差值用于监测网络延迟与时钟漂移。
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    source: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[str] = mapped_column(
        Text, ForeignKey("resource.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text)

    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    #: 原始上报载荷。**入库前必过脱敏管道**（`app.normalize.sensitive`）。
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)

    #: 幂等：产生本事件的批次。重复批次不得重复写入。
    batch_id: Mapped[str | None] = mapped_column(String(64), index=True)

    __table_args__ = (
        CheckConstraint("source IN ('zsvirt','probe','derived')", name="ck_event_source"),
        CheckConstraint(
            "severity IN ('info','warning','error','critical')",
            name="ck_event_severity",
        ),
        # 按资源 + 时间窗检索（诊断汇聚证据的主力查询）
        Index("ix_event_resource_occurred", "resource_id", "occurred_at"),
        # 游标分页的稳定排序
        Index("ix_event_occurred_id", "occurred_at", "id"),
        # 按类型 + 时间窗（规则求值）
        Index("ix_event_type_occurred", "type", "occurred_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Event {self.id} {self.type} severity={self.severity}>"


# ---------------------------------------------------------------- 告警


class Alert(Base):
    """有状态的判断（`docs/DATA_MODEL.md` §5.2）。

    **红线：`evidence_event_ids` 必填** —— 没有证据的告警不允许存在。
    这是"可解释"要求的下限：运维看到一条告警，必须能追到触发它的事件。
    """

    __tablename__ = "alert"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: 触发它的规则 ID —— 保证告警可解释（为什么报）
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_id: Mapped[str] = mapped_column(
        Text, ForeignKey("resource.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AlertState.FIRING.value, index=True
    )

    first_fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: 聚合计数（去重后的触发次数）
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: **必填**：指向触发本告警的证据事件
    evidence_event_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    #: 聚合/去重键。同键的重复触发累加 count 而不是新建告警。
    aggregation_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)

    diagnosis_id: Mapped[str | None] = mapped_column(Text, index=True)

    title: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    __table_args__ = (
        CheckConstraint(
            "severity IN ('info','warning','error','critical')",
            name="ck_alert_severity",
        ),
        CheckConstraint(
            "state IN ('firing','acked','resolved','silenced')",
            name="ck_alert_state",
        ),
        CheckConstraint("count >= 1", name="ck_alert_count_positive"),
        # 证据必填的红线在数据库层强制：数组不得为空
        CheckConstraint(
            "coalesce(array_length(evidence_event_ids, 1), 0) >= 1",
            name="ck_alert_evidence_required",
        ),
        # 同一聚合键 + 资源 + 规则在 firing/silenced 态下只应有一条活动告警
        Index(
            "ix_alert_aggregation_active",
            "aggregation_key",
            "state",
            unique=False,
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Alert {self.id} {self.rule_id} state={self.state} count={self.count}>"


# ---------------------------------------------------------------- 诊断


class Diagnosis(Base):
    """根因结论（`docs/DATA_MODEL.md` §5.3）。

    `confidence` 是**规则加权评分**，不是统计学概率；必须能由
    `confidence_breakdown` 复算（C 确认 D-036），且必须携带
    `rule_set_version` 才能复现结论。
    """

    __tablename__ = "diagnosis"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    #: 触发方式：{"alertId": ...} 或 {"window": {...}, "anchorResourceId": ...}
    trigger: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    root_cause: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 规则加权分，0.0–1.0。非统计学概率。
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    #: 各规则的得分贡献 —— 保证 confidence 可复算
    confidence_breakdown: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, nullable=False, default=list
    )

    #: 证据直接支持 + 其下游后代（与 potentially 分开，C 要求 D-075）
    affected_resources: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    #: 结构相关但无证据 —— **不得混入 affected_resources**
    potentially_affected: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    #: 证据链上但无证据的过渡层
    on_chain: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    #: 证据列表。**不得为空**，否则 root_cause 只能是 UNKNOWN。
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    recommendation: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, nullable=False, default=list
    )

    #: 产出该结论的规则集版本 —— 结论可复现的前提
    rule_set_version: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    __table_args__ = (
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_diagnosis_confidence"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Diagnosis {self.id} {self.root_cause} confidence={self.confidence}>"


# ---------------------------------------------------------------- 接入幂等


class IngestBatch(Base):
    """上报批次（幂等去重，成员 A 的 Q2 已确认语义）。

    A 采用**至少一次**投递并允许重复；B 按 `batch_id` 幂等去重，
    且**重复批次必须返回与首次一致的结果**（不是判为脏数据）。

    因此本表不只记录"见过"，还保存首次的响应摘要 `result`，
    重复批次直接回放它。
    """

    __tablename__ = "ingest_batch"

    batch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    vm_id: Mapped[str | None] = mapped_column(Text, index=True)

    agent_version: Mapped[str | None] = mapped_column(String(32))
    #: 探针给出的发送时间 —— 用于计算时钟漂移（received_at − sent_at）
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    accepted_resources: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: 首次响应摘要，供重复批次回放（保证"返回与首次一致的结果"）
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    __table_args__ = (Index("ix_batch_received", "received_at"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<IngestBatch {self.batch_id} agent={self.agent_id}>"


__all__ = [
    "Alert",
    "Base",
    "Diagnosis",
    "Event",
    "IngestBatch",
    "Resource",
    "ResourceEdge",
    "utcnow",
]
