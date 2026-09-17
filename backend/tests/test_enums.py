"""枚举与字典一致性测试。

这些测试守护的是**契约冻结**（F-01～F-04）：枚举值一旦被改动、
或新增枚举却忘记补中文文案，这里立刻失败。

为什么值得单独测：枚举漂移是"只改代码不改契约"的最典型形态 ——
代码能跑、测试看似通过，但前端渲染出空白或字典端点漏项。
"""

from __future__ import annotations

import pytest

from app.enums import (
    ALERT_STATE_LABELS,
    ALERT_STATE_TRANSITIONS,
    EVENT_TYPE_DEFAULT_SEVERITY,
    EVENT_TYPE_LABELS,
    OBSERVABILITY_LABELS,
    REAL_RESOURCE_KINDS,
    RECOMMENDATION_LABELS,
    RESOURCE_KIND_LABELS,
    RESOURCE_STATUS_LABELS,
    ROOT_CAUSE_LABELS,
    SEVERITY_LABELS,
    SEVERITY_RANK,
    AlertState,
    EventType,
    Observability,
    RecommendationCode,
    ResourceKind,
    ResourceStatus,
    RootCause,
    Severity,
    assert_enums_consistent,
    build_dict_payload,
    worst_severity,
)
from app.graph.algorithms import RESOURCE_KINDS as GRAPH_RESOURCE_KINDS


class TestContractCounts:
    """冻结时确认的数量。数量变化意味着契约被改动，需三方复核。"""

    def test_event_types_are_17(self):
        """F-01 冻结了 17 项事件类型。"""
        assert len(list(EventType)) == 17

    def test_root_causes_are_11(self):
        """F-02 冻结了 11 项根因码。"""
        assert len(list(RootCause)) == 11

    def test_recommendations_are_14(self):
        """F-03 冻结了 14 项建议码。"""
        assert len(list(RecommendationCode)) == 14

    def test_severities_are_4(self):
        assert {m.value for m in Severity} == {"info", "warning", "error", "critical"}

    def test_alert_states_are_4(self):
        assert {m.value for m in AlertState} == {
            "firing",
            "acked",
            "resolved",
            "silenced",
        }

    def test_resource_kinds_include_vgpu_and_task(self):
        """F-04.1 定论：`vgpu` 与 `task` 均纳入首版。"""
        kinds = {m.value for m in ResourceKind}
        assert "vgpu" in kinds
        assert "task" in kinds

    def test_real_kinds_exclude_unresolved_placeholder(self):
        """`unresolved` 是占位类型，不属于真实资源类型。"""
        assert ResourceKind.UNRESOLVED.value not in REAL_RESOURCE_KINDS
        assert len(REAL_RESOURCE_KINDS) == 9


