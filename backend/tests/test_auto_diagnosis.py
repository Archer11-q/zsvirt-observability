"""告警 → 诊断自动联动测试（`docs/DEVELOPMENT_PLAN.md` 任务 3，C 的 Q13）。

自动路径的验收重点是**它不该做什么**：

| 场景 | 期望 |
|---|---|
| 严重告警（error 以上）产生 | 自动诊断一次并回写 `alert.diagnosisId` |
| 提示级告警（info / warning）产生 | **不**自动诊断（避免灌一堆 `UNKNOWN`） |
| 同一条告警收到新证据 | **不**重复诊断（幂等） |
| 已恢复 / 已静默的告警 | 不诊断 |
| 诊断过程中抛错 | 告警本身仍存在，失败逐条记录 |
| 一次产生大量告警 | 受上限保护，并置 `truncated` |
| 重复批次 | 不触发任何新诊断 |

另外还有一条端到端断言：诊断与告警在同一次上报响应里就能看到关联，
运维不需要再点一次"一键诊断"。
"""

from __future__ import annotations

from datetime import timedelta

import _scenarios as sc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.diagnosis.auto import DEFAULT_MAX_DIAGNOSES, run_auto_diagnosis
from app.enums import AlertState, ResourceKind, Severity
from app.graph.repository import upsert_resource
from app.models import Alert, Diagnosis

CONTAINER_ID = "container:probe:probe-x:web-0"


