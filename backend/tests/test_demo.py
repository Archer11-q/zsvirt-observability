"""演示运行器与场景定义测试。

演示材料也需要测试：**演示脚本一旦坏了，现场才发现**。而且这里的断言有实际价值：

- 场景必须**可复现**（同一台机器两次运行给出同样的数字）；
- 每个场景必须给出**预期根因** —— 这条把"演示能跑"和"演示讲得通"绑在一起；
- `healthy` 场景必须**不产生任何告警**（负向基线），否则演示一开场就是一片红。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.demo import DEMO_NOW, collect, render, reset, seed
from app.enums import AlertState
from app.zsvirt.scenarios import SCENARIO_NAMES, build_batch, describe_scenarios

#: 每个场景的预期（无告警的场景为 None）。
EXPECTED_CAUSE: dict[str, str | None] = {
    "healthy": None,
    "gpu_self_exhausted": "GPU_MEMORY_EXHAUSTED",
    "gpu_neighbor_contention": "GPU_NEIGHBOR_CONTENTION",
    "container_oom": "CONTAINER_MEMORY_LIMIT",
    "network_failure": "NETWORK_UNREACHABLE",
}


class TestScenarioDefinitions:
    def test_every_scenario_is_described(self) -> None:
        described = describe_scenarios()
        assert set(described) == set(SCENARIO_NAMES)
        assert all(text for text in described.values())

    def test_batch_ids_are_stable_per_scenario(self) -> None:
        """固定 `batchId` 让重复运行走幂等回放，演示不会重复写事件。"""
        first = build_batch("container_oom", at=DEMO_NOW)
        second = build_batch("container_oom", at=DEMO_NOW)
        assert first["batchId"] == second["batchId"] == "demo-container_oom"

    def test_batches_are_reproducible(self) -> None:
        a = build_batch("gpu_self_exhausted", at=DEMO_NOW)
        b = build_batch("gpu_self_exhausted", at=DEMO_NOW)
        assert a == b, "同一场景与同一时间基准必须产生完全相同的载荷"

    def test_unknown_scenario_is_rejected(self) -> None:
        with pytest.raises(KeyError):
            build_batch("nope", at=DEMO_NOW)

    @pytest.mark.parametrize("name", SCENARIO_NAMES)
    def test_vm_id_has_the_expected_shape(self, name: str) -> None:
        """`vmId` 形态错会让全部资源退化成占位节点，症状难以定位。"""
        batch = build_batch(name, at=DEMO_NOW)
        parts = batch["vmId"].split(":")
        assert len(parts) >= 3 and parts[0] == "vm" and parts[1] == "zsvirt"

    @pytest.mark.parametrize("name", SCENARIO_NAMES)
    def test_parent_chain_is_complete(self, name: str) -> None:
        """链条上不能有指向不存在 `sourceId` 的 `parentSourceId`。

        缺一环会导致外键违约 → 整批 503。这个错已经踩过一次。
        """
        batch = build_batch(name, at=DEMO_NOW)
        declared = {(r["kind"], r["sourceId"]) for r in batch["resources"]}
        declared.add(("vm", "self"))
        for resource in batch["resources"]:
            parent = resource.get("parentSourceId")
            if parent is None:
                continue
            assert any(src == parent for _, src in declared), (
                f"{name}: {resource['sourceId']} 的父 {parent!r} 未在同一批里上报"
            )

    @pytest.mark.parametrize("name", SCENARIO_NAMES)
    def test_event_resource_refs_exist(self, name: str) -> None:
        batch = build_batch(name, at=DEMO_NOW)
        declared = {(r["kind"], r["sourceId"]) for r in batch["resources"]}
        for event in batch["events"]:
            ref = event["resourceRef"]
            assert (ref["kind"], ref["sourceId"]) in declared, (
                f"{name}: 事件引用了未上报的资源 {ref}"
            )


class TestDemoRunner:
    def test_list_is_not_empty(self) -> None:
        assert len(SCENARIO_NAMES) >= 5

    @pytest.mark.parametrize("name", list(EXPECTED_CAUSE))
    def test_scenario_produces_expected_root_cause(self, db_session: Session, name: str) -> None:
        """**演示的核心断言**：每个场景必须给出预期根因。

        这条把"演示能跑"与"演示讲得通"绑在一起 —— 只断言"产生了诊断"
        无法发现根因判错。
        """
        reset(db_session)
        # 放宽门限：本测试验证的是"场景能否得出正确根因"，
        # 而门限行为由专门的用例覆盖（见 TestDemoRunner 的门限用例）。
        summaries = seed(db_session, [name], now=DEMO_NOW, diagnose_warnings=True)
        assert len(summaries) == 1

        report = collect(db_session)
        causes = {d["rootCause"] for d in report["diagnoses"]}
        expected = EXPECTED_CAUSE[name]

        if expected is None:
            assert report["alerts"] == [], f"{name} 不该产生告警"
            assert causes == set()
        else:
            assert expected in causes, (
                f"{name} 期望根因 {expected}，实际 {causes}；"
                f"告警={[a['ruleId'] for a in report['alerts']]}"
            )

    def test_healthy_scenario_is_a_clean_baseline(self, db_session: Session) -> None:
        """负向基线：正常负载**一条告警都不能有**。"""
        reset(db_session)
        seed(db_session, ["healthy"], now=DEMO_NOW)
        report = collect(db_session)
        assert report["alerts"] == []
        assert report["diagnoses"] == []
        # 资源与拓扑仍然存在 —— 否则"没有告警"可能只是因为什么都没上报
        assert report["counts"]["resources"] >= 7
        assert report["counts"]["edges"] >= 6

    def test_each_scenario_exactly_one_diagnosis(self, db_session: Session) -> None:
        """一次事故一条结论（事故聚合的验收）。

        逐条告警诊断会让一个容器 OOM 场景刷出三条几乎相同的结论。
        """
        for name in ("container_oom", "network_failure"):
            reset(db_session)
            seed(db_session, [name], now=DEMO_NOW, diagnose_warnings=True)
            report = collect(db_session)
            assert len(report["diagnoses"]) == 1, (
                f"{name} 产生 {len(report['diagnoses'])} 条诊断，应为 1 条"
            )

    def test_all_alerts_of_an_incident_share_one_diagnosis(self, db_session: Session) -> None:
        """事故内**每条**告警都必须关联到同一条结论 —— 不留没有答案的告警。"""
        reset(db_session)
        seed(db_session, ["container_oom"], now=DEMO_NOW, diagnose_warnings=True)
        report = collect(db_session)

        diagnosed = {a["diagnosisId"] for a in report["alerts"] if a["diagnosisId"]}
        assert diagnosed, "应该至少有一条告警关联了诊断"
        assert len(diagnosed) == 1, f"事故内关联了多条不同结论：{diagnosed}"

    def test_warning_alert_is_reported_as_skipped(self, db_session: Session) -> None:
        """按门限跳过的告警必须**如实报告原因**，而不是安静地什么都不做。"""
        reset(db_session)
        summaries = seed(db_session, ["gpu_neighbor_contention"], now=DEMO_NOW)
        auto = summaries[0]["autoDiagnosis"]
        assert auto["skippedBelowSeverity"] == 1
        assert auto["linked"] == {}

    def test_relaxing_the_threshold_diagnoses_the_warning(self, db_session: Session) -> None:
        """放宽门限后必须能补上被跳过的告警，且**只产出一条**结论。

        第一版在 `seed` 里另写了一份诊断逻辑，绕过了事故聚合，
        结果对同一事故产出了两条重复结论。

        注意：`diagnose_warnings` 场景下 `auto` 摘要里的"跳过"已被清零 ——
        那些告警最终**确实**被诊断了，再报告"跳过"会自相矛盾。
        """
        reset(db_session)
        summaries = seed(
            db_session, ["gpu_neighbor_contention"], now=DEMO_NOW, diagnose_warnings=True
        )
        auto = summaries[0]["autoDiagnosis"]
        assert auto["linked"], "放宽门限后应有关联的结论"
        assert auto["skippedBelowSeverity"] == 0, "跳过计数必须被清零，否则报告自相矛盾"

        report = collect(db_session)
        assert len(report["diagnoses"]) == 1, "一次事故只应有一条结论"
        assert report["diagnoses"][0]["rootCause"] == "GPU_NEIGHBOR_CONTENTION"

    def test_reset_clears_data_but_keeps_schema(self, db_session: Session) -> None:
        seed(db_session, ["container_oom"], now=DEMO_NOW)
        assert collect(db_session)["counts"]["resources"] > 0

        reset(db_session)
        after = collect(db_session)
        assert after["counts"]["resources"] == 0
        assert after["counts"]["alerts"] == 0

    def test_reset_reports_what_it_removed(self, db_session: Session) -> None:
        seed(db_session, ["container_oom"], now=DEMO_NOW)
        counts = reset(db_session)
        assert counts.get("resource", 0) > 0
        assert counts.get("alert", 0) > 0

    def test_second_run_is_idempotent_at_the_batch_level(self, db_session: Session) -> None:
        """同一 `batchId` 重复灌入必须走幂等回放，不重复写事件。

        这顺带演示了 A 的 Q2 幂等语义 —— 演示脚本自己就是它的用例。
        """
        reset(db_session)
        seed(db_session, ["container_oom"], now=DEMO_NOW)
        first = collect(db_session)["counts"]
        summaries = seed(db_session, ["container_oom"], now=DEMO_NOW)
        second = collect(db_session)["counts"]

        assert summaries[0]["duplicate"] is True
        assert first == second, "重复灌入改变了数据量"


class TestReportRendering:
    def test_render_declares_the_data_source(self, db_session: Session) -> None:
        """**诚实性**：报告开头必须说明数据是模拟的。

        模拟读数被当成真实 GPU 数据来讲解，是本项目最严重的信任事故。
        """
        import io

        reset(db_session)
        seed(db_session, ["container_oom"], now=DEMO_NOW)
        buffer = io.StringIO()
        render(collect(db_session), out=buffer)
        text = buffer.getvalue()

        assert "模拟" in text
        assert "SimulatedProvider" in text
        assert "origin=simulated" in text

    def test_render_shows_the_full_chain(self, db_session: Session) -> None:
        import io

        reset(db_session)
        seed(db_session, ["container_oom"], now=DEMO_NOW)
        buffer = io.StringIO()
        render(collect(db_session), out=buffer)
        text = buffer.getvalue()

        for kind in ("host", "gpu", "vgpu", "vm", "container", "ai_service", "agent"):
            assert kind in text, f"报告里缺少 {kind} 层"

    def test_render_shows_confidence_breakdown(self, db_session: Session) -> None:
        import io

        reset(db_session)
        seed(db_session, ["container_oom"], now=DEMO_NOW)
        buffer = io.StringIO()
        render(collect(db_session), out=buffer)
        text = buffer.getvalue()

        assert "R-CTR-OOM-100" in text, "报告必须列出支撑结论的规则"
        assert "建议" in text
        assert "INCREASE_CONTAINER_MEMORY" in text

    def test_render_handles_empty_state(self, db_session: Session) -> None:
        """空库也要能渲染（演示开始时常见）。"""
        import io

        reset(db_session)
        buffer = io.StringIO()
        render(collect(db_session), out=buffer)
        assert "（无）" in buffer.getvalue()

    def test_collect_exposes_exactly_one_diagnosis_per_trigger(self, db_session: Session) -> None:
        """一次事故一条结论，且结论 id 互不相同。

        `trigger` 里含列表（聚合的 `alertIds`），不能直接作为集合的键 ——
        用 JSON 串做键。
        """
        import json

        reset(db_session)
        seed(db_session, ["container_oom"], now=DEMO_NOW, diagnose_warnings=True)
        report = collect(db_session)

        ids = [d["id"] for d in report["diagnoses"]]
        assert len(ids) == len(set(ids)), f"结论 id 重复：{ids}"

        triggers = [
            json.dumps(d["trigger"], sort_keys=True, ensure_ascii=False)
            for d in report["diagnoses"]
        ]
        assert len(triggers) == len(set(triggers)), f"同一事故产生了多条结论：{triggers}"

    def test_alert_state_is_firing(self, db_session: Session) -> None:
        reset(db_session)
        seed(db_session, ["container_oom"], now=DEMO_NOW)
        states = {a["state"] for a in collect(db_session)["alerts"]}
        assert states == {AlertState.FIRING.value}


class TestTimestamps:
    def test_demo_now_is_fixed(self) -> None:
        """固定时间基准是"可复现"的前提。"""
        assert datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC) == DEMO_NOW

    def test_seeded_events_carry_the_demo_timestamp(self, db_session: Session) -> None:
        from sqlalchemy import text

        reset(db_session)
        seed(db_session, ["container_oom"], now=DEMO_NOW)
        rows = db_session.execute(
            text("select min(occurred_at), max(occurred_at) from event")
        ).one()
        assert rows[0] is not None
        assert rows[0].year == 2026
