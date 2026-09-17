"""Regression tests for the defects that only a multi-scenario run exposed.

Three times during this task, the single-chain unit tests were green while a
three-fault run gave the wrong answer:

* every alert in a chain merged into one incident (all `onChain` sets empty made
  overlap-clustering degenerate);
* sibling containers shared a scope, because `load_graph` is an *undirected*
  closure -- invisible with only one chain;
* every workload card showed whichever incident happened to be newest.

Each is asserted here against the API, on a database with several scenarios
loaded at once. That is the shape a demo actually runs in.
"""

from __future__ import annotations

import _scenarios as sc
from fastapi.testclient import TestClient

from app.demo import DEMO_NOW, collect, reset, seed

#: 三个 GPU/容器/网络场景的预期根因
EXPECTED = {
    "gpu_self_exhausted": "GPU_MEMORY_EXHAUSTED",
    "container_oom": "CONTAINER_MEMORY_LIMIT",
    "network_failure": "NETWORK_UNREACHABLE",
}


class TestMultipleIncidentsStaySeparate:
    def test_three_faults_give_three_distinct_causes(self, client: TestClient) -> None:
        """三起不同故障必须给出**三个**根因。

        这是本文件存在的理由：任何"把所有告警并成一次事故"的回归都会让它失败。
        单场景用例看不到这一点 —— 一次事故本来就只有一条结论。
        """
        for name in EXPECTED:
            r = client.post("/api/v1/ingest/batch", json=sc.scenario_for_demo(name))
            assert r.status_code == 200, r.text

        items = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        causes = {d["rootCause"] for d in items}
        assert causes == set(EXPECTED.values()), (
            f"期望 {sorted(EXPECTED.values())}，实际 {sorted(causes)}"
        )

    def test_each_incident_has_exactly_one_diagnosis(self, client: TestClient) -> None:
        """每起事故一条结论 —— 不多（重复）也不少（漏掉）。"""
        for name in EXPECTED:
            client.post("/api/v1/ingest/batch", json=sc.scenario_for_demo(name))

        items = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        assert len(items) == len(EXPECTED), (
            f"{len(items)} 条结论对应 {len(EXPECTED)} 起事故："
            f"{[(d['rootCause'], d['trigger'].get('clusterSize')) for d in items]}"
        )

    def test_alerts_do_not_share_a_diagnosis_across_incidents(
        self, client: TestClient
    ) -> None:
        """不同事故的告警不得指向同一条结论。"""
        for name in EXPECTED:
            client.post("/api/v1/ingest/batch", json=sc.scenario_for_demo(name))

        items = client.get("/api/v1/alerts", params={"limit": 50}).json()["data"]["items"]
        by_diagnosis: dict[str, set[str]] = {}
        for alert in items:
            if alert["diagnosisId"]:
                by_diagnosis.setdefault(alert["diagnosisId"], set()).add(alert["resourceId"])

        assert by_diagnosis, "至少应有告警关联到诊断"
        # 一条结论涉及的资源必须都在同一条链路上（同一容器分支），
        # 不能横跨两个兄弟容器
        for diagnosis_id, resources in by_diagnosis.items():
            containers = {r for r in resources if r.startswith("container:")}
            assert len(containers) <= 1, (
                f"结论 {diagnosis_id} 同时涉及多个容器 {containers} —— 两起事故被并了"
            )

    def test_workload_card_shows_its_own_incident(self, client: TestClient) -> None:
        """工作负载卡片必须显示**它自己**那次事故，而不是整条链上最新的一次。

        回归点：归并只按 `created_at` 取最新时，所有卡片都会显示最后注入的那起故障。
        """
        for name in EXPECTED:
            client.post("/api/v1/ingest/batch", json=sc.scenario_for_demo(name))

        work = client.get(
            "/api/v1/workloads",
            params={"since": "2026-09-16T00:00:00+00:00", "to": "2026-09-18T00:00:00+00:00"},
        ).json()["data"]

        by_suffix = {item["id"]: item for item in work["items"]}
        for name, cause in EXPECTED.items():
            suffix = sc.demo_suffix(name)
            workload_id = f"ai_service:probe:{sc.DEMO_AGENT_ID}:demo-svc-{suffix}"
            assert workload_id in by_suffix, f"缺少工作负载 {workload_id}"
            assert by_suffix[workload_id]["rootCause"] == cause, (
                f"{name} 的卡片显示 {by_suffix[workload_id]['rootCause']}，应为 {cause}"
            )

    def test_sibling_containers_have_disjoint_scopes(self, client: TestClient) -> None:
        """兄弟容器的可达范围必须互不相交。

        回归点：`load_graph` 是**无向**闭包，从容器向上走到 VM 后会再向下走进
        另一个容器，于是两起事故互相污染。单链测试看不到这个缺陷。
        """
        from app.db import get_db
        from app.diagnosis.service import resource_scope_for
        from app.main import app

        for name in EXPECTED:
            client.post("/api/v1/ingest/batch", json=sc.scenario_for_demo(name))

        factory = app.dependency_overrides[get_db]
        session = next(factory())
        try:
            oom = f"container:probe:{sc.DEMO_AGENT_ID}:demo-ctr-oom"
            net = f"container:probe:{sc.DEMO_AGENT_ID}:demo-ctr-net"
            oom_scope = resource_scope_for(session, oom)
            net_scope = resource_scope_for(session, net)

            assert oom in oom_scope and net in net_scope
            assert oom not in net_scope, "网络容器的范围里出现了 OOM 容器（兄弟泄漏）"
            assert net not in oom_scope, "OOM 容器的范围里出现了网络容器（兄弟泄漏）"
            # 共同祖先仍然共享：基础设施是同一个
            assert "vm:probe:demo-agent:self" in oom_scope & net_scope
        finally:
            session.close()

    def test_healthy_scenario_does_not_disturb_others(self, client: TestClient) -> None:
        """正常负载场景不得影响已有告警与诊断。"""
        for name in EXPECTED:
            client.post("/api/v1/ingest/batch", json=sc.scenario_for_demo(name))
        before = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]

        client.post("/api/v1/ingest/batch", json=sc.scenario_for_demo("healthy"))

        after = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        assert len(after) == len(before)


class TestDemoRunnerMultiScenario:
    def test_seed_all_scenarios_gives_three_causes(self, db_session) -> None:
        reset(db_session)
        seed(db_session, list(EXPECTED), now=DEMO_NOW, diagnose_warnings=True)
        report = collect(db_session)
        assert {d["rootCause"] for d in report["diagnoses"]} == set(EXPECTED.values())

    def test_every_alert_in_an_incident_is_linked(self, db_session) -> None:
        """事故内每条告警都要有结论 —— 不能留下"有告警但没答案"的条目。"""
        reset(db_session)
        seed(db_session, list(EXPECTED), now=DEMO_NOW, diagnose_warnings=True)
        report = collect(db_session)

        unlinked = [a["ruleId"] for a in report["alerts"] if not a["diagnosisId"]]
        assert unlinked == [], f"以下告警没有关联结论：{unlinked}"
