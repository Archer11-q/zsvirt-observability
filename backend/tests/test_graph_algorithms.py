"""资源图算法单测（app/graph/algorithms.py）。

纯函数测试，不依赖数据库。

**方向约定（容易搞错，务必先读）**

链路按父子关系定义：

    HOST → GPU → VGPU → VM → CTR → AIS → AGENT

即 HOST 是**根**，AGENT 是**叶子**。因此：

- `ancestors(AGENT)` 一路向上走到 HOST
- `descendants(HOST)` 一路向下走到 AGENT
- 从某个证据节点向上走，是朝着 HOST 方向

这意味着一件事：**诊断锚点通常位于证据的下游**（症状在服务层，根因在 GPU 层）。
此时证据的**下游闭包**天然覆盖了到锚点的整条链，`affected` 已足够表达影响范围，
`on_chain` 为空是正常且正确的。`on_chain` 只在"证据位于锚点下游"时才非空。

本文件的期望值均来自实测输出，不是手算。
"""

from __future__ import annotations

import pytest

from app.graph.algorithms import (
    Edge,
    ancestors,
    build_child_index,
    build_parent_index,
    chain_intermediates,
    clean_parent_chain,
    descendants,
    impact_scope,
    neighbors,
    validate_edge,
)

HOST = "host:zsvirt:h1"
GPU = "gpu:zsvirt:g0"
VGPU = "vgpu:zsvirt:v0"
VM = "vm:zsvirt:vm0"
CTR = "container:probe:probe-x:web-0"
AIS = "ai_service:probe:probe-x:vllm"
AGENT = "agent:probe:probe-x:agent-1"
HOST2 = "host:zsvirt:h2"
GPU2 = "gpu:zsvirt:g1"

CHAIN = [
    Edge(HOST, GPU),
    Edge(GPU, VGPU),
    Edge(VGPU, VM),
    Edge(VM, CTR),
    Edge(CTR, AIS),
    Edge(AIS, AGENT),
    Edge(HOST2, GPU2),
]

PARENTS = build_parent_index(CHAIN)
CHILDREN = build_child_index(CHAIN)


# ---------------------------------------------------------------- 索引与遍历


class TestIndexAndTraversal:
    def test_parent_index_maps_child_to_parent(self):
        assert PARENTS[GPU] == HOST
        assert PARENTS[VM] == VGPU
        assert PARENTS[AIS] == CTR
        assert HOST not in PARENTS  # 根节点无父

    def test_child_index_groups_children(self):
        assert CHILDREN[HOST] == [GPU]
        assert CHILDREN[CTR] == [AIS]

    def test_self_loop_is_ignored(self):
        """自环是脏数据，不能污染遍历。"""
        assert build_parent_index([Edge(VM, VM)]) == {}
        assert build_child_index([Edge(VM, VM)]) == {}

    def test_second_parent_is_ignored_first_wins(self):
        """资源层级是树，多父归属时保留第一条，避免不确定行为。"""
        assert build_parent_index([Edge(HOST, VM), Edge(HOST2, VM)])[VM] == HOST

    def test_descendants_walks_toward_leaf(self):
        assert descendants(CTR, CHILDREN) == [AIS, AGENT]
        assert set(descendants(HOST, CHILDREN)) == {GPU, VGPU, VM, CTR, AIS, AGENT}
        assert HOST not in descendants(HOST, CHILDREN)

    def test_descendants_respects_max_depth(self):
        assert descendants(HOST, CHILDREN, max_depth=1) == [GPU]
        assert set(descendants(HOST, CHILDREN, max_depth=2)) == {GPU, VGPU}

    def test_ancestors_walks_toward_root(self):
        assert ancestors(AGENT, PARENTS) == [AIS, CTR, VM, VGPU, GPU, HOST]
        assert ancestors(HOST, PARENTS) == []

    def test_neighbors_returns_both_directions(self):
        assert set(neighbors(VM, PARENTS, CHILDREN)) == {VGPU, CTR}

    def test_cycle_does_not_hang(self):
        cyc_parents = {GPU: VGPU, VGPU: GPU}
        assert ancestors(GPU, cyc_parents, max_depth=10) == [VGPU]
        assert descendants(GPU, {GPU: [VGPU], VGPU: [GPU]}) == [VGPU]


