"""诊断引擎的数据结构。

这些类型是引擎的**输入输出契约**，刻意不依赖 ORM / HTTP，
以便脱离数据库与网络做单元测试（docs/backend/DIAGNOSIS_DESIGN.md §2.1）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.graph.algorithms import (
    Edge as GraphEdge,  # 与本地 Edge（诊断消息用）区分
)
from app.graph.algorithms import (
    ancestors as graph_ancestors,
)
from app.graph.algorithms import (
    build_child_index,
    build_parent_index,
)
from app.graph.algorithms import (
    chain_intermediates as graph_chain_intermediates,
)
from app.graph.algorithms import (
    clean_parent_chain as graph_clean_parent_chain,
)
from app.graph.algorithms import (
    descendants as graph_descendants,
)
from app.graph.algorithms import (
    impact_scope as graph_impact_scope,
)


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

    # ---- 图遍历：复用 app.graph.algorithms，避免与资源图出现双份实现 ----
    #
    # 诊断引擎与资源图（L3）必须共享同一套遍历语义，否则"引擎算出的影响范围"
    # 与"拓扑接口给出的影响范围"会不一致。这里只做适配，不重复实现算法。

    @property
    def _parent_index(self) -> dict[str, str]:
        return build_parent_index(
            GraphEdge(parent_id=e.parent_id, child_id=e.child_id, relation=e.relation)
            for e in self.edges
        )

    @property
    def _child_index(self) -> dict[str, list[str]]:
        return build_child_index(
            GraphEdge(parent_id=e.parent_id, child_id=e.child_id, relation=e.relation)
            for e in self.edges
        )

    def children_of(self, resource_id: str) -> list[str]:
        return list(self._child_index.get(resource_id, ()))

    def parents_of(self, resource_id: str) -> list[str]:
        parent = self._parent_index.get(resource_id)
        return [parent] if parent is not None else []

    def descendants(self, resource_id: str) -> list[str]:
        """沿资源图向下传播（受影响方向）。"""
        return graph_descendants(resource_id, self._child_index)

    def ancestors(self, resource_id: str) -> list[str]:
        """向上追溯（找根因的方向）。"""
        return graph_ancestors(resource_id, self._parent_index)

    def correlation_path(self, resource_id: str, stop_at: set[str]) -> list[str]:
        """沿**干净父链**向上行走，返回中间资源。

        从 resource_id 向上回溯，**遇到 stop_at 中的资源即停**（不包括它），
        返回途中的资源。这刻画的是证据链：

            gpu(证据) → vgpu(证据) → vm(中间层) → container(中间层) → ai_service(锚点)

        从 GPU 出发向上走，在 VGPU（另一条证据）处停住，于是把 VM、CTR
        纳入受影响范围 —— 它们确实参与了这条链。**关键是不得越过停止点继续
        走到宿主机**：宿主机与"GPU 显存耗尽"这条链无关。
        """
        return graph_clean_parent_chain(
            resource_id,
            parent_index=self._parent_index,
            stop_at=stop_at,
            hard_stop=self.anchor_resource_id,
        )

    def chain_intermediates(self, evidence_resources: Iterable[str]) -> list[str]:
        """证据链上缺环的中间层（见 `app.graph.algorithms.chain_intermediates`）。"""
        return graph_chain_intermediates(
            evidence_resources, self._parent_index, self.anchor_resource_id
        )

    def impact_scope(self, evidence_resources: Iterable[str], *, propagate: bool = True):
        """影响范围。与 `app.graph.algorithms.impact_scope` 同一实现。"""
        return graph_impact_scope(
            self.anchor_resource_id,
            evidence_resources,
            self._parent_index,
            self._child_index,
            propagate=propagate,
        )
