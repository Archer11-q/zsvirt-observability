"""诊断端点测试（`docs/API_CONTRACT.md` §4.6 / §4.7）。

核心断言是三条**红线**（每条都是契约里的硬要求，不是风格偏好）：

1. `confidence` 必须能由 `confidenceBreakdown` **逐项求和复算**（C 的 D-036）
2. `affectedResources` / `onChain` / `potentiallyAffected` 是**三个独立字段**
   且**互不重叠**（C 的 D-075）—— 合并它们会让"影响范围"退化成全量拓扑
3. 无证据时 `rootCause` 只能是 `UNKNOWN` 且**不编造**根因与建议

规则集当前为空（真实规则属任务 2），因此这里通过 monkeypatch 注入一个
测试规则集来验证**有规则时的完整链路** —— 否则这些断言要等到任务 2 才能写，
而"confidence 可复算"恰恰是最容易在重构中被破坏的一条。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import _factories as f
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.diagnosis.types import EvidenceKind, Rule, RuleSet

RULES = RuleSet(
    version="rs-test-0.1.0",
    rules=(
        Rule(
            id="R-GPU-MEM-001",
            root_cause="GPU_MEMORY_EXHAUSTED",
            contribution=0.55,
            evidence_kind=EvidenceKind.EVENT,
            match_name="gpu.memory.exhausted",
            # GPU 显存指标在 vGPU（访客可见的那一层）与容器层都可能报出来，
            # 因此这条规则不限定资源类型 —— 这也让"证据落在链的哪一层"成为
            # 测试可以自由选择的变量。
            resource_kinds=(),
            propagate=True,
        ),
        Rule(
            id="R-INFER-TIMEOUT-002",
            root_cause="GPU_MEMORY_EXHAUSTED",
            contribution=0.37,
            evidence_kind=EvidenceKind.EVENT,
            match_name="inference.timeout",
            resource_kinds=("container", "ai_service"),
            propagate=True,
        ),
    ),
)


@pytest.fixture
def with_rules(monkeypatch: pytest.MonkeyPatch) -> RuleSet:
    """把测试规则集装进诊断服务。

    补丁打在**服务层**（`service.run_diagnosis` 的真实调用点）而不是引擎上：
    这样 `build_context` → 引擎 → 落库 的整条链路都会真实执行。
    """
    from app.diagnosis import service as diagnosis_service

    original = diagnosis_service.run_diagnosis

    def patched(session: Session, **kwargs: Any):
        kwargs.setdefault("rule_set", RULES)
        return original(session, **kwargs)

    monkeypatch.setattr(diagnosis_service, "run_diagnosis", patched)
    return RULES


def seed_scenario(session: Session) -> None:
    """一条链路 + 两条命中规则的事件。"""
    f.build_chain(session)
    f.add_event(session, "evt_0000000000000000000000001", type="gpu.memory.exhausted")
    f.add_event(session, "evt_0000000000000000000000002", type="inference.timeout")
    session.commit()


# ================================================================ 触发


class TestTriggerDiagnosis:
    def test_trigger_with_window_and_anchor(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        seed_scenario(db_session)

        r = client.post(
            "/api/v1/diagnoses",
            json={"anchorResourceId": f.AIS, "window": f.window_from()},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]

        assert data["rootCause"] == "GPU_MEMORY_EXHAUSTED"
        assert data["ruleSetVersion"] == "rs-test-0.1.0"
        assert data["trigger"]["anchorResourceId"] == f.AIS
        assert data["durationMs"] is not None and data["durationMs"] >= 0

    def test_confidence_is_recomputable_from_breakdown(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """契约红线：`confidence` == Σ `contribution`（C 的 D-036）。"""
        seed_scenario(db_session)

        data = client.post(
            "/api/v1/diagnoses",
            json={"anchorResourceId": f.AIS, "window": f.window_from()},
        ).json()["data"]

        breakdown = data["confidenceBreakdown"]
        assert len(breakdown) == 2, breakdown
        total = sum(item["contribution"] for item in breakdown)
        assert abs(total - data["confidence"]) < 1e-9, (
            f"confidence={data['confidence']} 无法由 breakdown 复算：{breakdown}"
        )
        assert data["confidence"] == pytest.approx(0.92)
        # 每条贡献都要能说明"观察到什么"，否则运维只拿到一个数字
        assert all(item["observed"] for item in breakdown)

    def test_impact_sets_are_separate_and_disjoint(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """契约红线：三集分开输出且不重叠（C 的 D-075）。

        证据放在 **VGPU**（规则允许 `vgpu` 资源）以便同时覆盖"链上过渡层"：
        链路 HOST→GPU→VGPU→VM→CTR→AIS，锚点 AIS，证据 {VGPU}
          - `affected`  = 证据自身（VGPU）+ 其非过渡层后代
          - `onChain`   = 证据到锚点之间的 VM、CTR
          - `potentiallyAffected` = 结构可达但离链的 HOST / GPU
        宿主机（离链祖先）**不进 affected** —— 它提供证据所在的层，本身没被影响。
        """
        f.build_chain(db_session)
        f.add_event(
            db_session,
            "evt_0000000000000000000000001",
            resource_id=f.VGPU,
            type="gpu.memory.exhausted",
        )
        db_session.commit()

        data = client.post(
            "/api/v1/diagnoses",
            json={"anchorResourceId": f.AIS, "window": f.window_from()},
        ).json()["data"]

        affected = set(data["affectedResources"])
        on_chain = set(data["onChain"])
        potential = set(data["potentiallyAffected"])

        assert affected == {f.VGPU}, affected
        assert on_chain == {f.VM, f.CTR}, on_chain
        assert potential == {f.HOST, f.GPU}, potential

        # 两两不相交，且并集覆盖锚点的全部可达资源
        assert not (affected & on_chain)
        assert not (affected & potential)
        assert not (on_chain & potential)
        assert affected | on_chain | potential == {
            f.HOST,
            f.GPU,
            f.VGPU,
            f.VM,
            f.CTR,
        }
        # 锚点自身永远不进任何一集
        assert f.AIS not in affected
        assert f.HOST not in affected, "离链祖先不得进 affected"

    def test_evidence_points_back_to_real_events(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        seed_scenario(db_session)

        data = client.post(
            "/api/v1/diagnoses",
            json={"anchorResourceId": f.AIS, "window": f.window_from()},
        ).json()["data"]

        assert len(data["evidence"]) == 2
        event_ids = {eid for ev in data["evidence"] for eid in ev["eventIds"]}
        assert event_ids == {
            "evt_0000000000000000000000001",
            "evt_0000000000000000000000002",
        }
        # 证据必须标出来源，模拟数据尤其要能识别（诚实性要求）
        assert all(ev["source"] for ev in data["evidence"])
        # 有根因就必须有处置建议
        assert data["recommendation"], "有根因却无建议，前端无法给出下一步"

    def test_trigger_without_evidence_returns_unknown(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """契约红线：无证据 → `UNKNOWN`，且不编造建议。

        窗口内没有事件时，返回一个"看起来很像"的根因是**最坏的失败方式** ——
        运维会照着它去改配置。必须返回 UNKNOWN。
        """
        f.build_chain(db_session)
        db_session.commit()

        data = client.post(
            "/api/v1/diagnoses",
            json={"anchorResourceId": f.AIS, "window": f.window_from()},
        ).json()["data"]

        assert data["rootCause"] == "UNKNOWN"
        assert data["confidence"] == 0.0
        assert data["evidence"] == []
        assert data["confidenceBreakdown"] == []
        # 但必须给出"证据不足，建议扩大窗口/补采集"这类可执行的下一步
        assert [r["code"] for r in data["recommendation"]] == ["COLLECT_MORE_EVIDENCE"]
        assert any("UNKNOWN" in n for n in data["notes"])

    def test_trigger_from_alert_links_back(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """C 的 Q13：从告警一键诊断，并把结论回写到告警。"""
        seed_scenario(db_session)
        f.add_alert(
            db_session,
            "alert_01J0000000000000000000001",
            resource_id=f.CTR,
            evidence=["evt_0000000000000000000000001"],
            fired_at=f.NOW - timedelta(minutes=1),
        )
        db_session.commit()

        body = client.post(
            "/api/v1/diagnoses",
            json={"alertId": "alert_01J0000000000000000000001"},
        ).json()

        data = body["data"]
        assert data["trigger"] == {"alertId": "alert_01J0000000000000000000001"}
        assert data["rootCause"] == "GPU_MEMORY_EXHAUSTED"
        assert body["data"]["linkedToAlert"] is True

        # 回写必须真的落到告警上（前端的"查看诊断"按钮依赖它）
        detail = client.get("/api/v1/alerts/alert_01J0000000000000000000001").json()
        assert detail["data"]["diagnosisId"] == data["id"]

    def test_link_disabled_leaves_alert_untouched(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """`linkToAlert=false` 时不回写 —— 联调时需要"只看不写"。"""
        seed_scenario(db_session)
        f.add_alert(
            db_session,
            "alert_01J0000000000000000000002",
            resource_id=f.CTR,
            evidence=["evt_0000000000000000000000001"],
        )
        db_session.commit()

        body = client.post(
            "/api/v1/diagnoses",
            json={
                "alertId": "alert_01J0000000000000000000002",
                "linkToAlert": False,
            },
        ).json()

        assert body["data"]["linkedToAlert"] is False
        detail = client.get("/api/v1/alerts/alert_01J0000000000000000000002").json()
        assert detail["data"]["diagnosisId"] is None

    def test_unknown_alert_returns_404(self, client: TestClient) -> None:
        r = client.post("/api/v1/diagnoses", json={"alertId": "alert_nope"})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "TRIGGER_TARGET_NOT_FOUND"

    def test_unknown_resource_returns_404(self, client: TestClient) -> None:
        r = client.post("/api/v1/diagnoses", json={"anchorResourceId": "vm:zsvirt:nope"})
        assert r.status_code == 404

    def test_missing_anchor_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/diagnoses", json={})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_inverted_window_returns_400(self, client: TestClient, db_session: Session) -> None:
        f.build_chain(db_session)
        db_session.commit()

        r = client.post(
            "/api/v1/diagnoses",
            json={
                "anchorResourceId": f.AIS,
                "window": {
                    "from": f.NOW.isoformat(),
                    "to": (f.NOW - timedelta(hours=1)).isoformat(),
                },
            },
        )
        assert r.status_code == 400
        assert "from 必须早于 to" in r.json()["error"]["message"]

    def test_malformed_window_returns_400(self, client: TestClient, db_session: Session) -> None:
        f.build_chain(db_session)
        db_session.commit()

        r = client.post(
            "/api/v1/diagnoses",
            json={
                "anchorResourceId": f.AIS,
                "window": {"from": "not-a-time", "to": "also-not"},
            },
        )
        assert r.status_code == 400
        assert "ISO8601" in r.json()["error"]["message"]

    def test_failed_trigger_writes_nothing(self, client: TestClient, db_session: Session) -> None:
        """参数非法时**不落库** —— 否则列表里会堆满空诊断。"""
        f.build_chain(db_session)
        db_session.commit()

        client.post("/api/v1/diagnoses", json={"anchorResourceId": "vm:zsvirt:nope"})

        listed = client.get("/api/v1/diagnoses").json()["data"]["items"]
        assert listed == []


# ================================================================ 详情


class TestGetDiagnosis:
    def test_shape_matches_contract(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """`API_CONTRACT.md` §4.7 逐字段核对。"""
        seed_scenario(db_session)
        created = client.post(
            "/api/v1/diagnoses",
            json={"anchorResourceId": f.AIS, "window": f.window_from()},
        ).json()["data"]

        data = client.get(f"/api/v1/diagnosis/{created['id']}").json()["data"]

        for field in (
            "id",
            "createdAt",
            "trigger",
            "rootCause",
            "confidence",
            "confidenceBreakdown",
            "affectedResources",
            "potentiallyAffected",
            "onChain",
            "evidence",
            "recommendation",
            "ruleSetVersion",
            "notes",
        ):
            assert field in data, f"缺少契约字段 {field}"
        assert data["id"].startswith("diag_")

    def test_created_at_is_utc(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """时间统一 UTC 输出，不暴露会话时区偏移。"""
        seed_scenario(db_session)
        created = client.post(
            "/api/v1/diagnoses",
            json={"anchorResourceId": f.AIS, "window": f.window_from()},
        ).json()["data"]

        data = client.get(f"/api/v1/diagnosis/{created['id']}").json()["data"]
        parsed = datetime.fromisoformat(data["createdAt"])
        assert parsed.utcoffset() == timedelta(0), data["createdAt"]

    def test_include_evidence_inlines_event_bodies(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        seed_scenario(db_session)
        created = client.post(
            "/api/v1/diagnoses",
            json={"anchorResourceId": f.AIS, "window": f.window_from()},
        ).json()["data"]

        plain = client.get(f"/api/v1/diagnosis/{created['id']}").json()["data"]
        assert plain["evidenceEvents"] == [], "默认不得内联（列表页会被撑爆）"

        inlined = client.get(
            f"/api/v1/diagnosis/{created['id']}", params={"includeEvidence": "true"}
        ).json()["data"]
        ids = {e["id"] for e in inlined["evidenceEvents"]}
        assert ids == {
            "evt_0000000000000000000000001",
            "evt_0000000000000000000000002",
        }
        # 内联的是**真实事件**，不是摘要
        assert all("occurredAt" in e and "severity" in e for e in inlined["evidenceEvents"])

    def test_missing_evidence_event_is_skipped_not_fatal(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """证据指向的事件若已不存在，少返回一条而不是 500。

        事件表只追加不删除，理论上不会发生；但若历史诊断引用了被人工清理过的
        事件，整个详情接口挂掉会让**所有**诊断都看不到。
        """
        seed_scenario(db_session)
        created = client.post(
            "/api/v1/diagnoses",
            json={"anchorResourceId": f.AIS, "window": f.window_from()},
        ).json()["data"]

        from app.models import Event

        db_session.execute(
            Event.__table__.delete().where(Event.id == "evt_0000000000000000000000002")
        )
        db_session.commit()

        r = client.get(f"/api/v1/diagnosis/{created['id']}", params={"includeEvidence": "true"})
        assert r.status_code == 200, r.text
        assert {e["id"] for e in r.json()["data"]["evidenceEvents"]} == {
            "evt_0000000000000000000000001"
        }

    def test_unknown_id_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/diagnosis/diag_nope")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"


# ================================================================ 列表


class TestListDiagnoses:
    def _make(
        self,
        client: TestClient,
        db_session: Session,
        *,
        anchor: str,
        at: datetime | None = None,
    ) -> str:
        body: dict[str, Any] = {"anchorResourceId": anchor, "window": f.window_from()}
        if at is not None:
            body["window"] = {
                "from": (at - timedelta(minutes=30)).isoformat(),
                "to": (at + timedelta(minutes=30)).isoformat(),
            }
        return client.post("/api/v1/diagnoses", json=body).json()["data"]["id"]

    def test_empty_list(self, client: TestClient) -> None:
        body = client.get("/api/v1/diagnoses").json()
        assert body["data"]["items"] == []
        assert body["data"]["rootCauseCounts"] == {}
        assert body["meta"]["page"]["nextCursor"] is None

    def test_newest_first_with_root_cause_counts(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """C 的 Q13：列表让前端能发现"有哪些诊断"，并做根因 Top N。"""
        f.build_chain(db_session)
        # 两次诊断各自的时间窗内都要有证据，否则第二次会合理地返回 UNKNOWN，
        # 计数断言就变成在测"窗口边界"而不是在测排序与计数。
        f.add_event(db_session, "evt_0000000000000000000000011", occurred_at=f.NOW)
        f.add_event(
            db_session,
            "evt_0000000000000000000000012",
            occurred_at=f.NOW - timedelta(hours=2),
        )
        db_session.commit()

        first = self._make(client, db_session, anchor=f.AIS, at=f.NOW - timedelta(hours=2))
        second = self._make(client, db_session, anchor=f.AIS, at=f.NOW)

        data = client.get("/api/v1/diagnoses").json()["data"]
        ids = [item["id"] for item in data["items"]]
        assert ids == [second, first], "按 createdAt 降序"
        assert data["rootCauseCounts"] == {"GPU_MEMORY_EXHAUSTED": 2}

    def test_pagination_covers_all_without_overlap(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """游标分页必须不漏不重（C 的 Q11 对列表接口同样适用）。"""
        seed_scenario(db_session)
        created = [
            self._make(client, db_session, anchor=f.AIS, at=f.NOW - timedelta(hours=i))
            for i in range(5)
        ]

        collected: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = client.get("/api/v1/diagnoses", params=params).json()
            collected.extend(item["id"] for item in body["data"]["items"])
            cursor = body["meta"]["page"]["nextCursor"]
            if not cursor:
                break

        assert len(collected) == len(set(collected)), f"分页出现重复：{collected}"
        assert set(collected) == set(created), "分页漏掉了诊断"

    def test_next_cursor_absent_on_last_page(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        seed_scenario(db_session)
        self._make(client, db_session, anchor=f.AIS)

        body = client.get("/api/v1/diagnoses", params={"limit": 10}).json()
        assert body["meta"]["page"]["nextCursor"] is None

    def test_filter_by_resource_matches_any_of_three_sets(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        """`resourceId` 过滤要覆盖三集 —— 只查 affected 会漏掉链上与潜在项。"""
        seed_scenario(db_session)
        self._make(client, db_session, anchor=f.AIS)

        for rid in (f.CTR, f.VM, f.GPU):
            body = client.get("/api/v1/diagnoses", params={"resourceId": rid}).json()
            assert len(body["data"]["items"]) == 1, f"{rid} 未被检索到"

        other = client.get("/api/v1/diagnoses", params={"resourceId": "vm:zsvirt:unrelated"}).json()
        assert other["data"]["items"] == []

    def test_filter_by_root_cause(
        self, client: TestClient, db_session: Session, with_rules: RuleSet
    ) -> None:
        seed_scenario(db_session)
        self._make(client, db_session, anchor=f.AIS)

        hit = client.get("/api/v1/diagnoses", params={"rootCause": "GPU_MEMORY_EXHAUSTED"}).json()
        assert len(hit["data"]["items"]) == 1

        miss = client.get("/api/v1/diagnoses", params={"rootCause": "UNKNOWN"}).json()
        assert miss["data"]["items"] == []

    def test_invalid_cursor_returns_400(self, client: TestClient) -> None:
        r = client.get("/api/v1/diagnoses", params={"cursor": "!!!not-base64!!!"})
        assert r.status_code == 400
        assert "游标非法" in r.json()["error"]["message"]

    def test_inverted_window_returns_400(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/diagnoses",
            params={"from": f.NOW.isoformat(), "to": (f.NOW - timedelta(hours=1)).isoformat()},
        )
        assert r.status_code == 400
