"""告警引擎测试（`docs/DEVELOPMENT_PLAN.md` 任务 2）。

逐条覆盖验收标准，并把**负向**用例放在同等位置 —— 告警质量主要由
"不该报的时候有没有报"决定，而不是"该报的时候报了没有"。

| 验收标准 | 测试 |
|---|---|
| 1 事件序列 → 预期数量的告警，聚合生效无重复 | `TestScenarios`、`TestAggregation` |
| 2 每条告警 `evidenceEventIds` 非空且指向真实事件 | `TestEvidenceBinding` |
| 3 重复触发只累加 `count`、不新建 | `TestAggregation` |
| 4 静默期内不产生新告警；到期后恢复 | `TestSilence` |
| 5 条件恢复后转 `resolved` 且写 `resolvedAt` | `TestRecovery` |
| 6 负向：无证据的事件不产生告警 | `TestNegativeCases` |

另有两条本模块特有的红线：

- **规则集版本随告警落库** —— 否则"这条告警当时按什么规则报的"无从回答；
- **严重级别取规则与事件中更严重的一方** —— 规则说 warning、事件报 critical
  时不得降级显示。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import _scenarios as sc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.alerts.engine import (
    evaluate_events,
    reconcile,
    reconcile_all,
    release_expired_silences,
)
from app.alerts.rules import (
    DEFAULT_RULE_SET_VERSION,
    AlertCondition,
    AlertRule,
    MetricThreshold,
    RuleSet,
    assert_rules_are_consistent,
    build_default_rule_set,
)
from app.enums import AlertState, ResourceKind, Severity
from app.graph.repository import upsert_resource
from app.models import Alert, Event

CONTAINER_ID = "container:probe:probe-x:web-0"
GPU_ID = "gpu:zsvirt:g0"


def ingest(client: TestClient, payload: dict) -> dict:
    r = client.post("/api/v1/ingest/batch", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def alerts_of(client: TestClient, **params) -> list[dict]:
    r = client.get("/api/v1/alerts", params={"limit": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()["data"]["items"]


def seed_resource(session: Session, resource_id: str, kind: str) -> None:
    upsert_resource(
        session, resource_id=resource_id, kind=kind, status="running", seen_at=sc.NOW
    )
    session.commit()


def seed_alert(
    session: Session,
    alert_id: str,
    *,
    rule_id: str = "R-CTR-OOM-010",
    state: str = AlertState.FIRING.value,
    last_fired_at=None,
    count: int = 1,
    labels: dict | None = None,
    evidence: list[str] | None = None,
    resource_id: str = CONTAINER_ID,
) -> str:
    """直接在**引擎可见的 session** 里插一条告警。

    为什么需要它：通过 API 写入的行是在请求处理器的 session 里提交的，而
    `reconcile` / `release_expired_silences` 用的是 `db_session` —— 两者是
    不同的 session，后者看不到前者提交的行。要让引擎的行为可测，就必须让
    被测数据出现在引擎的 session 里。
    """
    moment = last_fired_at or sc.NOW
    session.add(
        Alert(
            id=alert_id,
            rule_id=rule_id,
            resource_id=resource_id,
            severity=Severity.CRITICAL.value,
            state=state,
            first_fired_at=moment,
            last_fired_at=moment,
            count=count,
            evidence_event_ids=evidence or [f"evt_{alert_id}"],
            # 聚合键必须与将要上报的场景事件**不同**，否则引擎会认为这是
            # "已有活动告警"并累加到它上面（推进 last_fired_at），
            # 于是"陈旧的告警"在测试里变得不陈旧 —— 引擎是对的，是夹具造了冲突。
            aggregation_key=f"{rule_id}:{resource_id}",
            title="容器被 OOM Killer 终止",
            labels=labels or {},
        )
    )
    session.flush()
    return alert_id


def make_event(
    event_id: str,
    *,
    resource_id: str = CONTAINER_ID,
    event_type: str = "container.oom_killed",
    metrics: dict | None = None,
    severity: str = Severity.CRITICAL.value,
) -> Event:
    return Event(
        id=event_id,
        occurred_at=sc.NOW,
        received_at=sc.NOW,
        source="probe",
        resource_id=resource_id,
        type=event_type,
        severity=severity,
        metrics=metrics or {},
        raw={},
    )


# ================================================================ 规则集自检


class TestRuleSet:
    def test_default_rule_set_is_consistent(self) -> None:
        """规则是**数据**，所以数据也要有校验（对齐 `assert_enums_consistent`）。"""
        assert_rules_are_consistent(build_default_rule_set())

    def test_default_rule_set_covers_all_three_scenarios(self) -> None:
        """赛题明文要求三类场景，覆盖情况必须可一眼核对。"""
        by_type = {r.condition.event_type for r in build_default_rule_set().rules}
        assert {
            "gpu.memory.exhausted",
            "gpu.utilization.high",
            "container.oom_killed",
            "container.restart",
            "process.crash",
            "inference.timeout",
            "inference.error",
            "agent.task.failed",
            "container.network.unreachable",
        } <= by_type

    def test_default_rule_set_covers_platform_self_monitoring(self) -> None:
        """平台自己坏了却不报警，比漏报业务告警更严重。"""
        by_type = {r.condition.event_type for r in build_default_rule_set().rules}
        assert {
            "ingest.clock_drift.high",
            "zsvirt.sync.failed",
            "gpu.provider.degraded",
        } <= by_type

    def test_unknown_event_type_is_rejected(self) -> None:
        bad = RuleSet(
            version="rs-bad",
            rules=(
                AlertRule(
                    id="R-BAD",
                    title="x",
                    severity=Severity.WARNING.value,
                    condition=AlertCondition(event_type="not.a.real.type"),
                ),
            ),
        )
        with pytest.raises(AssertionError, match="未知事件类型"):
            assert_rules_are_consistent(bad)

    def test_rule_without_any_condition_is_rejected(self) -> None:
        """没有条件的规则会匹配一切 —— 这类规则集不能被放进生产。"""
        bad = RuleSet(
            version="rs-bad",
            rules=(
                AlertRule(
                    id="R-CATCHALL",
                    title="x",
                    severity=Severity.INFO.value,
                    condition=AlertCondition(),
                ),
            ),
        )
        with pytest.raises(AssertionError, match="所有"):
            assert_rules_are_consistent(bad)

    def test_duplicate_rule_id_is_rejected(self) -> None:
        rule = AlertRule(
            id="R-DUP",
            title="x",
            severity=Severity.WARNING.value,
            condition=AlertCondition(event_type="container.restart"),
        )
        with pytest.raises(AssertionError, match="规则 id 重复"):
            assert_rules_are_consistent(RuleSet(version="rs", rules=(rule, rule)))

    def test_unfrozen_root_cause_hint_is_rejected(self) -> None:
        bad = RuleSet(
            version="rs-bad",
            rules=(
                AlertRule(
                    id="R-BAD2",
                    title="x",
                    severity=Severity.WARNING.value,
                    condition=AlertCondition(event_type="container.restart"),
                    root_cause_hint="NOT_A_FROZEN_CODE",
                ),
            ),
        )
        with pytest.raises(AssertionError, match="根因码"):
            assert_rules_are_consistent(bad)

    def test_unknown_resource_kind_is_rejected(self) -> None:
        bad = RuleSet(
            version="rs-bad",
            rules=(
                AlertRule(
                    id="R-BAD3",
                    title="x",
                    severity=Severity.WARNING.value,
                    condition=AlertCondition(
                        event_type="container.restart", resource_kinds=("kubernetes",)
                    ),
                ),
            ),
        )
        with pytest.raises(AssertionError, match="未知资源类型"):
            assert_rules_are_consistent(bad)


# ================================================================ 场景端到端


class TestScenarios:
    """标准 1：给定场景事件序列，产生**预期数量**的告警。"""

    def test_scenario_one_produces_gpu_and_inference_alerts(
        self, client: TestClient
    ) -> None:
        summary = ingest(client, sc.scenario_gpu_memory_exhausted())["alerts"]
        assert summary["created"] == 2, summary
        assert summary["rulesEvaluated"] == len(build_default_rule_set().rules)
        assert summary["ruleSetVersion"] == DEFAULT_RULE_SET_VERSION

        assert {a["ruleId"] for a in alerts_of(client)} == {
            "R-GPU-MEM-001",
            "R-AIS-TIMEOUT-020",
        }

    def test_scenario_two_produces_oom_and_restart_alerts(self, client: TestClient) -> None:
        summary = ingest(client, sc.scenario_container_oom())["alerts"]
        assert summary["created"] == 2, summary
        assert {a["ruleId"] for a in alerts_of(client)} == {
            "R-CTR-OOM-010",
            "R-CTR-RESTART-011",
        }

    def test_scenario_three_produces_network_alerts(self, client: TestClient) -> None:
        summary = ingest(client, sc.scenario_network_failure())["alerts"]
        assert summary["created"] == 2, summary
        assert {a["ruleId"] for a in alerts_of(client)} == {
            "R-NET-UNREACH-023",
            "R-AGENT-TASK-022",
        }

    def test_alert_carries_rule_and_hint(self, client: TestClient) -> None:
        ingest(client, sc.scenario_gpu_memory_exhausted())
        alert = next(a for a in alerts_of(client) if a["ruleId"] == "R-GPU-MEM-001")

        assert alert["severity"] == Severity.CRITICAL.value
        assert alert["state"] == AlertState.FIRING.value
        assert alert["title"], "告警必须有人能看懂的标题"
        assert alert["count"] >= 1
        # 规则集版本随告警落库 —— 规则改动后必须能回答"当时按什么规则报的"
        assert alert["labels"]["ruleSetVersion"] == DEFAULT_RULE_SET_VERSION
        assert alert["labels"]["rootCauseHint"] == "GPU_MEMORY_EXHAUSTED"
        assert alert["labels"]["sourceEventType"] == "gpu.memory.exhausted"

    def test_alert_lands_on_the_right_resource(self, client: TestClient) -> None:
        """告警必须挂在**证据所在**的资源上，而不是随便一个链路成员。

        挂错资源的后果是运维去检查一台没有问题的机器。
        """
        ingest(client, sc.scenario_gpu_memory_exhausted())
        by_rule = {a["ruleId"]: a for a in alerts_of(client)}

        assert by_rule["R-GPU-MEM-001"]["resourceId"].startswith("vgpu:")
        assert by_rule["R-AIS-TIMEOUT-020"]["resourceId"].startswith("ai_service:")

    def test_ingest_without_alerts_reports_zero(self, client: TestClient) -> None:
        """摘要字段必须恒在 —— 前端与探针都不该处理"字段有时没有"的情况。"""
        summary = ingest(client, sc.scenario_benign())["alerts"]
        for key in (
            "ruleSetVersion",
            "rulesEvaluated",
            "eventsEvaluated",
            "created",
            "updated",
            "skippedSilenced",
            "truncated",
        ):
            assert key in summary, f"告警摘要缺少 {key}"


# ================================================================ 聚合去重


class TestAggregation:
    """标准 3：重复触发只累加 `count`，**不新建**告警。"""

    def test_second_batch_updates_instead_of_creating(self, client: TestClient) -> None:
        first = ingest(client, sc.scenario_container_oom(at=sc.NOW))["alerts"]
        assert first["created"] == 2

        second = ingest(
            client, sc.scenario_container_oom(at=sc.NOW + timedelta(minutes=1))
        )["alerts"]
        assert second["created"] == 0, "不应新建告警"
        assert second["updated"] == 2, "应累加到已有告警上"
        assert len(alerts_of(client)) == 2, "告警总数不变"

    def test_repeat_increments_count_and_advances_last_fired(
        self, client: TestClient
    ) -> None:
        ingest(client, sc.scenario_container_oom(at=sc.NOW))
        later = sc.NOW + timedelta(minutes=1)
        ingest(client, sc.scenario_container_oom(at=later))

        alert = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")
        assert alert["count"] == 2, "两次触发 → count=2"
        # `lastFiredAt` 必须推进，否则"最近还在报的"排序会骗人
        assert datetime.fromisoformat(alert["lastFiredAt"]) == later

    def test_evidence_accumulates_without_duplicates(self, client: TestClient) -> None:
        """历史证据必须保留（诊断要看到整条证据链），且不得重复。"""
        ingest(client, sc.scenario_container_oom(at=sc.NOW))
        ingest(client, sc.scenario_container_oom(at=sc.NOW + timedelta(minutes=1)))

        alert = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")
        evidence = alert["evidenceEventIds"]
        assert len(evidence) == 2, evidence
        assert len(set(evidence)) == 2, "证据不得重复"

    def test_different_resources_are_different_alerts(self, client: TestClient) -> None:
        """聚合键含资源 id —— 两个容器 OOM 是两条告警，不是一条 count=2。"""
        payload = sc.scenario_container_oom(at=sc.NOW)
        payload["resources"].append(
            {
                "kind": ResourceKind.CONTAINER.value,
                "sourceId": "worker-0",
                "name": "worker-0",
                # 兄弟容器引用 VM 时必须用**探针本地别名** `self`。
                # 探针资源在 `{kind}:probe:{agentId}:{sourceId}` 命名空间里，
                # 父资源按同一命名空间解析，所以 `vm0`（ZSvirt 的 uuid）会解析成
                # `vm:probe:probe-x:vm0` —— 一个不存在的资源，整批因外键违约失败。
                # 这也是一条真实的契约约束：一个 agent 只报一台 VM，
                # 它无法把别的 VM 报成自己的父节点。
                "parentSourceId": "self",
                "status": "running",
            }
        )
        payload["events"].append(
            {
                "occurredAt": sc.NOW.isoformat(),
                "resourceRef": {"kind": ResourceKind.CONTAINER.value, "sourceId": "worker-0"},
                "type": "container.oom_killed",
                "metrics": {},
            }
        )
        summary = ingest(client, payload)["alerts"]
        assert summary["created"] == 3, "两个容器各一条 OOM + 一条重启"

    def test_count_reflects_number_of_matching_events(self, client: TestClient) -> None:
        """同一批里同一规则命中 3 条事件 → `count` 记 3，而不是 1。

        "重启了 3 次"和"重启了 1 次"是不同的信息量。
        """
        payload = sc.scenario_container_oom(at=sc.NOW)
        payload["events"] = [
            {
                "occurredAt": (sc.NOW + timedelta(seconds=i)).isoformat(),
                "resourceRef": {"kind": ResourceKind.CONTAINER.value, "sourceId": "web-0"},
                "type": "container.restart",
                "metrics": {"restartCount": i + 1},
            }
            for i in range(3)
        ]
        summary = ingest(client, payload)["alerts"]
        assert summary["created"] == 1

        alert = alerts_of(client)[0]
        # 证据 3 条全在
        assert len(alert["evidenceEventIds"]) == 3
        # `count` 起点为 1（第一次触发）再加新增证据 2 条
        assert alert["count"] == 3


# ================================================================ 证据绑定


class TestEvidenceBinding:
    """标准 2：`evidenceEventIds` 非空且指向**真实**事件。"""

    def test_evidence_points_at_real_events(self, client: TestClient) -> None:
        ingest(client, sc.scenario_gpu_memory_exhausted())

        for alert in alerts_of(client):
            assert alert["evidenceEventIds"], "红线：没有证据的告警不允许存在"
            for event_id in alert["evidenceEventIds"]:
                r = client.get(f"/api/v1/events/{event_id}")
                assert r.status_code == 200, f"证据指向不存在的事件 {event_id}"

    def test_evidence_expands_to_matching_event_type(self, client: TestClient) -> None:
        """证据展开后的事件类型应与规则一致 —— 证明采纳的是"对的那条"。"""
        ingest(client, sc.scenario_container_oom())
        alert = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")

        r = client.get(f"/api/v1/alerts/{alert['id']}/evidence")
        assert r.status_code == 200, r.text
        assert [e["type"] for e in r.json()["data"]["events"]] == ["container.oom_killed"]

    def test_reverse_lookup_from_event_to_alert(self, client: TestClient) -> None:
        """从一条事件反查"它被哪些告警采纳" —— 可解释性的反向链路。"""
        ingest(client, sc.scenario_container_oom())
        alert = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")
        event_id = alert["evidenceEventIds"][0]

        r = client.get(f"/api/v1/events/{event_id}/related-alerts")
        assert r.status_code == 200, r.text
        assert [a["id"] for a in r.json()["data"]["alerts"]] == [alert["id"]]

    def test_engine_refuses_to_build_alert_without_evidence(
        self, client: TestClient, db_session: Session
    ) -> None:
        """无 id 的事件 → **抛错**，而不是静默丢弃。

        `Event.id` 由写入方生成且有非空约束，所以"事件在、但没有 id"只可能来自
        绕过 ORM 的调用方。静默丢弃会把"这条告警为什么没产生"变成一个查不出来的谜，
        因此这里选择让调用方立刻失败。
        """
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)

        orphan = make_event("")  # 空 id：手工构造的非法输入
        with pytest.raises(ValueError, match="没有 id"):
            evaluate_events(db_session, [orphan], now=sc.NOW)

    def test_database_rejects_alert_without_evidence(self, db_session: Session) -> None:
        """红线在**数据库层**也强制（`ck_alert_evidence_required`）。

        这一条比引擎里的检查更重要：即使将来有人绕过引擎直接写库，
        空证据的告警依然进不去。
        """
        from sqlalchemy.exc import IntegrityError

        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        db_session.add(
            Alert(
                id="alert_no_evidence",
                rule_id="R-CTR-OOM-010",
                resource_id=CONTAINER_ID,
                severity=Severity.CRITICAL.value,
                state=AlertState.FIRING.value,
                first_fired_at=sc.NOW,
                last_fired_at=sc.NOW,
                count=1,
                evidence_event_ids=[],
                aggregation_key="R-CTR-OOM-010:" + CONTAINER_ID,
            )
        )
        with pytest.raises(IntegrityError, match="ck_alert_evidence_required"):
            db_session.flush()
        db_session.rollback()


# ================================================================ 负向


class TestNegativeCases:
    """标准 6：不该报的时候**一条都不能报**。"""

    def test_benign_events_produce_no_alerts(self, client: TestClient) -> None:
        summary = ingest(client, sc.scenario_benign())["alerts"]
        assert summary["created"] == 0
        assert summary["updated"] == 0
        assert alerts_of(client) == []

    def test_event_on_wrong_resource_layer_produces_no_alert(
        self, client: TestClient
    ) -> None:
        """容器事件落在 VM 上 → 规则里的 `resource_kinds` 必须拦住它。

        放行的后果是前端把"容器 OOM"显示在一台虚拟机上，运维会查错对象。
        """
        summary = ingest(client, sc.scenario_wrong_layer())["alerts"]
        assert summary["created"] == 0, summary
        assert alerts_of(client) == []

    def test_empty_event_list_is_not_an_error(self, client: TestClient) -> None:
        summary = ingest(client, sc.batch([]))["alerts"]
        assert summary["created"] == 0
        assert summary["eventsEvaluated"] == 0

    def test_empty_rule_set_evaluates_cleanly(
        self, client: TestClient, db_session: Session
    ) -> None:
        """空规则集是**合法**的：如实报告求值了 0 条规则，而不是报错。

        演示时用它证明"告警全部来自规则数据，没有硬编码"。
        """
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        event = make_event("evt_empty_ruleset")
        db_session.add(event)
        db_session.flush()

        result = evaluate_events(db_session, [event], rule_set=RuleSet(version="rs-empty"))
        assert result.created == []
        assert result.rules_evaluated == 0
        assert result.events_evaluated == 1

    def test_unknown_event_type_produces_no_alert(self, client: TestClient) -> None:
        payload = sc.batch(
            [
                {
                    "occurredAt": sc.NOW.isoformat(),
                    "resourceRef": {"kind": ResourceKind.CONTAINER.value, "sourceId": "web-0"},
                    "type": "something.brand.new",
                    "metrics": {},
                }
            ]
        )
        assert ingest(client, payload)["alerts"]["created"] == 0

    def test_metric_threshold_below_value_does_not_fire(
        self, client: TestClient, db_session: Session
    ) -> None:
        """阈值条件真的在比较数值：80% 不触发，98% 触发。"""
        seed_resource(db_session, GPU_ID, ResourceKind.GPU.value)

        low = make_event(
            "evt_low",
            resource_id=GPU_ID,
            event_type="gpu.utilization.high",
            metrics={"gpu_memory_usage": "80%"},
            severity=Severity.WARNING.value,
        )
        db_session.add(low)
        db_session.flush()

        rule = AlertRule(
            id="R-METRIC-001",
            title="GPU 显存高",
            severity=Severity.WARNING.value,
            condition=AlertCondition(
                event_type="gpu.utilization.high",
                metric=MetricThreshold("gpu_memory_usage", ">=", 95),
            ),
        )
        rule_set = RuleSet(version="rs-metric", rules=(rule,))

        assert evaluate_events(db_session, [low], rule_set=rule_set).created == []

        high = make_event(
            "evt_high",
            resource_id=GPU_ID,
            event_type="gpu.utilization.high",
            metrics={"gpuMemoryUsage": 98},  # 顺带验证 camelCase 也能取到
            severity=Severity.WARNING.value,
        )
        db_session.add(high)
        db_session.flush()
        assert len(evaluate_events(db_session, [high], rule_set=rule_set).created) == 1

    def test_min_severity_is_enforced(
        self, client: TestClient, db_session: Session
    ) -> None:
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        quiet = make_event("evt_info", severity=Severity.INFO.value)
        db_session.add(quiet)
        db_session.flush()

        rule = AlertRule(
            id="R-SEV-001",
            title="容器 OOM（仅 error 以上）",
            severity=Severity.CRITICAL.value,
            condition=AlertCondition(
                event_type="container.oom_killed", min_severity=Severity.ERROR.value
            ),
        )
        rule_set = RuleSet(version="rs-sev", rules=(rule,))
        assert evaluate_events(db_session, [quiet], rule_set=rule_set).created == []


# ================================================================ 静默与恢复


class TestSilence:
    """标准 4：静默期内不产生新告警；到期后恢复。"""

    def test_silenced_alert_is_not_reopened_by_new_evidence(
        self, client: TestClient
    ) -> None:
        ingest(client, sc.scenario_container_oom(at=sc.NOW))
        alert = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")

        r = client.post(
            f"/api/v1/alerts/{alert['id']}/actions",
            json={"action": "silence", "silenceSeconds": 3600},
        )
        assert r.status_code == 200, r.text

        summary = ingest(
            client, sc.scenario_container_oom(at=sc.NOW + timedelta(minutes=1))
        )["alerts"]
        assert summary["created"] == 0, "静默期内不得新建告警"
        assert summary["skippedSilenced"] == 1, "应报告因静默而只更新了证据"

        after = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")
        assert after["state"] == AlertState.SILENCED.value, "静默状态不得被新证据冲掉"
        assert after["count"] == 2, "证据仍要累加 —— 静默不是停止观测"

    def test_expired_silence_returns_to_firing(
        self, client: TestClient, db_session: Session
    ) -> None:
        """静默到期后应能再次打扰运维。

        数据种在 `db_session` 里 —— 引擎用同一个 session 查询，才看得见它。
        （通过 API 写入的行在请求处理器的 session 里，引擎看不到；
        第一版就是这么写的，于是 `released` 永远是空列表。）
        """
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        alert_id = seed_alert(
            db_session,
            "alert_silenced_expired",
            state=AlertState.SILENCED.value,
            labels={
                "silencedUntil": (
                    sc.NOW + timedelta(minutes=1)
                ).isoformat()
            },
            last_fired_at=sc.NOW - timedelta(minutes=1),
        )
        db_session.commit()

        released = release_expired_silences(db_session, now=sc.NOW + timedelta(minutes=5))
        db_session.commit()

        assert released == [alert_id]
        after = next(a for a in alerts_of(client) if a["id"] == alert_id)
        assert after["state"] == AlertState.FIRING.value

    def test_silence_does_not_expire_early(
        self, client: TestClient, db_session: Session
    ) -> None:
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        alert_id = seed_alert(
            db_session,
            "alert_silenced_active",
            state=AlertState.SILENCED.value,
            labels={"silencedUntil": (sc.NOW + timedelta(minutes=10)).isoformat()},
            last_fired_at=sc.NOW,
        )
        db_session.commit()

        released = release_expired_silences(db_session, now=sc.NOW + timedelta(minutes=1))
        assert released == []
        after = next(a for a in alerts_of(client) if a["id"] == alert_id)
        assert after["state"] == AlertState.SILENCED.value

    def test_silenced_alert_is_not_auto_resolved(
        self, client: TestClient, db_session: Session
    ) -> None:
        """静默 ≠ 已解决。把静默当"自动关单"会让仍在发生的故障被关掉。"""
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        alert_id = seed_alert(
            db_session,
            "alert_silenced_stale",
            state=AlertState.SILENCED.value,
            labels={"silencedUntil": (sc.NOW + timedelta(hours=2)).isoformat()},
            last_fired_at=sc.NOW - timedelta(hours=1),
        )
        db_session.commit()

        result = reconcile(db_session, now=sc.NOW + timedelta(hours=1))
        db_session.commit()

        assert result.resolved == []
        after = next(a for a in alerts_of(client) if a["id"] == alert_id)
        assert after["state"] == AlertState.SILENCED.value


class TestRecovery:
    """标准 5：条件恢复后转 `resolved` 且写入 `resolvedAt`。"""

    def test_stale_alert_is_resolved(self, client: TestClient, db_session: Session) -> None:
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        stale = seed_alert(
            db_session,
            "alert_stale",
            rule_id="R-CTR-OOM-010",
            last_fired_at=sc.NOW - timedelta(minutes=30),
        )
        fresh = seed_alert(
            db_session,
            "alert_fresh",
            rule_id="R-CTR-RESTART-011",
            last_fired_at=sc.NOW - timedelta(minutes=1),
        )
        db_session.commit()

        result = reconcile(db_session, now=sc.NOW)
        db_session.commit()

        assert result.resolved == [stale], "只应恢复超过恢复窗口的那条"
        # `checked` 是**候选数**：`fresh` 还在恢复窗口内，根本不是候选，
        # 因此这里是 1 而不是 2。
        assert result.checked == 1

        resolved = {a["id"]: a for a in alerts_of(client, state="resolved")}
        assert stale in resolved
        assert resolved[stale]["resolvedAt"] is not None, "必须写入 resolvedAt"
        assert fresh not in resolved, "证据刚来过，不得判定恢复"

    def test_fresh_alert_is_not_resolved(
        self, client: TestClient, db_session: Session
    ) -> None:
        """证据还在来，就不能判定恢复。"""
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        seed_alert(db_session, "alert_fresh_only", last_fired_at=sc.NOW)
        db_session.commit()

        result = reconcile(db_session, now=sc.NOW + timedelta(minutes=1))
        assert result.resolved == []
        assert result.checked == 0

    def test_resolved_can_fire_again_as_a_new_alert(
        self, client: TestClient, db_session: Session
    ) -> None:
        """复发应产生**新**告警，而不是复活旧记录。

        复活会抹掉旧记录曾经的 `resolvedAt`，"复发"与"从未恢复"就再也分不清。
        """
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        old_id = seed_alert(
            db_session,
            "alert_resolved_history",
            state=AlertState.RESOLVED.value,
            last_fired_at=sc.NOW - timedelta(minutes=30),
        )
        # 给它一个真实的恢复时刻：引擎**不该**碰已恢复的记录，
        # 所以这里断言的是"引擎没有改动它"，而不是"引擎补上了它"。
        resolved_row = db_session.get(Alert, old_id)
        resolved_row.resolved_at = sc.NOW - timedelta(minutes=20)
        db_session.commit()

        # 同一条聚合键再次触发（同一容器、同一规则）
        summary = ingest(client, sc.scenario_container_oom(at=sc.NOW))["alerts"]
        new_ids = {a["id"] for a in alerts_of(client, state="firing")}
        assert old_id not in new_ids, "不得复活已恢复的历史记录"
        assert summary["created"] >= 1

        oom = next(
            a for a in alerts_of(client, state="firing") if a["ruleId"] == "R-CTR-OOM-010"
        )
        assert oom["id"] != old_id

        # 历史那条仍保留**原有的**恢复时刻（引擎不得改写已恢复的记录）
        history = next(a for a in alerts_of(client, state="resolved") if a["id"] == old_id)
        assert history["resolvedAt"] == (sc.NOW - timedelta(minutes=20)).isoformat()

    def test_acked_alert_is_resolved_when_evidence_stops(
        self, client: TestClient, db_session: Session
    ) -> None:
        """已确认 ≠ 还在发生。证据停了就该关掉。"""
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        alert_id = seed_alert(
            db_session,
            "alert_acked_stale",
            state=AlertState.ACKED.value,
            last_fired_at=sc.NOW - timedelta(minutes=30),
        )
        db_session.commit()

        result = reconcile(db_session, now=sc.NOW)
        db_session.commit()

        assert result.resolved == [alert_id]

    def test_reconcile_all_reports_both_phases_separately(
        self, client: TestClient, db_session: Session
    ) -> None:
        """两个阶段**分开报告**，且静默刚到期的那条不会被顺带判为恢复。

        若把两者合并成一个动作，刚从静默中释放的告警会立刻因为"静默期间没有
        新证据"而被关闭 —— 那等于把静默偷偷变成自动关单。
        """
        seed_resource(db_session, CONTAINER_ID, ResourceKind.CONTAINER.value)
        silenced = seed_alert(
            db_session,
            "alert_silence_just_expired",
            rule_id="R-CTR-RESTART-011",
            state=AlertState.SILENCED.value,
            labels={"silencedUntil": (sc.NOW + timedelta(minutes=1)).isoformat()},
            # 静默期内没有新证据，所以 last_fired_at 很旧
            last_fired_at=sc.NOW - timedelta(minutes=30),
        )
        stale = seed_alert(
            db_session,
            "alert_plain_stale",
            rule_id="R-PROC-CRASH-012",
            last_fired_at=sc.NOW - timedelta(minutes=30),
        )
        db_session.commit()

        summary = reconcile_all(db_session, now=sc.NOW + timedelta(minutes=5))
        db_session.commit()

        assert summary["released"] == [silenced]
        assert summary["recovery"]["resolved"] == [stale], (
            "刚释放的静默告警不应在同一次调用里被判为已恢复 —— "
            "静默期内没有证据是「我们没在听」，不能当作「问题已消失」"
        )
        assert summary["recovery"]["checked"] == 1, "刚释放的那条不是恢复候选"

        after = next(a for a in alerts_of(client) if a["id"] == silenced)
        assert after["state"] == AlertState.FIRING.value
        assert after["lastFiredAt"] == (sc.NOW + timedelta(minutes=5)).isoformat(), (
            "恢复窗口必须从释放时刻重新计时"
        )


# ================================================================ 严重级别


class TestSeverity:
    def test_event_severity_wins_when_worse(self, client: TestClient) -> None:
        """规则说 warning、事件报 critical → 取 critical。

        降级显示会让真正紧急的信号看起来不紧急。
        """
        ingest(
            client,
            sc.single_event_batch(
                event_type="container.restart", severity=Severity.CRITICAL.value
            ),
        )
        alert = alerts_of(client)[0]
        assert alert["ruleId"] == "R-CTR-RESTART-011"
        assert alert["severity"] == Severity.CRITICAL.value

    def test_rule_severity_wins_when_event_is_lower(self, client: TestClient) -> None:
        """事件报 info，但规则认定这属于严重问题 → 保持规则的级别。"""
        ingest(
            client,
            sc.single_event_batch(
                event_type="container.oom_killed", severity=Severity.INFO.value
            ),
        )
        assert alerts_of(client)[0]["severity"] == Severity.CRITICAL.value


# ================================================================ 幂等


class TestIdempotency:
    def test_duplicate_batch_does_not_inflate_count(self, client: TestClient) -> None:
        """重复投递同一 `batchId` **不得**让 `count` 灌水（A 的 Q2）。

        这是"事件到达即求值"最容易出错的地方：若对重放的事件再求值一次，
        `count` 会随重试次数增长，"这个故障触发了几次"就变成假数据。
        探针在弱网下重试是常态，所以这条不是理论风险。
        """
        payload = sc.scenario_container_oom(at=sc.NOW)

        first = ingest(client, payload)
        assert first["alerts"]["created"] == 2
        before = {a["id"]: a["count"] for a in alerts_of(client)}

        replay = ingest(client, payload)  # 同一个 batchId
        assert replay["duplicate"] is True
        # 回放的是**首次结果的缓存**，因此摘要与首次完全一致 ——
        # 这正是幂等要保证的"重复批次返回与首次一致的结果"。
        assert replay["alerts"] == first["alerts"]

        after = {a["id"]: a["count"] for a in alerts_of(client)}
        assert after == before, "重复投递不得新建告警，也不得累加 count"
        assert all(count == 1 for count in after.values())