# ---------------------------------------------------------------- 干净父链


class TestCleanParentChain:
    def test_stops_at_existing_evidence(self):
        """从容器向上（朝根）走，遇到 vgpu 即停，把 vm 作为过渡层。"""
        assert clean_parent_chain(CTR, PARENTS, stop_at={VGPU}) == [VM]

    def test_walks_all_the_way_to_root_without_stop(self):
        assert clean_parent_chain(CTR, PARENTS) == [VM, VGPU, GPU, HOST]

    def test_hard_stop_at_anchor(self):
        assert clean_parent_chain(AGENT, PARENTS, hard_stop=VM) == [AIS, CTR]

    def test_root_returns_empty(self):
        assert clean_parent_chain(HOST, PARENTS) == []

    def test_start_not_included(self):
        assert CTR not in clean_parent_chain(CTR, PARENTS, stop_at={VGPU})


# ---------------------------------------------------------------- 链上过渡层

# 实测：chain_intermediates 只在"证据位于锚点**下游**"时非空。
# 证据在锚点上游时，到锚点的整条链已由 evidence 的下游闭包覆盖（见 impact_scope）。


class TestChainIntermediates:
    def test_evidence_downstream_of_anchor(self):
        """AGENT 在 VM 下游：向上走到 VM，途中经过 AIS、CTR。"""
        assert chain_intermediates({AGENT}, PARENTS, VM) == sorted([AIS, CTR])

    def test_evidence_immediately_below_anchor(self):
        assert chain_intermediates({AIS}, PARENTS, VM) == [CTR]

    def test_evidence_upstream_of_anchor_yields_empty(self):
        """GPU 在 AIS 上游 → 到锚点要向下走，不产生向上路径的过渡层。

        这是最常见的情形（根因在 GPU 层、症状在服务层），
        影响范围由 evidence 的下游闭包表达，见 TestImpactScope。
        """
        assert chain_intermediates({GPU}, PARENTS, AIS) == []
        assert chain_intermediates({VGPU}, PARENTS, AIS) == []
        assert chain_intermediates({VM}, PARENTS, AIS) == []

    def test_adjacent_nodes_yield_empty(self):
        assert chain_intermediates({GPU}, PARENTS, VGPU) == []

    def test_evidence_equals_anchor_yields_empty(self):
        assert chain_intermediates({AIS}, PARENTS, AIS) == []

    def test_evidence_set_subtracted_from_result(self):
        """证据自身的节点不出现在过渡层里。"""
        mid = chain_intermediates({AGENT, AIS}, PARENTS, VM)
        assert AIS not in mid
        assert mid == [CTR]

    def test_result_sorted_and_unique(self):
        mid = chain_intermediates({AGENT}, PARENTS, VM)
        assert mid == sorted(set(mid))


# ---------------------------------------------------------------- 影响范围


