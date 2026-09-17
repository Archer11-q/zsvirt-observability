"""资源图遍历算法（纯函数，无数据库依赖）。

这些算法与 `app.diagnosis` 共用同一套语义，避免"引擎里一套、存储层另一套"
导致跨层关联结果不一致。设计见 docs/backend/BACKEND_DESIGN.md §2.4。

术语：
  descendants        沿资源图向下传播（受影响方向）
  ancestors          向上追溯（找根因的方向）
  clean_parent_chain 沿干净父链行走 —— 遇到另一条证据链端点即停，
                     用于刻画"证据链"而不会拐到无关分支
  chain_intermediates 证据与锚点之间、在链上但本身无证据的中间资源
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

RESOURCE_KINDS = (
    "host",
    "gpu",
    "vgpu",
    "vm",
    "container",
    "process",
    "ai_service",
    "agent",
    "task",
)

# 允许的父子关系方向。用于校验，防止构建出 host 挂在 vm 下面的荒谬图。
PARENT_CHILD_ALLOWED: dict[str, frozenset[str]] = {
    "host": frozenset({"gpu"}),
    "gpu": frozenset({"vgpu"}),
    "vgpu": frozenset({"vm"}),
    "vm": frozenset({"container", "process"}),
    "container": frozenset({"process", "ai_service"}),
    "process": frozenset({"ai_service"}),
    "ai_service": frozenset({"agent"}),
    "agent": frozenset({"task"}),
    "task": frozenset(),
}


class GraphError(ValueError):
    """资源图结构非法。"""


@dataclass(frozen=True)
class Edge:
    parent_id: str
    child_id: str
    relation: str = "hosts"


@dataclass(frozen=True)
class ImpactScope:
    """影响范围。三个集合语义严格区分，**不得互相混入**。

    affected             有证据支持的资源 + 它们的下游后代
    potentially_affected 结构上相关但无证据的资源（不含链上中间层）
    on_chain             证据与锚点之间、位于干净父链上但无证据的中间层

    成员 C 的要求（D-075）：`affected` 与 `potentially_affected` 必须并列输出，
    不得合并 —— 否则"影响范围"会退化成无意义的全量拓扑。
    """

    affected: tuple[str, ...]
    potentially_affected: tuple[str, ...] = ()
    on_chain: tuple[str, ...] = ()

    @property
    def affected_count(self) -> int:
        return len(self.affected)

    @property
    def total_reachable(self) -> int:
        return len(self.affected) + len(self.potentially_affected) + len(self.on_chain)


# ---------------------------------------------------------------- 索引构建


def build_parent_index(edges: Iterable[Edge]) -> dict[str, str]:
    """child_id -> parent_id。

    同一 child 有多个 parent 时保留第一条并忽略后续 —— 资源层级是树，
    不允许多归属（多重关系类型见 docs/DATA_MODEL.md §6 的待确认项）。
    自环被忽略。
    """
    index: dict[str, str] = {}
    for edge in edges:
        if edge.child_id == edge.parent_id:
            continue
        index.setdefault(edge.child_id, edge.parent_id)
    return index


def build_child_index(edges: Iterable[Edge]) -> dict[str, list[str]]:
    """parent_id -> [child_id, ...]（保持输入顺序，便于确定性测试）。"""
    index: dict[str, list[str]] = {}
    for edge in edges:
        if edge.child_id == edge.parent_id:
            continue
        index.setdefault(edge.parent_id, []).append(edge.child_id)
    return index


def validate_edge(parent_kind: str | None, child_kind: str | None) -> bool:
    """校验父子类型方向是否合法。类型未知时放行（避免因缺失元数据而丢关系）。"""
    if parent_kind is None or child_kind is None:
        return True
    allowed = PARENT_CHILD_ALLOWED.get(parent_kind)
    if allowed is None:
        return False
    return child_kind in allowed


# ---------------------------------------------------------------- 遍历


def descendants(
    root: str,
    child_index: Mapping[str, Sequence[str]],
    max_depth: int | None = None,
) -> list[str]:
    """向下广度优先遍历，返回 root 的全部后代（不含 root 自身）。

    顺序为 BFS，结果确定。含环保护。max_depth 为 None 表示不限深度。
    """
    out: list[str] = []
    seen: set[str] = {root}
    frontier: list[tuple[str, int]] = [(root, 0)]
    while frontier:
        next_frontier: list[tuple[str, int]] = []
        for node, depth in frontier:
            if max_depth is not None and depth >= max_depth:
                continue
            for child in child_index.get(node, ()):
                if child in seen:
                    continue
                seen.add(child)
                out.append(child)
                next_frontier.append((child, depth + 1))
        frontier = next_frontier
    return out


def ancestors(
    node: str,
    parent_index: Mapping[str, str],
    max_depth: int | None = None,
) -> list[str]:
    """向上追溯，返回全部祖先（不含 node 自身）。含环保护。"""
    out: list[str] = []
    seen: set[str] = {node}
    current = node
    depth = 0
    while True:
        if max_depth is not None and depth >= max_depth:
            break
        parent = parent_index.get(current)
        if parent is None or parent in seen:
            break
        seen.add(parent)
        out.append(parent)
        current = parent
        depth += 1
    return out


def neighbors(
    node: str,
    parent_index: Mapping[str, str],
    child_index: Mapping[str, Sequence[str]],
) -> list[str]:
    """相邻资源（父 + 子），用于影响范围的最小闭包。"""
    out: list[str] = []
    parent = parent_index.get(node)
    if parent is not None:
        out.append(parent)
    out.extend(child_index.get(node, ()))
    return out


def clean_parent_chain(
    start: str,
    parent_index: Mapping[str, str],
    stop_at: Iterable[str] = (),
    hard_stop: str | None = None,
) -> list[str]:
    """从 start 出发沿父链向上走到停止点，返回途中的中间资源。

    不含 start 自身，也不含停止点。之所以叫**干净**父链：只要沿途遇到
    `stop_at` 中的节点就立刻停住 —— 该节点是另一条证据链的端点，再往上走
    就跑到别的链上去了。

    例：证据 = {GPU, VGPU}，从 GPU 出发 → 第一步就撞到 VGPU 而停 → 返回 []
        证据 = {VGPU} 单独看，从 VGPU 出发 → [VM, CTR]（锚点处停）

    **注意**：调用方若要取"证据到锚点之间的全部中间资源"，应使用
    `chain_intermediates` —— 它会正确地从证据集合的**边界**继续往上走。
    """
    stop = set(stop_at)
    out: list[str] = []
    seen: set[str] = {start}
    current = start
    while True:
        parent = parent_index.get(current)
        if parent is None or parent in seen:
            break
        if parent in stop or (hard_stop is not None and parent == hard_stop):
            break
        seen.add(parent)
        out.append(parent)
        current = parent
    return out


def correlation_path(
    start: str,
    anchors: Iterable[str] = (),
    parent_index: Mapping[str, str] | None = None,
    stop_at: Iterable[str] = (),
    hard_stop: str | None = None,
) -> list[str]:
    """`clean_parent_chain` 的兼容别名（`anchors` 已废弃，仅为向后兼容保留）。"""
    del anchors
    if parent_index is None:
        raise GraphError("parent_index is required")
    return clean_parent_chain(start, parent_index, stop_at=stop_at, hard_stop=hard_stop)


def chain_intermediates(
    evidence_resources: Iterable[str],
    parent_index: Mapping[str, str],
    anchor: str,
) -> list[str]:
    """证据与锚点之间、位于链上但**本身无证据**的过渡层。

    算法分两遍，避免边界错误：

      第一遍：判断 anchor 是否在该证据的**祖先链**上。
              若不是（两者不在同一条链上），该证据不贡献过渡层。
      第二遍：从证据沿父链向上走并**收集每一个经过的节点**，直到抵达 anchor
              （anchor 本身不收集）。遇到其他证据节点**仍然收集** ——
              它是需要跨越的过渡目标，且更上游可能还有证据。

    例：证据 = {GPU, VGPU}，锚点 = AI_SERVICE（链路 HOST→GPU→VGPU→VM→CTR→AIS）
        GPU  → 收集 VGPU, VM, CTR → 抵达 AIS 停
        VGPU → 收集 VM, CTR      → 抵达 AIS 停
        并集 = {VGPU, VM, CTR}，减去证据 = {VM, CTR}

    **离链祖先（宿主机）不会被收集** —— 锚点在它下游，向上走不到它。

    若某证据在锚点**下游**（锚点不是它的祖先），它不贡献过渡层。
    """
    evidence = set(evidence_resources)
    mid: set[str] = set()

    for resource_id in evidence:
        # 第一遍：anchor 是否在该证据的祖先链上？
        ancestors_of_evidence: set[str] = set()
        current = resource_id
        while True:
            parent = parent_index.get(current)
            if parent is None or parent in ancestors_of_evidence:
                break
            ancestors_of_evidence.add(parent)
            current = parent

        if anchor not in ancestors_of_evidence:
            continue  # 锚点不在该证据上游 → 无过渡层

        # 第二遍：收集路径上的节点（不含 anchor）
        current = resource_id
        seen: set[str] = {resource_id}
        while True:
            parent = parent_index.get(current)
            if parent is None or parent in seen or parent == anchor:
                break
            seen.add(parent)
            mid.add(parent)
            current = parent

    mid.discard(anchor)
    mid -= evidence
    return sorted(mid)


def impact_scope(
    anchor: str,
    evidence_resources: Iterable[str],
    parent_index: Mapping[str, str],
    child_index: Mapping[str, Sequence[str]],
    *,
    propagate: bool = True,
) -> ImpactScope:
    """计算影响范围。

    三个集合，语义严格区分（docs/backend/DIAGNOSIS_DESIGN.md §6）：

    | 集合 | 定义 |
    |---|---|
    | `affected` | 有证据的资源 + **它们的下游后代**（证据直接支持；下游经数据流受影响） |
    | `on_chain` | 证据与锚点之间、干净父链上但**无证据**的中间层（`propagate=False` 时为空） |
    | `potentially_affected` | 结构上可达但既无证据也不在链上的资源 |

    **离链祖先（如证据为 GPU 时的宿主机）三个集合都不进** —— 它提供了证据
    所在的层，但并未被影响。

    **红线**：`affected` 与 `potentially_affected` 必须分开输出，不得混入
    （成员 C 的要求，D-075）。
    """
    reachable: set[str] = {anchor}
    reachable.update(ancestors(anchor, parent_index))
    reachable.update(descendants(anchor, child_index))

    # 只考虑落在可达范围内的证据（不在本次诊断上下文里的证据不参与）
    evidence_set = {r for r in evidence_resources if r in reachable}

    # affected：证据自身 + 它们的下游后代
    affected: set[str] = set(evidence_set)
    for resource_id in evidence_set:
        affected.update(descendants(resource_id, child_index))

    # on_chain：干净父链上缺环的中间层
    on_chain = set(chain_intermediates(evidence_set, parent_index, anchor) if propagate else ())

    affected.discard(anchor)
    on_chain.discard(anchor)
    potentially = reachable - affected - on_chain - {anchor}

    return ImpactScope(
        affected=tuple(sorted(affected)),
        potentially_affected=tuple(sorted(potentially)),
        on_chain=tuple(sorted(on_chain)),
    )
