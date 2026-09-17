"""诊断引擎的数据结构。

这些类型是引擎的**输入输出契约**，刻意不依赖 ORM / HTTP，
以便脱离数据库与网络做单元测试（docs/backend/DIAGNOSIS_DESIGN.md §2.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EvidenceKind(StrEnum):
    METRIC = "metric"
    EVENT = "event"
    ALERT = "alert"
    LOG = "log"


class EvidenceSource(StrEnum):
    """证据来源。用于诚实性要求：模拟数据必须可识别（见 DATA_MODEL.md §4.2.1）。"""

    ZSVIRT = "zsvirt"
    ZWATCH = "zwatch"
    PROBE = "probe"
    SIMULATED = "simulated"
    DERIVED = "derived"


@dataclass(frozen=True)
class TimeWindow:
    """时间窗。根因必须早于或等于症状，反向关联作为反证扣分。"""

    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end

    def with_tolerance(self, seconds: int) -> TimeWindow:
        from datetime import timedelta

        return TimeWindow(
            self.start - timedelta(seconds=seconds), self.end + timedelta(seconds=seconds)
        )


@dataclass(frozen=True)
class Evidence:
    """一条证据。所有根因结论都必须能指回具体证据。"""

    kind: EvidenceKind
    name: str
    at: datetime
    resource_id: str
    source: EvidenceSource = EvidenceSource.PROBE
    value: str | None = None
    count: int | None = None
    event_ids: tuple[str, ...] = ()

    @property
    def is_simulated(self) -> bool:
        return self.source is EvidenceSource.SIMULATED


@dataclass(frozen=True)
class Resource:
    """引擎视角的资源（不是 ORM 实体）。

    status 与 observability 是两个独立字段：
      status        业务状态，由来源系统给出
      observability B 的观测状态
    见 docs/DATA_MODEL.md §4.1。
    """

    id: str
    kind: str
    status: str = "unknown"
    observability: str = "active"
    parent_id: str | None = None


@dataclass(frozen=True)
class Edge:
    parent_id: str
    child_id: str
    relation: str = "hosts"


@dataclass(frozen=True)
class RuleHit:
    """一条规则的求值结果。命中与未命中都要留痕，保证可复算。"""

    rule_id: str
    root_cause: str
    contribution: float
    observed: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    """声明式规则。

    规则是**数据**，不是代码分支：新故障场景 = 新增规则数据，
    不新增代码路径（docs/backend/DIAGNOSIS_DESIGN.md §7.2）。
    """

    id: str
    root_cause: str
    contribution: float
    evidence_kind: EvidenceKind
    match_name: str
    resource_kinds: tuple[str, ...] = ()
    comparator: str | None = None
    threshold: float | None = None
    min_count: int | None = None
    requires_source: EvidenceSource | None = None
    contradicts: str | None = None
    observed_template: str = "{value}"
    # 跨层传播：当本规则命中时，位于命中资源与锚点之间路径上的资源
    # 也视为受影响（它们确实在证据链上，不是"结构上碰巧在下游"）。
    propagate: bool = False


@dataclass(frozen=True)
class RuleSet:
    """规则集。带版本号 —— 结论可复现的前提（D-036）。"""

    version: str
    rules: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class Recommendation:
    code: str
    text: str


@dataclass(frozen=True)
class Diagnosis:
    """诊断结论。

    红线（docs/DATA_MODEL.md §5.3）：
      - evidence 不得为空，否则 rootCause 只能是 UNKNOWN；
      - confidence 必须能由 confidence_breakdown 复算；
      - 必须携带 rule_set_version。
    """

    id: str
    created_at: datetime
    trigger: dict[str, Any]
    root_cause: str
    confidence: float
    confidence_breakdown: tuple[RuleHit, ...]
    affected_resources: tuple[str, ...] = ()
    potentially_affected: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    recommendation: tuple[Recommendation, ...] = ()
    rule_set_version: str = ""
    notes: tuple[str, ...] = field(default=())

    @property
    def is_unknown(self) -> bool:
        return self.root_cause == "UNKNOWN"


@dataclass(frozen=True)
class DiagnosisContext:
    """装配好的诊断上下文 —— 引擎的全部输入。"""

    anchor_resource_id: str
    window: TimeWindow
    resources: dict[str, Resource] = field(default_factory=dict)
    edges: tuple[Edge, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    rule_set: RuleSet = field(default_factory=lambda: RuleSet(version="rs-empty"))

    def children_of(self, resource_id: str) -> list[str]:
        return [e.child_id for e in self.edges if e.parent_id == resource_id]

    def parents_of(self, resource_id: str) -> list[str]:
        return [e.parent_id for e in self.edges if e.child_id == resource_id]

    def descendants(self, resource_id: str) -> list[str]:
        """沿资源图向下传播（受影响方向）。"""
        seen: set[str] = set()
        stack = [resource_id]
        out: list[str] = []
        while stack:
            current = stack.pop()
            for child in self.children_of(current):
                if child not in seen:
                    seen.add(child)
                    out.append(child)
                    stack.append(child)
        return out

    def ancestors(self, resource_id: str) -> list[str]:
        """向上追溯（找根因的方向）。"""
        seen: set[str] = set()
        stack = [resource_id]
        out: list[str] = []
        while stack:
            current = stack.pop()
            for parent in self.parents_of(current):
                if parent not in seen:
                    seen.add(parent)
                    out.append(parent)
                    stack.append(parent)
        return out

    def path_to_anchor(self, resource_id: str) -> list[str]:
        """返回从 resource_id（**不含**）向上到锚点（**不含**）之间的中间资源。

        保留此方法用于"锚点视角"的路径查询；跨层传播请用
        `correlation_path`（它沿证据链形状行走，不强制经过锚点）。
        """
        target = self.anchor_resource_id
        if resource_id == target:
            return []
        parents = {e.child_id: e.parent_id for e in self.edges if e.child_id != e.parent_id}
        out: list[str] = []
        seen: set[str] = set()
        current = resource_id
        while current in parents:
            parent = parents[current]
            if parent in seen:
                break
            seen.add(parent)
            if parent == target:
                return out
            out.append(parent)
            current = parent
        return out

    def correlation_path(self, resource_id: str, stop_at: set[str]) -> list[str]:
        """沿证据链形状向上行走，返回中间资源。

        从 resource_id 向上回溯，**遇到 stop_at 中的资源即停**（不包括它），
        返回途中的资源。这刻画的是"证据链形状"：

            gpu(证据) → vgpu(证据) → vm(中间层) → container(中间层) → ai_service(锚点)

        从 container 出发向上走，在 vgpu 处遇到已有证据即停，于是把
        vm 纳入受影响范围 —— 它确实参与了这条证据链，而不是"结构上碰巧在下游"。
        """
        parents = {e.child_id: e.parent_id for e in self.edges if e.child_id != e.parent_id}
        out: list[str] = []
        seen: set[str] = set()
        current = resource_id
        while current in parents:
            parent = parents[current]
            if parent in seen or parent in stop_at or parent == self.anchor_resource_id:
                break
            seen.add(parent)
            out.append(parent)
            current = parent
        return out
