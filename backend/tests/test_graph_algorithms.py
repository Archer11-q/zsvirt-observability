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

# chain_intermediates 必须**两个方向都处理**：锚点在链路末端（AI 服务）而证据
# 在 GPU / 容器上，是本项目最常见的场景。只做"证据在锚点下游"那一半会让
# `onChain` 在这种场景下恒为空，并把证据与锚点之间的过渡层错并进
# potentially_affected（与完全离链的宿主机混为一谈）—— 那正是 D-075 要防的。


class TestChainIntermediates:
    def test_evidence_downstream_of_anchor(self):
        """AGENT 在 VM 下游：向上走到 VM，途中经过 AIS、CTR。"""
        assert chain_intermediates({AGENT}, PARENTS, VM) == sorted([AIS, CTR])

    def test_evidence_immediately_below_anchor(self):
        assert chain_intermediates({AIS}, PARENTS, VM) == [CTR]

    def test_evidence_above_anchor_returns_the_connecting_path(self):
        """证据在锚点**上方**（本项目最常见）：返回两者之间的过渡层。

        链路 HOST→GPU→VGPU→VM→CTR→AIS，锚点 AIS：
          - 证据 VM   → 之间是 CTR            → [CTR]
          - 证据 VGPU → 之间是 VM、CTR        → [CTR, VM]
          - 证据 GPU  → 之间是 VGPU、VM、CTR  → [CTR, VGPU, VM]
        """
        assert chain_intermediates({VM}, PARENTS, AIS) == [CTR]
        assert chain_intermediates({VGPU}, PARENTS, AIS) == sorted([VM, CTR])
        assert chain_intermediates({GPU}, PARENTS, AIS) == sorted([VGPU, VM, CTR])

    def test_off_chain_ancestor_is_never_intermediate(self):
        """宿主机**不得**出现在过渡层里 —— 它提供了证据所在的层，但没被影响。

        这条是上面那个修复的反向保护：如果实现改成"从锚点一路向上走到根"，
        这个断言会立刻失败。
        """
        mid = chain_intermediates({GPU}, PARENTS, AIS)
        assert HOST not in mid, "离链祖先混进 on_chain 会让影响范围退化成全图"

    def test_evidence_on_another_chain_contributes_nothing(self):
        """证据与锚点互不为祖先 → 不贡献（GPU2 在另一条链上）。"""
        assert chain_intermediates({GPU2}, PARENTS, AIS) == []

    def test_adjacent_above_yields_empty(self):
        """证据正好是锚点的父节点 → 之间没有过渡层。"""
        assert chain_intermediates({CTR}, PARENTS, AIS) == []

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
    """三集是**可达集合的一个划分**：

    - `affected`             = 有证据的资源 + 其下游后代，**减去**证据与锚点之间的过渡层
    - `on_chain`             = 证据与锚点之间、本身无证据的过渡层（两个方向都可能）
    - `potentially_affected` = 结构上可达但既无证据也不在链上的资源（通常是**离链祖先**）
    """

    def test_evidence_and_downstream_are_affected(self):
        scope = impact_scope(AIS, {GPU, VGPU}, PARENTS, CHILDREN)
        # VM / CTR 是证据与锚点之间的过渡层，归 on_chain，不重复计入 affected
        assert set(scope.affected) == {GPU, VGPU, AGENT}
        assert set(scope.on_chain) == {VM, CTR}
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

    def test_evidence_above_anchor_populates_on_chain(self):
        """典型场景：证据在上游（GPU），锚点在下游（AIS）→ 过渡层非空。

        链路 HOST→GPU→VGPU→VM→CTR→AIS，
        证据 {GPU}，锚点 AIS → on_chain = {VGPU, VM, CTR}。

        这个断言是**回归测试**：修复前 `chain_intermediates` 只处理反方向，
        这里恒为空，过渡层与"离链的宿主机"一起落进 potentially_affected。
        """
        scope = impact_scope(AIS, {GPU}, PARENTS, CHILDREN)
        assert set(scope.on_chain) == {VGPU, VM, CTR}
        # 证据自身与其叶子后代仍在 affected；锚点不计入
        assert set(scope.affected) == {GPU, AGENT}
        assert AIS not in scope.affected, "锚点自身永不算受影响"

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
        """多层证据（GPU + 容器）时三集仍不重复，且并集覆盖整条链。"""
        scope = impact_scope(AIS, {GPU, CTR}, PARENTS, CHILDREN)
        assert len(scope.affected) == len(set(scope.affected))
        # VGPU / VM 夹在 GPU 与 CTR 之间且无证据 -> on_chain
        assert set(scope.on_chain) == {VGPU, VM}
        assert set(scope.affected) == {GPU, CTR, AGENT}
        union = set(scope.affected) | set(scope.on_chain) | set(scope.potentially_affected)
        assert union == {HOST, GPU, VGPU, VM, CTR, AGENT}, "并集覆盖锚点的全部可达资源"



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