class TestConsistency:
    def test_assert_enums_consistent_passes(self):
        """每个枚举值都有中文文案、每个事件都有默认级别、每个状态都有迁移表。"""
        assert_enums_consistent()

    def test_every_enum_value_has_label(self):
        pairs = [
            (Severity, SEVERITY_LABELS),
            (AlertState, ALERT_STATE_LABELS),
            (ResourceKind, RESOURCE_KIND_LABELS),
            (ResourceStatus, RESOURCE_STATUS_LABELS),
            (Observability, OBSERVABILITY_LABELS),
            (EventType, EVENT_TYPE_LABELS),
            (RootCause, ROOT_CAUSE_LABELS),
            (RecommendationCode, RECOMMENDATION_LABELS),
        ]
        for enum_cls, labels in pairs:
            for member in enum_cls:
                assert member.value in labels, f"{enum_cls.__name__}.{member.name} 缺文案"
                assert labels[member.value], f"{enum_cls.__name__}.{member.name} 文案为空"

    def test_no_orphan_labels(self):
        """文案表里不得有枚举中不存在的键（否则是删枚举没删文案）。"""
        pairs = [
            (Severity, SEVERITY_LABELS),
            (AlertState, ALERT_STATE_LABELS),
            (ResourceKind, RESOURCE_KIND_LABELS),
            (ResourceStatus, RESOURCE_STATUS_LABELS),
            (Observability, OBSERVABILITY_LABELS),
            (EventType, EVENT_TYPE_LABELS),
            (RootCause, ROOT_CAUSE_LABELS),
            (RecommendationCode, RECOMMENDATION_LABELS),
        ]
        for enum_cls, labels in pairs:
            values = {m.value for m in enum_cls}
            orphan = set(labels) - values
            assert not orphan, f"{enum_cls.__name__} 文案表有多余键 {sorted(orphan)}"

    def test_graph_kinds_match_enum(self):
        """**防漂移**：图算法模块与枚举模块的资源类型必须一致。

        两处独立定义同一个枚举是漂移高发点 —— 一旦不一致，
        图校验会放行或拒绝错误的资源类型。
        """
        from app.enums import ResourceKind as RK

        assert set(GRAPH_RESOURCE_KINDS) == {m.value for m in RK if m is not RK.UNRESOLVED}

    def test_graph_kinds_exclude_placeholder(self):
        """占位类型不得被当作真实资源类型参与层级校验。"""
        from app.enums import ResourceKind as RK

        assert RK.UNRESOLVED.value not in GRAPH_RESOURCE_KINDS


class TestLabelsAreChinese:
    #: 无需翻译的专有名词 —— 强行中文化反而不专业（A、C 一直使用这些写法）。
    PROPER_NOUNS = {"GPU", "vGPU"}

    def test_labels_are_chinese_or_known_proper_nouns(self):
        """文案必须可读 —— C 的明确要求：前端不硬编码中文映射。

        允许 `GPU` / `vGPU` 这类专有名词保持原样：它们没有通行的中文译名，
        硬译会让界面更费解。
        """
        for key, label in RESOURCE_KIND_LABELS.items():
            assert label.strip(), f"{key} 的文案为空"
            if label in self.PROPER_NOUNS:
                continue
            assert any("\u4e00" <= ch <= "\u9fff" for ch in label), (
                f"{key} 的文案 {label!r} 既不含中文，也不在允许的专有名词列表中"
            )

    def test_proper_noun_list_is_minimal(self):
        """专有名词白名单必须保持最小 —— 否则会变成"什么都能塞"的借口。"""
        assert {"GPU", "vGPU"} == self.PROPER_NOUNS, (
            "若确需新增白名单项，请同时更新本断言并说明理由"
        )

    def test_codes_themselves_are_ascii(self):
        """码本身必须是 ASCII 大写/点分，不得混入中文。"""
        for member in RootCause:
            assert member.value.isascii()
        for member in RecommendationCode:
            assert member.value.isascii()
        for member in EventType:
            assert member.value.isascii()
            assert member.value == member.value.lower(), "事件类型必须全小写"


class TestSeverityOrdering:
    def test_rank_is_total_order(self):
        ranks = [SEVERITY_RANK[m.value] for m in Severity]
        assert ranks == sorted(ranks), "严重级别必须有严格递增的排序值"
        assert len(set(ranks)) == len(ranks), "排序值不得重复"

    def test_worst_severity(self):
        assert worst_severity("info", "critical") == "critical"
        assert worst_severity("critical", "info") == "critical"
        assert worst_severity("warning", "warning") == "warning"
        assert worst_severity("error", "warning") == "error"

    def test_worst_severity_unknown_does_not_crash(self):
        """未知级别不得抛异常 —— 上报数据可能带非预期值，健康检查不能因此崩。"""
        assert worst_severity("unknown", "warning") == "warning"
        assert worst_severity("unknown", "also-unknown") in {"unknown", "also-unknown"}


