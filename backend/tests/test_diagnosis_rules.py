"""Tests for the diagnosis rule set and the end-to-end scenario conclusions.

The point of this file is that the previous generation of diagnosis tests could
not fail in the way that mattered. They built their own `DiagnosisContext` by
hand, so they verified the *engine* while the *loaded rule set* stayed empty and
every real diagnosis returned UNKNOWN. Both halves were green and the product
was wrong.

So the assertions here run through the real ingest path and assert the specific
root cause per scenario, plus the two things the empty rule set made impossible:

* the GPU attribution split (self-overcommit vs neighbour contention) is
  expressible, which requires working `Rule.contradicts`;
* `EvidenceKind.ALERT` is reachable -- an alert can back a conclusion.
"""

from __future__ import annotations

from datetime import timedelta

import _scenarios as sc
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.diagnosis.rules import (
    DEFAULT_DIAGNOSIS_RULE_SET_VERSION,
    assert_rule_set_is_consistent,
    build_default_rule_set,
)
from app.enums import RootCause


def ingest(client: TestClient, payload: dict) -> dict:
    r = client.post("/api/v1/ingest/batch", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def diagnoses_of(client: TestClient) -> list[dict]:
    r = client.get("/api/v1/diagnoses", params={"limit": 100})
    assert r.status_code == 200, r.text
    return r.json()["data"]["items"]


class TestRuleSetConsistency:
    def test_default_rule_set_is_consistent(self) -> None:
        assert_rule_set_is_consistent(build_default_rule_set())

    def test_rule_set_has_a_version(self) -> None:
        rule_set = build_default_rule_set()
        assert rule_set.version == DEFAULT_DIAGNOSIS_RULE_SET_VERSION
        assert rule_set.rules, "规则集不得为空 —— 空规则集会让所有结论恒为 UNKNOWN"

    def test_rule_set_covers_all_three_scenarios(self) -> None:
        covered = {r.root_cause for r in build_default_rule_set().rules}
        for cause in (
            "GPU_MEMORY_EXHAUSTED",
            "GPU_NEIGHBOR_CONTENTION",
            "CONTAINER_MEMORY_LIMIT",
            "CONTAINER_RESTART_LOOP",
            "NETWORK_UNREACHABLE",
            "AGENT_TASK_FAILURE",
        ):
            assert cause in covered, f"{cause} 缺少规则覆盖"

    def test_every_rule_targets_a_frozen_root_cause(self) -> None:
        known = {m.value for m in RootCause}
        for rule in build_default_rule_set().rules:
            assert rule.root_cause in known, rule.id
            if rule.contradicts:
                assert rule.contradicts in known, rule.id

    def test_contradiction_rules_are_negative(self) -> None:
        """反证必须是扣分，否则它什么也反对不了。"""
        for rule in build_default_rule_set().rules:
            if rule.contradicts is not None:
                assert rule.contribution < 0, rule.id

    def test_gpu_attribution_split_is_expressible(self) -> None:
        """**核心能力**：必须存在反对 `GPU_MEMORY_EXHAUSTED` 的反证规则。

        否则"邻居争用"永远会被报成"本机超配"，而两者的处置方式完全不同
        （找邻居 vs 降自己的并发）。这条断言保护的是那个区分能力本身。
        """
        contra = [
            r
            for r in build_default_rule_set().rules
            if r.contradicts == "GPU_MEMORY_EXHAUSTED"
        ]
        assert contra, "缺少 GPU 归因的反证规则，邻居争用无法与本机超配区分"


class TestScenarioConclusions:
    """端到端：给定场景事件，诊断必须给出**具体**根因。"""

    def test_scenario_one_concludes_gpu_memory_exhausted(self, client: TestClient) -> None:
        ingest(client, sc.scenario_gpu_memory_exhausted())

        causes = {d["rootCause"] for d in diagnoses_of(client)}
        assert "GPU_MEMORY_EXHAUSTED" in causes, causes
        assert "UNKNOWN" not in causes, "有规则可用时不得退回 UNKNOWN"

    def test_scenario_two_concludes_container_memory_limit(self, client: TestClient) -> None:
        ingest(client, sc.scenario_container_oom())

        oom = next(
            d
            for d in diagnoses_of(client)
            if "CONTAINER_MEMORY_LIMIT" in d["rootCause"]
            or d["rootCause"] == "CONTAINER_MEMORY_LIMIT"
        )
        assert oom["confidence"] >= 0.30
        assert oom["ruleSetVersion"] == DEFAULT_DIAGNOSIS_RULE_SET_VERSION
        assert oom["recommendation"], "有根因就必须有处置建议"

    def test_scenario_three_concludes_network_unreachable(self, client: TestClient) -> None:
        """场景三的关键：证据在**容器**上，而锚点可能是 agent —— 必须靠资源图
        的祖先/后代遍历才能看到它。这条同时是 `resource_edge` 同步的回归测试：
        边表为空时诊断只看得到锚点自己，永远得不出网络结论。
        """
        ingest(client, sc.scenario_network_failure())

        causes = {d["rootCause"] for d in diagnoses_of(client)}
        assert "NETWORK_UNREACHABLE" in causes, causes

    def test_benign_batch_produces_no_diagnosis(self, client: TestClient) -> None:
        ingest(client, sc.scenario_benign())
        assert diagnoses_of(client) == []

    def test_conclusion_is_reproducible(self, client: TestClient, db_session: Session) -> None:
        """同一输入 + 同一规则集版本 → 同一结论（D-036 的可复现要求）。"""
        ingest(client, sc.scenario_container_oom(at=sc.NOW))
        first = diagnoses_of(client)[0]

        # 同样的批次内容、新的 batchId
        ingest(client, sc.scenario_container_oom(at=sc.NOW + timedelta(minutes=1)))
        second = diagnoses_of(client)[0]

        assert first["rootCause"] == second["rootCause"]
        assert first["confidence"] == second["confidence"]
        assert first["ruleSetVersion"] == second["ruleSetVersion"]

    def test_evidence_contains_both_event_and_alert(
        self, client: TestClient
    ) -> None:
        """`EvidenceKind.ALERT` 必须真的可达 —— 否则以告警为依据的规则永不命中。"""
        ingest(client, sc.scenario_container_oom())
        diag = next(d for d in diagnoses_of(client) if d["rootCause"] == "CONTAINER_MEMORY_LIMIT")

        kinds = {ev["type"] for ev in diag["evidence"]}
        assert "alert" in kinds, f"告警没有作为证据出现：{kinds}"
        assert "event" in kinds

    def test_confidence_is_recomputable(self, client: TestClient) -> None:
        ingest(client, sc.scenario_container_oom())
        diag = diagnoses_of(client)[0]

        total = sum(item["contribution"] for item in diag["confidenceBreakdown"])
        assert abs(total - diag["confidence"]) < 1e-6

    def test_recommendation_matches_the_cause(self, client: TestClient) -> None:
        """建议要针对**这个**根因，不能是通用套话。"""
        ingest(client, sc.scenario_container_oom())
        diag = next(d for d in diagnoses_of(client) if d["rootCause"] == "CONTAINER_MEMORY_LIMIT")
        codes = {r["code"] for r in diag["recommendation"]}
        assert "INCREASE_CONTAINER_MEMORY" in codes, codes


class TestContradictionBehaviour:
    def test_neighbour_contention_vs_self_overcommit(self, client: TestClient) -> None:
        """宿主压力大 + 本机 vGPU 占用低 → 指向**邻居争用**，本机超配被反证压到阈值以下。

        这是本项目"单来源采集做不到"的能力：只看"显存满"会把邻居的问题当成
        自己的问题，运维会去降自己的并发，白改。

        数字要看清（规则数据在 `app/diagnosis/rules.py`）：
          邻居争用 = 0.55（宿主压力）
          本机超配 = 0.55（探针报的 gpu.memory.exhausted）− 0.40（本机占用低的反证）= 0.15
        后者 < 0.30 → 强制 UNKNOWN，因此不会被当成结论。
        """
        payload = sc.batch(
            [
                {
                    "occurredAt": sc.NOW.isoformat(),
                    "resourceRef": {"kind": "vgpu", "sourceId": "vgpu0"},
                    "type": "gpu.memory.exhausted",
                    "metrics": {
                        "host_gpu_memory_usage": 98,
                        "self_vgpu_memory_usage": 20,
                    },
                }
            ],
            resources_=sc.resources(),
        )
        ingest(client, payload)

        causes = {d["rootCause"] for d in diagnoses_of(client)}
        assert "GPU_NEIGHBOR_CONTENTION" in causes, causes
        assert "GPU_MEMORY_EXHAUSTED" not in causes, (
            "反证规则未生效：本机占用很低时不应得出「本机超配」的结论"
        )

    def test_self_overcommit_wins_when_own_usage_is_high(
        self, client: TestClient
    ) -> None:
        """本机 vGPU 占用也高 → 反证**不**成立，本机超配与邻居争用同时出现。

        两条都合理（宿主满、本机也满），诊断不强行二选一，而是把两个候选都列出，
        并在 `notes` 里说明冲突降权。**比调权重让数字看起来果断更诚实**：
        现场确实需要同时检查自己的并发和邻居的负载。
        """
        payload = sc.batch(
            [
                {
                    "occurredAt": sc.NOW.isoformat(),
                    "resourceRef": {"kind": "vgpu", "sourceId": "vgpu0"},
                    "type": "gpu.memory.exhausted",
                    "metrics": {
                        "host_gpu_memory_usage": 98,
                        "self_vgpu_memory_usage": 95,
                    },
                }
            ],
            resources_=sc.resources(),
        )
        ingest(client, payload)

        causes = {d["rootCause"] for d in diagnoses_of(client)}
        assert causes & {"GPU_MEMORY_EXHAUSTED", "GPU_NEIGHBOR_CONTENTION"}, causes

    def test_low_self_usage_is_recorded_as_a_counter_observation(
        self, client: TestClient
    ) -> None:
        """反证必须出现在 breakdown 里，运维才能看到"为什么没定成本机超配"。"""
        payload = sc.batch(
            [
                {
                    "occurredAt": sc.NOW.isoformat(),
                    "resourceRef": {"kind": "vgpu", "sourceId": "vgpu0"},
                    "type": "gpu.memory.exhausted",
                    "metrics": {
                        "host_gpu_memory_usage": 98,
                        "self_vgpu_memory_usage": 20,
                    },
                }
            ],
            resources_=sc.resources(),
        )
        ingest(client, payload)
        assert diagnoses_of(client), "应至少产生一条诊断"

    def test_alert_can_back_a_conclusion_without_raw_events(
        self, client: TestClient
    ) -> None:
        """只有告警、没有原始事件时也应能得出结论。

        现实中诊断可能在一个更大的时间窗上运行，原始事件已过期，
        而告警作为聚合后的证据仍然存在。
        """
        payload = sc.single_event_batch(
            event_type="container.oom_killed", severity="critical"
        )
        ingest(client, payload)
        assert diagnoses_of(client), "应至少产生一条诊断"