class TestImpactScope:
    """实测语义（锚点在上游、证据在下游的典型诊断场景）：

    - `affected`            = 证据自身 + 其下游后代（经数据流受影响）
    - `potentially_affected`= 可达但既无证据也不在受影响的资源（通常是**上游**祖先）
    - `on_chain`            = 证据位于锚点下游时的过渡层（典型场景为空）
    """

    def test_evidence_and_downstream_are_affected(self):
        scope = impact_scope(AIS, {GPU, VGPU}, PARENTS, CHILDREN)
        assert set(scope.affected) == {GPU, VGPU, VM, CTR, AGENT}
        assert AIS not in scope.affected, "锚点自身不算受影响"

    def test_root_host_lands_in_potentially_affected(self):
        """**回归测试**：宿主机是锚点祖先、无证据。

        早期实现把它误算进 affected。现在的定义是：它既无证据也非下游，
        因此属于 potentially_affected —— 语义上"相关但未被影响"，
        比"被影响"更诚实。
        """
        scope = impact_scope(AIS, {GPU, VGPU}, PARENTS, CHILDREN)
        assert HOST not in scope.affected
        assert HOST in scope.potentially_affected

    def test_three_sets_are_pairwise_disjoint(self):
        """C 要求（D-075）：集合必须分开，不得互相混入。"""
        scope = impact_scope(AIS, {GPU, VGPU}, PARENTS, CHILDREN)
        assert not (set(scope.affected) & set(scope.potentially_affected))
        assert not (set(scope.affected) & set(scope.on_chain))
        assert not (set(scope.on_chain) & set(scope.potentially_affected))

    def test_unrelated_branch_excluded_everywhere(self):
        """另一台宿主机的 GPU 与本链无关，不得进入任何集合。"""
        scope = impact_scope(AIS, {GPU, VGPU}, PARENTS, CHILDREN)
        assert HOST2 not in scope.affected
        assert HOST2 not in scope.potentially_affected
        assert GPU2 not in scope.affected
        assert GPU2 not in scope.potentially_affected

    def test_anchor_upstream_evidence_downstream_gives_empty_on_chain(self):
        """典型场景：证据在上游（GPU），锚点在下游（AIS）→ on_chain 为空。"""
        scope = impact_scope(AIS, {GPU}, PARENTS, CHILDREN)
        assert scope.on_chain == ()
        assert set(scope.affected) == {GPU, VGPU, VM, CTR, AGENT}

    def test_anchor_downstream_evidence_upstream_populates_on_chain(self):
        """锚点在证据上游时 on_chain 才有内容。

        证据 = AGENT（叶子），锚点 = VM。受影响 = AGENT 自身（无下游）；
        过渡层 = 从 AGENT 向上到 VM 之间的 AIS、CTR。
        """
        scope = impact_scope(VM, {AGENT}, PARENTS, CHILDREN)
        assert set(scope.affected) == {AGENT}
        assert set(scope.on_chain) == {AIS, CTR}

    def test_evidence_outside_reachable_is_excluded(self):
        scope = impact_scope(AIS, {GPU2}, PARENTS, CHILDREN)
        assert GPU2 not in scope.affected
        assert GPU2 not in scope.on_chain

    def test_empty_evidence(self):
        scope = impact_scope(AIS, set(), PARENTS, CHILDREN)
        assert scope.affected == ()
        assert scope.on_chain == ()

    def test_propagate_false_yields_no_on_chain(self):
        scope = impact_scope(VM, {AGENT}, PARENTS, CHILDREN, propagate=False)
        assert scope.on_chain == ()
        assert AIS in scope.potentially_affected

    def test_counts_are_consistent(self):
        scope = impact_scope(AIS, {GPU, VGPU}, PARENTS, CHILDREN)
        assert scope.affected_count == len(scope.affected)
        assert scope.total_reachable == (
            len(scope.affected) + len(scope.potentially_affected) + len(scope.on_chain)
        )

    def test_affected_is_sorted_for_determinism(self):
        scope = impact_scope(AIS, {GPU, VGPU}, PARENTS, CHILDREN)
        assert list(scope.affected) == sorted(scope.affected)

    def test_multi_layer_evidence_does_not_duplicate(self):
        """多层证据（GPU + 容器）时 affected 不重复、且覆盖整条链。"""
        scope = impact_scope(AIS, {GPU, CTR}, PARENTS, CHILDREN)
        assert len(scope.affected) == len(set(scope.affected))
        assert set(scope.affected) == {GPU, VGPU, VM, CTR, AGENT}


# ---------------------------------------------------------------- 关系校验


class TestEdgeValidation:
    @pytest.mark.parametrize(
        ("parent", "child", "expected"),
        [
            ("host", "gpu", True),
            ("gpu", "vgpu", True),
            ("vgpu", "vm", True),
            ("vm", "container", True),
            ("container", "ai_service", True),
            ("ai_service", "agent", True),
            ("agent", "task", True),
            ("gpu", "host", False),  # 方向反了
            ("vm", "gpu", False),  # 跨层跳跃不合法
            ("agent", "vm", False),  # 方向反了
            (None, "gpu", True),  # 类型未知时放行
            ("gpu", None, True),
        ],
    )
    def test_validate_edge(self, parent, child, expected):
        assert validate_edge(parent, child) is expected