class TestAlertStateMachine:
    def test_every_state_has_transitions_defined(self):
        for member in AlertState:
            assert member.value in ALERT_STATE_TRANSITIONS

    def test_resolved_can_refire(self):
        """已恢复的告警可复发（同一聚合键再次触发）。"""
        assert "firing" in ALERT_STATE_TRANSITIONS["resolved"]

    def test_firing_can_be_silenced_and_resolved(self):
        allowed = ALERT_STATE_TRANSITIONS["firing"]
        assert {"acked", "resolved", "silenced"} <= allowed

    def test_silenced_does_not_directly_resolve_only(self):
        """静默是旁路，不是终态 —— 必须能回到 firing 或 resolved。"""
        assert ALERT_STATE_TRANSITIONS["silenced"] >= {"firing", "resolved"}


class TestDictPayload:
    def test_payload_has_all_required_sections(self):
        """C 的 Q14 要求：字典端点须覆盖 F-01/F-02/F-03 全部码 + 中文文案。"""
        payload = build_dict_payload()
        assert set(payload) >= {
            "severity",
            "alertState",
            "resourceKind",
            "resourceStatus",
            "observability",
            "eventType",
            "rootCause",
            "recommendation",
        }

    def test_payload_maps_code_to_label(self):
        payload = build_dict_payload()
        assert payload["rootCause"]["GPU_MEMORY_EXHAUSTED"] == "GPU 显存耗尽（本机超配）"
        assert payload["rootCause"]["GPU_NEIGHBOR_CONTENTION"] == "GPU 争用（同宿主其他负载占用）"
        assert payload["recommendation"]["REDUCE_CONCURRENCY"] == "降低推理并发或 batch size"
        assert payload["eventType"]["container.oom_killed"] == "容器内存溢出被终止"

    def test_gpu_attribution_codes_are_distinct(self):
        """「GPU 归因」能力的核心：两个根因码必须给出不同文案与建议。

        若两者文案相同，前端与运维无法区分"该找邻居还是该降自己的并发"，
        归因能力就退化成了一个码。
        """
        payload = build_dict_payload()
        exhausted = payload["rootCause"]["GPU_MEMORY_EXHAUSTED"]
        neighbour = payload["rootCause"]["GPU_NEIGHBOR_CONTENTION"]
        assert exhausted != neighbour

    def test_payload_is_a_copy(self):
        """不得返回内部字典本身 —— 调用方改动会污染全局状态。"""
        first = build_dict_payload()
        first["rootCause"]["GPU_MEMORY_EXHAUSTED"] = "tampered"
        second = build_dict_payload()
        assert second["rootCause"]["GPU_MEMORY_EXHAUSTED"] != "tampered"


class TestEventDefaultSeverity:
    def test_all_event_types_have_default(self):
        missing = {m.value for m in EventType} - set(EVENT_TYPE_DEFAULT_SEVERITY)
        assert not missing, f"缺默认级别的类型: {sorted(missing)}"

    def test_defaults_use_valid_severity(self):
        for etype, sev in EVENT_TYPE_DEFAULT_SEVERITY.items():
            assert sev in {m.value for m in Severity}, f"{etype} 的默认级别 {sev} 非法"

    def test_critical_defaults_are_resource_exhaustion(self):
        """被耗尽 / 被终止类事件必须是 critical（不能降级）。"""
        assert EVENT_TYPE_DEFAULT_SEVERITY["gpu.memory.exhausted"] == "critical"
        assert EVENT_TYPE_DEFAULT_SEVERITY["container.oom_killed"] == "critical"

    @pytest.mark.parametrize(
        "etype",
        [
            "inference.timeout",
            "agent.task.failed",
            "agent.network.timeout",
            "container.network.unreachable",
        ],
    )
    def test_application_failures_are_error_at_least(self, etype: str):
        assert SEVERITY_RANK[EVENT_TYPE_DEFAULT_SEVERITY[etype]] >= SEVERITY_RANK["error"]