def ingest(client: TestClient, payload: dict) -> dict:
    r = client.post("/api/v1/ingest/batch", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def alerts_of(client: TestClient, **params) -> list[dict]:
    r = client.get("/api/v1/alerts", params={"limit": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()["data"]["items"]


def seed_alert(
    session: Session,
    alert_id: str,
    *,
    severity: str = Severity.CRITICAL.value,
    state: str = AlertState.FIRING.value,
    diagnosis_id: str | None = None,
    resource_id: str = CONTAINER_ID,
) -> str:
    session.add(
        Alert(
            id=alert_id,
            rule_id="R-CTR-OOM-010",
            resource_id=resource_id,
            severity=severity,
            state=state,
            first_fired_at=sc.NOW,
            last_fired_at=sc.NOW,
            count=1,
            evidence_event_ids=[f"evt_{alert_id}"],
            aggregation_key=f"R-CTR-OOM-010:{resource_id}:{alert_id}",
            title="容器被 OOM Killer 终止",
            labels={},
            diagnosis_id=diagnosis_id,
        )
    )
    session.flush()
    return alert_id


class TestAutomaticLinkage:
    def test_critical_alert_gets_diagnosed_on_ingest(self, client: TestClient) -> None:
        """端到端：上报一次容器 OOM，响应里就能看到告警已带上诊断结论。"""
        data = ingest(client, sc.scenario_container_oom(at=sc.NOW))

        auto = data["alerts"]["autoDiagnosis"]
        assert auto["attempted"] == 1, "只有 critical 的 OOM 告警应被自动诊断"
        assert len(auto["linked"]) == 1

        alert = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")
        assert alert["diagnosisId"] is not None
        assert alert["diagnosisId"] == next(iter(auto["linked"].values()))

        # 诊断本身必须可查、形态正确
        diag = client.get(f"/api/v1/diagnosis/{alert['diagnosisId']}").json()["data"]
        # 子集断言：聚合后的 trigger 还会带上 `alertIds` / `clusterSize`，
        # 让"这条结论关联了哪些告警"事后可查。本用例只关心告警 id 在不在。
        assert diag["trigger"]["alertId"] == alert["id"]
        assert alert["id"] in diag["trigger"]["alertIds"]
        assert diag["ruleSetVersion"], "结论必须带规则集版本"

        # **具体根因**，不再是 UNKNOWN。
        #
        # 此前这里断言的是 `UNKNOWN`（规则集为空时的正确行为）。规则集落地后，
        # 诚实的期望值是对应场景的具体根因 —— 保留 UNKNOWN 断言会掩盖
        # "规则集写坏了导致永不命中"这类回归。
        assert diag["rootCause"] == "CONTAINER_MEMORY_LIMIT"
        assert diag["confidence"] >= 0.30, "有规则的结论必须过最小置信阈值"
        assert diag["confidenceBreakdown"], "根因必须能列出支撑它的规则贡献"
        assert diag["evidence"], "根因必须有证据（红线）"

    def test_warning_alert_is_not_auto_diagnosed(self, client: TestClient) -> None:
        """提示级告警不自动诊断：自动跑根因分析只会灌出一堆 `UNKNOWN`，
        而 `UNKNOWN` 太多会让运维学会忽略整个诊断列表。"""
        data = ingest(client, sc.scenario_container_oom(at=sc.NOW))
        auto = data["alerts"]["autoDiagnosis"]

        assert auto["skippedBelowSeverity"] == 1
        restart = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-RESTART-011")
        assert restart["diagnosisId"] is None
        # 用户仍可手动补上
        r = client.post("/api/v1/diagnoses", json={"alertId": restart["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["id"] is not None

    def test_repeat_evidence_does_not_re_diagnose(self, client: TestClient) -> None:
        """幂等：一条持续告警收到新证据时不得重复诊断。

        否则一条持续数小时的告警会按上报频率产出成百上千条几乎相同的结论。
        """
        ingest(client, sc.scenario_container_oom(at=sc.NOW))
        first = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")
        assert first["diagnosisId"] is not None

        second = ingest(
            client, sc.scenario_container_oom(at=sc.NOW + timedelta(minutes=1))
        )

        assert second["alerts"]["created"] == 0, "不应新建告警"
        auto = second["alerts"]["autoDiagnosis"]
        assert auto["linked"] == {}, "不得产生第二条诊断"
        assert auto["skippedAlreadyDiagnosed"] == 1

        after = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")
        assert after["diagnosisId"] == first["diagnosisId"], "关联不得被改写"

        # 诊断表里应只有一条
        listed = client.get("/api/v1/diagnoses").json()["data"]["items"]
        assert len(listed) == 1

    def test_duplicate_batch_triggers_no_diagnosis(self, client: TestClient) -> None:
        payload = sc.scenario_container_oom(at=sc.NOW)
        ingest(client, payload)
        before = len(client.get("/api/v1/diagnoses").json()["data"]["items"])

        replay = ingest(client, payload)
        assert replay["duplicate"] is True

        after = len(client.get("/api/v1/diagnoses").json()["data"]["items"])
        assert after == before

    def test_benign_batch_reports_no_auto_diagnosis(self, client: TestClient) -> None:
        """没有告警时不应出现 `autoDiagnosis` 字段 —— 报告"做了 0 件事"是噪声。"""
        data = ingest(client, sc.scenario_benign())
        assert "autoDiagnosis" not in data["alerts"]

    def test_response_does_not_leak_internal_alert_ids(self, client: TestClient) -> None:
        """`createdAlertIds` 是进程内字段，不得出现在响应里。"""
        data = ingest(client, sc.scenario_container_oom(at=sc.NOW))
        assert "createdAlertIds" not in data
        assert "createdAlertIds" not in data["alerts"]


class TestAutoDiagnosisGuards:
    """直接调 `run_auto_diagnosis`，覆盖 API 不方便构造的守卫分支。"""

    def test_resolved_alert_is_skipped(self, client: TestClient, db_session: Session) -> None:
        upsert_resource(
            db_session, resource_id=CONTAINER_ID, kind=ResourceKind.CONTAINER.value,
            status="running", seen_at=sc.NOW,
        )
        alert_id = seed_alert(db_session, "alert_resolved", state=AlertState.RESOLVED.value)
        db_session.commit()

        result = run_auto_diagnosis(db_session, [alert_id], now=sc.NOW)
        assert result.skipped_not_firing == [alert_id]
        assert result.attempted == []

    def test_silenced_alert_is_skipped(self, client: TestClient, db_session: Session) -> None:
        """静默是"别打扰我"，其中也包括别自动跑分析。"""
        upsert_resource(
            db_session, resource_id=CONTAINER_ID, kind=ResourceKind.CONTAINER.value,
            status="running", seen_at=sc.NOW,
        )
        alert_id = seed_alert(db_session, "alert_silenced", state=AlertState.SILENCED.value)
        db_session.commit()

        result = run_auto_diagnosis(db_session, [alert_id], now=sc.NOW)
        assert result.skipped_not_firing == [alert_id]

    def test_existing_diagnosis_is_respected(
        self, client: TestClient, db_session: Session
    ) -> None:
        upsert_resource(
            db_session, resource_id=CONTAINER_ID, kind=ResourceKind.CONTAINER.value,
            status="running", seen_at=sc.NOW,
        )
        db_session.add(
            Diagnosis(
                id="diag_preexisting",
                created_at=sc.NOW,
                trigger={"alertId": "alert_have_diag"},
                root_cause="CONTAINER_MEMORY_LIMIT",
                confidence=0.8,
                confidence_breakdown=[],
                affected_resources=[CONTAINER_ID],
                potentially_affected=[],
                on_chain=[],
                evidence=[{"type": "event", "name": "container.oom_killed"}],
                recommendation=[],
                rule_set_version="rs-test",
                notes=[],
            )
        )
        alert_id = seed_alert(db_session, "alert_have_diag", diagnosis_id="diag_preexisting")
        db_session.commit()

        result = run_auto_diagnosis(db_session, [alert_id], now=sc.NOW)
        assert result.skipped_already_diagnosed == [alert_id]
        assert db_session.get(Alert, alert_id).diagnosis_id == "diag_preexisting"

    def test_severity_threshold_is_configurable(
        self, client: TestClient, db_session: Session
    ) -> None:
        upsert_resource(
            db_session, resource_id=CONTAINER_ID, kind=ResourceKind.CONTAINER.value,
            status="running", seen_at=sc.NOW,
        )
        warning_id = seed_alert(
            db_session, "alert_warning", severity=Severity.WARNING.value
        )
        db_session.commit()

        default = run_auto_diagnosis(db_session, [warning_id], now=sc.NOW)
        assert default.attempted == []

        lowered = run_auto_diagnosis(
            db_session,
            [warning_id],
            min_severity=Severity.INFO.value,
            now=sc.NOW,
        )
        assert lowered.attempted == [warning_id]

    def test_cap_protects_against_bulk_ingest(
        self, client: TestClient, db_session: Session
    ) -> None:
        """一次上报可能瞬间产生大量告警；必须封顶，宁可漏也不能卡住上报。

        未诊断的告警在列表里 `diagnosisId` 为 `null`，用户点"一键诊断"即可补上 ——
        这是**可恢复的降级**，而卡住上报是不可恢复的。

        五个告警必须落在**五个不同资源**上：同资源的多条告警会被事故聚合正确地
        并成一次事故，那样就测不到上限了。上限要防的正是"一次大批量上报触达
        大量不同资源"。
        """
        resource_ids = []
        for index in range(5):
            resource_id = f"container:probe:probe-x:bulk-{index}"
            upsert_resource(
                db_session,
                resource_id=resource_id,
                kind=ResourceKind.CONTAINER.value,
                status="running",
                seen_at=sc.NOW,
            )
            resource_ids.append(resource_id)

        ids = [
            seed_alert(db_session, f"alert_bulk_{index}", resource_id=resource_id)
            for index, resource_id in enumerate(resource_ids)
        ]
        db_session.commit()

        result = run_auto_diagnosis(db_session, ids, max_diagnoses=2, now=sc.NOW)

        assert len(result.attempted) == 2, "两次事故应被诊断"
        assert result.truncated is True
        # 每次事故的成员各自关联到同一条结论
        assert len(set(result.linked.values())) == 2
        assert sorted(result.linked) == ["alert_bulk_0", "alert_bulk_1"], (
            "截断时优先保留更严重 / 更近触发的事故"
        )

    def test_diagnosis_failure_does_not_break_the_alert(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """诊断抛错必须被吞掉并逐条记录 —— 告警本身不应受影响。

        这是本模块最重要的取舍：数据采集与告警是主流程，根因分析是增值能力；
        增值能力不能反过来毁掉主流程。
        """
        upsert_resource(
            db_session, resource_id=CONTAINER_ID, kind=ResourceKind.CONTAINER.value,
            status="running", seen_at=sc.NOW,
        )
        alert_id = seed_alert(db_session, "alert_boom")
        db_session.commit()

        from app.diagnosis import service as diagnosis_service

        def explode(*args, **kwargs):
            raise RuntimeError("模拟诊断崩溃")

        monkeypatch.setattr(diagnosis_service, "resolve_trigger", explode)
        result = run_auto_diagnosis(db_session, [alert_id], now=sc.NOW)

        assert alert_id in result.failed
        assert "模拟诊断崩溃" in result.failed[alert_id]
        # 告警仍在，仍无诊断
        row = db_session.get(Alert, alert_id)
        assert row is not None
        assert row.diagnosis_id is None
        assert row.state == AlertState.FIRING.value

    def test_missing_alert_is_not_a_failure(
        self, client: TestClient, db_session: Session
    ) -> None:
        result = run_auto_diagnosis(db_session, ["alert_does_not_exist"], now=sc.NOW)
        assert result.failed == {}
        assert result.attempted == []

    def test_empty_input_is_a_noop(self, client: TestClient, db_session: Session) -> None:
        result = run_auto_diagnosis(db_session, [], now=sc.NOW)
        assert result.as_dict()["attempted"] == 0

    def test_default_cap_is_modest(self) -> None:
        """上限本身也是设计的一部分：它决定最坏情况下一次上报的额外耗时。"""
        assert 1 <= DEFAULT_MAX_DIAGNOSES <= 50


class TestManualAndAutomaticAgree:
    def test_manual_trigger_produces_same_shape(
        self, client: TestClient, db_session: Session
    ) -> None:
        """手动与自动两条路径产出的结论形态必须一致，否则前端要写两套渲染。"""
        ingest(client, sc.scenario_container_oom(at=sc.NOW))
        alert = next(a for a in alerts_of(client) if a["ruleId"] == "R-CTR-OOM-010")
        auto_diag = client.get(f"/api/v1/diagnosis/{alert['diagnosisId']}").json()["data"]

        manual = client.post("/api/v1/diagnoses", json={"alertId": alert["id"]}).json()["data"]

        # 两个响应体的字段集**允许**不同：手动路径会带上 `linkedToAlert`
        # （它可能回写告警），自动路径则有自己的摘要字段。必须一致的是
        # **诊断内容本身** —— 否则前端要为两条路径写两套渲染。
        assert set(manual) & set(auto_diag) >= {
            "id",
            "createdAt",
            "trigger",
            "rootCause",
            "confidence",
            "affectedResources",
            "onChain",
            "potentiallyAffected",
            "evidence",
            "recommendation",
            "ruleSetVersion",
        }
        for key in (
            "rootCause",
            "ruleSetVersion",
            "affectedResources",
            "onChain",
            "potentiallyAffected",
            "evidence",
            "recommendation",
        ):
            assert manual[key] == auto_diag[key], f"{key} 在两条路径下不一致"

    def test_auto_diagnosis_is_listed(self, client: TestClient) -> None:
        """自动产生的诊断必须在列表里可见（C 的 Q13：否则发现不了它）。"""
        ingest(client, sc.scenario_container_oom(at=sc.NOW))
        items = client.get("/api/v1/diagnoses").json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["trigger"]["alertId"]
