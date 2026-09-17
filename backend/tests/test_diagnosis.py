"""诊断引擎单测。

重点验证 docs/backend/DIAGNOSIS_DESIGN.md 定下的红线：
  - 无证据 → UNKNOWN
  - 无规则匹配 → UNKNOWN
  - 最高分 < 0.30 → UNKNOWN
  - confidence 可由 breakdown 复算
  - 确定性：同输入同版本 → 同输出
  - 诚实性：模拟来源证据被标记
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.diagnosis import (
    DiagnosisContext,
    Evidence,
    EvidenceKind,
    EvidenceSource,
    Rule,
    RuleSet,
    TimeWindow,
    diagnose,
)
from app.diagnosis.types import Edge, Resource

T0 = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
HOST = "host:zsvirt:h1"
GPU = "gpu:zsvirt:g0"
VGPU = "vgpu:zsvirt:v0"
VM = "vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"
CTR = "container:probe:probe-3f2a9c10:web-0"
AIS = "ai_service:probe:probe-3f2a9c10:vllm"


def build_ctx(evidence, rules, anchor=AIS, window=None):
    resources = {
        HOST: Resource(id=HOST, kind="host"),
        GPU: Resource(id=GPU, kind="gpu", parent_id=HOST),
        VGPU: Resource(id=VGPU, kind="vgpu", parent_id=GPU),
        VM: Resource(id=VM, kind="vm", parent_id=VGPU),
        CTR: Resource(id=CTR, kind="container", parent_id=VM),
        AIS: Resource(id=AIS, kind="ai_service", parent_id=CTR),
    }
    edges = (
        Edge(HOST, GPU),
        Edge(GPU, VGPU),
        Edge(VGPU, VM),
        Edge(VM, CTR),
        Edge(CTR, AIS),
    )
    return DiagnosisContext(
        anchor_resource_id=anchor,
        window=window or TimeWindow(T0 - timedelta(minutes=5), T0 + timedelta(minutes=2)),
        resources=resources,
        edges=edges,
        evidence=tuple(evidence),
        rule_set=RuleSet(version="rs-test-0.1.0", rules=tuple(rules)),
    )


GPU_MEM_RULE = Rule(
    id="R-GPU-MEM-001",
    root_cause="GPU_MEMORY_EXHAUSTED",
    contribution=0.55,
    evidence_kind=EvidenceKind.METRIC,
    match_name="gpu_memory_usage",
    resource_kinds=("vgpu", "gpu"),
    comparator=">=",
    threshold=95,
    observed_template="gpu_memory_usage={value}%",
    propagate=True,
)

TIMEOUT_RULE = Rule(
    id="R-INFER-TIMEOUT-002",
    root_cause="GPU_MEMORY_EXHAUSTED",
    contribution=0.25,
    evidence_kind=EvidenceKind.EVENT,
    match_name="inference.timeout",
    resource_kinds=("ai_service",),
    min_count=5,
    observed_template="inference.timeout x{value}",
    propagate=True,
)


def test_no_evidence_returns_unknown():
    ctx = build_ctx([], [GPU_MEM_RULE])
    result = diagnose(ctx, now=T0)
    assert result.root_cause == "UNKNOWN"
    assert result.confidence == 0.0
    assert result.evidence == ()


def test_no_rule_match_returns_unknown_with_evidence():
    ev = Evidence(
        kind=EvidenceKind.METRIC,
        name="something_else",
        at=T0,
        resource_id=VGPU,
        value="10",
    )
    ctx = build_ctx([ev], [GPU_MEM_RULE])
    result = diagnose(ctx, now=T0)
    assert result.root_cause == "UNKNOWN"
    assert len(result.evidence) == 1, "必须保留已收集的证据，而不是丢弃"
    assert result.rule_set_version == "rs-test-0.1.0"


def test_gpu_memory_exhausted_happy_path():
    evidence = [
        Evidence(
            kind=EvidenceKind.METRIC,
            name="gpu_memory_usage",
            at=T0 - timedelta(seconds=40),
            resource_id=VGPU,
            value="98",
            source=EvidenceSource.ZWATCH,
        ),
        Evidence(
            kind=EvidenceKind.EVENT,
            name="inference.timeout",
            at=T0 - timedelta(seconds=10),
            resource_id=AIS,
            count=12,
            event_ids=("evt_001", "evt_002"),
        ),
    ]
    ctx = build_ctx(evidence, [GPU_MEM_RULE, TIMEOUT_RULE])
    result = diagnose(ctx, now=T0)
    assert result.root_cause == "GPU_MEMORY_EXHAUSTED"
    assert result.confidence == pytest.approx(0.80, abs=0.001)
    # confidence 必须可由 breakdown 复算
    assert sum(h.contribution for h in result.confidence_breakdown) == pytest.approx(
        result.confidence, abs=0.001
    )
    assert len(result.evidence) == 2
    assert result.rule_set_version == "rs-test-0.1.0"
    assert result.recommendation, "根因非 UNKNOWN 时必须给出处置建议"

    # 跨层关联：证据自身（GPU / AI 服务）+ 其下游后代全部计入受影响范围，
    # 覆盖 GPU→vGPU→VM→容器 整条链，这正是「跨层关联」能力的体现。
    assert VGPU in result.affected_resources
    assert VM in result.affected_resources
    assert CTR in result.affected_resources

    # 宿主机是锚点祖先且无证据 → 归 potentially_affected，不算"被影响"。
    # 它提供了证据所在的层，但自身并未受影响；两者必须分开（C 要求 D-075）。
    assert HOST not in result.affected_resources
    assert HOST in result.potentially_affected


def test_low_confidence_forces_unknown():
    weak = Rule(
        id="R-WEAK-001",
        root_cause="SOME_CAUSE",
        contribution=0.20,
        evidence_kind=EvidenceKind.METRIC,
        match_name="weak_metric",
    )
    ev = Evidence(kind=EvidenceKind.METRIC, name="weak_metric", at=T0, resource_id=VGPU, value="1")
    result = diagnose(build_ctx([ev], [weak]), now=T0)
    assert result.root_cause == "UNKNOWN", "最高分低于阈值时必须返回 UNKNOWN"
    assert any("0.3" in n or "强制" in n for n in result.notes)


def test_confidence_is_clamped_to_one():
    big = Rule(
        id="R-BIG-001",
        root_cause="BIG_CAUSE",
        contribution=0.8,
        evidence_kind=EvidenceKind.METRIC,
        match_name="m",
    )
    ev = Evidence(kind=EvidenceKind.METRIC, name="m", at=T0, resource_id=VGPU, value="1")
    ctx = build_ctx([ev, ev], [big])
    result = diagnose(ctx, now=T0)
    assert result.confidence <= 1.0


def test_out_of_window_evidence_is_excluded():
    ev = Evidence(
        kind=EvidenceKind.METRIC,
        name="gpu_memory_usage",
        at=T0 - timedelta(hours=3),  # 远在窗口之外
        resource_id=VGPU,
        value="99",
    )
    result = diagnose(build_ctx([ev], [GPU_MEM_RULE]), now=T0)
    assert result.root_cause == "UNKNOWN"


def test_unrelated_resource_evidence_is_excluded():
    other = "vm:zsvirt:OTHER"
    ev = Evidence(
        kind=EvidenceKind.METRIC,
        name="gpu_memory_usage",
        at=T0,
        resource_id=other,
        value="99",
    )
    result = diagnose(build_ctx([ev], [GPU_MEM_RULE]), now=T0)
    assert result.root_cause == "UNKNOWN", "不在锚点上下游范围内的证据不得参与"


def test_determinism_same_input_same_output():
    evidence = [
        Evidence(
            kind=EvidenceKind.METRIC,
            name="gpu_memory_usage",
            at=T0,
            resource_id=VGPU,
            value="98",
        )
    ]
    ctx = build_ctx(evidence, [GPU_MEM_RULE])
    a = diagnose(ctx, now=T0)
    b = diagnose(ctx, now=T0)
    assert a.root_cause == b.root_cause
    assert a.confidence == b.confidence
    assert a.evidence == b.evidence


def test_simulated_evidence_is_flagged():
    ev = Evidence(
        kind=EvidenceKind.METRIC,
        name="gpu_memory_usage",
        at=T0,
        resource_id=VGPU,
        value="98",
        source=EvidenceSource.SIMULATED,
    )
    result = diagnose(build_ctx([ev], [GPU_MEM_RULE]), now=T0)
    assert result.root_cause == "GPU_MEMORY_EXHAUSTED"
    assert any("模拟" in n for n in result.notes), "模拟数据必须被标注，不得伪装成真实采集"
    assert result.evidence[0].is_simulated
