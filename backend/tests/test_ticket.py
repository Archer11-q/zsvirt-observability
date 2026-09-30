"""`POST /api/v1/diagnoses/{id}/ticket` —— 诊断工单文本测试。

赛题依据：「告警运营与可用性 10%」（界面/CLI 是否便于运维完成**定位和处置**）
与「创新性与工程落地性 15%」（是否具备**生产落地价值**）。

测试重点不在排版好看，而在三件工程属性：

1. **纯函数、可复现** —— 同一诊断两次导出必须逐字节相同。工单要归档、被反复引用，
   两次不一样就失去证据价值；
2. **不编结论** —— `UNKNOWN` 或低置信度必须**明说不可据此行动**，而不是凑一个
   看起来完整的工单；
3. **只读** —— 不写库、不改任何状态。本项目对 ZSvirt 只申请只读权限。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.diagnosis.ticket import render_ticket

NOW = datetime(2026, 9, 30, 7, 0, 0, tzinfo=UTC)


def _render(**overrides) -> str:
    """最小可用的渲染参数，各用例只覆盖自己关心的字段。"""
    base = {
        "diagnosis_id": "diag_test_1",
        "created_at": NOW,
        "root_cause": "CONTAINER_MEMORY_LIMIT",
        "confidence": 0.9,
        "confidence_breakdown": [
            {"ruleId": "R-CTR-OOM-100", "contribution": 0.55, "observed": "告警 ×1"},
            {"ruleId": "R-CTR-OOM-101", "contribution": 0.35, "observed": "oom_killed ×1"},
        ],
        "affected": ["container:probe:a:ctr0"],
        "on_chain": [],
        "potentially_affected": ["host:probe:a:h0"],
        "evidence": [
            {
                "type": "event",
                "name": "container.oom_killed",
                "resourceId": "container:probe:a:ctr0",
                "count": 1,
                "source": "probe",
            }
        ],
        "recommendation": [{"code": "INCREASE_CONTAINER_MEMORY", "text": "提高内存限额"}],
        "rule_set_version": "rs-diag-1.0.0",
        "notes": ["引擎备注示例"],
    }
    base.update(overrides)
    return render_ticket(**base).text


def _create_diagnosis(client: TestClient, db_session: Session) -> str:
    """建链路 + 触发一次诊断，返回 diagnosisId。

    锚点与时间窗都取自 `tests._factories` 的真实常量。手写一个不存在的资源 ID
    会得到 400，而把用例 `pytest.skip` 掉等于把缺口藏起来 —— 在摘要里它读起来
    像"已覆盖"。
    """
    from tests import _factories as f

    f.build_chain(db_session)
    db_session.commit()

    response = client.post(
        "/api/v1/diagnoses",
        json={"anchorResourceId": f.AIS, "window": f.window_from()},
    )
    assert response.status_code == 200, (
        f"夹具未产出诊断：{response.status_code} {response.text[:200]}"
    )
    return response.json()["data"]["id"]


# ================================================================ 纯函数


class TestPureFunctionReproducibility:
    def test_same_input_renders_identical_text(self) -> None:
        """**这条是本模块存在的理由。** 工单会被归档、被后来的人当作事实引用。"""
        assert _render() == _render()

    def test_signature_takes_no_session(self) -> None:
        """纯函数：不读数据库、不读配置。"""
        import inspect

        assert "session" not in inspect.signature(render_ticket).parameters


# ================================================================ 诚实性


class TestHonestyAboutWeakConclusions:
    """本项目一贯的红线：宁可说"不知道"，也不编一个看起来完整的结论。"""

    def test_unknown_is_flagged_as_not_actionable(self) -> None:
        text = _render(root_cause="UNKNOWN", confidence=0.2, recommendation=[])
        assert "UNKNOWN" in text
        assert ("不足以" in text) or ("不可" in text) or ("无法" in text)

    def test_low_confidence_is_flagged(self) -> None:
        document = render_ticket(
            diagnosis_id="d",
            created_at=NOW,
            root_cause="CONTAINER_MEMORY_LIMIT",
            confidence=0.35,
            confidence_breakdown=[{"ruleId": "R", "contribution": 0.35, "observed": "x"}],
            affected=[],
            on_chain=[],
            potentially_affected=[],
            evidence=[],
            recommendation=[{"code": "C", "text": "t"}],
            rule_set_version="rs",
            notes=[],
        )
        assert document.actionable is False
        assert "置信度偏低" in document.text

    def test_high_confidence_is_actionable(self) -> None:
        document = render_ticket(
            diagnosis_id="d",
            created_at=NOW,
            root_cause="CONTAINER_MEMORY_LIMIT",
            confidence=0.9,
            confidence_breakdown=[{"ruleId": "R", "contribution": 0.9, "observed": "x"}],
            affected=[],
            on_chain=[],
            potentially_affected=[],
            evidence=[],
            recommendation=[{"code": "C", "text": "t"}],
            rule_set_version="rs",
            notes=[],
        )
        assert document.actionable is True

    def test_missing_evidence_is_called_out(self) -> None:
        """无证据是红线场景，工单上必须能看出来，而不是留一片空白。"""
        assert "无证据" in _render(evidence=[])

    def test_evidence_source_is_always_printed(self) -> None:
        """来源必须进工单：模拟数据与真实采集的证据价值不同，
        而工单会被归档、被后来的人当作事实引用。"""
        assert "source=probe" in _render()
        assert "source=simulated" in _render(
            evidence=[
                {
                    "type": "metric",
                    "name": "self_vgpu_memory_usage",
                    "resourceId": "gpu:zsvirt:x",
                    "value": 98.0,
                    "source": "simulated",
                }
            ]
        )


# ================================================================ 关键事实


class TestKeyFactsArePresent:
    def test_confidence_breakdown_is_recomputable_in_the_text(self) -> None:
        """明细 + 合计都要在文本里 —— 工单读者要能自己验算。"""
        text = _render()
        assert "R-CTR-OOM-100" in text
        assert "+0.5500" in text
        assert "+0.3500" in text
        assert "合计" in text
        assert "+0.9000" in text

    def test_synthetic_adjustment_rows_are_marked(self) -> None:
        """`__conflict_penalty__` 这类合成行必须标注「调整」。

        否则读者会去规则集里找一个不存在的规则。
        """
        text = _render(
            confidence=0.35,
            confidence_breakdown=[
                {"ruleId": "R-GPU-SELF-001", "contribution": 0.55, "observed": "gpu 98%"},
                {
                    "ruleId": "__conflict_penalty__",
                    "contribution": -0.2,
                    "observed": "候选冲突",
                },
            ],
        )
        assert "调整" in text
        assert "-0.2000" in text

    def test_resource_names_and_ids_both_appear(self) -> None:
        """可读名便于人读，原始 ID 便于机器追溯 —— 两个都要。"""
        text = _render(resource_labels={"container:probe:a:ctr0": "demo-ctr-oom"})
        assert "demo-ctr-oom" in text
        assert "container:probe:a:ctr0" in text

    def test_missing_label_falls_back_to_raw_id(self) -> None:
        """没有可读名时用原始 ID —— ID 可追溯，编一个名字不可追溯。"""
        assert "container:probe:a:ctr0" in _render(resource_labels={})

    def test_section_rules_do_not_exceed_the_box(self) -> None:
        """分隔线不能长过边框。

        中文在等宽终端占 2 列，用 `len()` 算会让横线明显长出边框，
        看起来像排版坏掉（这是实际渲染出来才发现的问题）。
        """
        text = _render()
        widths = {
            sum(2 if ord(ch) > 0x2E80 else 1 for ch in line) for line in text.splitlines() if line
        }
        assert max(widths) <= 80, f"有行宽 {max(widths)} 超过边框宽度"


# ================================================================ 端点


class TestEndpoint:
    def test_ticket_for_a_real_diagnosis(self, client: TestClient, db_session: Session) -> None:
        diagnosis_id = _create_diagnosis(client, db_session)
        response = client.post(f"/api/v1/diagnoses/{diagnosis_id}/ticket")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["diagnosisId"] == diagnosis_id
        assert data["text"].strip()
        assert "Crosslayer" in data["text"]
        assert isinstance(data["actionable"], bool)
        assert data["title"].startswith("[Crosslayer]")
        # 工单必须能让人追溯到原始诊断
        assert f"/api/v1/diagnosis/{diagnosis_id}" in data["text"]

    def test_ticket_carries_the_confidence_breakdown(
        self, client: TestClient, db_session: Session
    ) -> None:
        """构成明细必须进工单 —— 归档后仍可复算，这是可解释性的底线。

        明细可能为空（例如 `UNKNOWN` 结论没有规则支撑）。那种情况下工单**不该**
        编一个"合计 0.0000"出来，而应明说无规则支撑 —— 所以这里分两路断言，
        而不是无条件要求"合计"出现。
        """
        diagnosis_id = _create_diagnosis(client, db_session)
        text = client.post(f"/api/v1/diagnoses/{diagnosis_id}/ticket").json()["data"]["text"]
        detail = client.get(f"/api/v1/diagnosis/{diagnosis_id}").json()["data"]

        for item in detail["confidenceBreakdown"]:
            assert item["ruleId"] in text, f"工单缺少规则 {item['ruleId']}"

        if detail["confidenceBreakdown"]:
            assert "合计" in text
        else:
            assert "没有规则支撑" in text, "空明细时应明说无规则支撑，而不是留白"

    def test_unknown_diagnosis_returns_404(self, client: TestClient) -> None:
        response = client.post("/api/v1/diagnoses/diag_does_not_exist/ticket")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"

    def test_ticket_is_read_only(self, client: TestClient, db_session: Session) -> None:
        """**只读**：导出工单不得改变诊断本身。

        这是给命题方的信用所在 —— 我们对 ZSvirt 只申请只读权限，
        自己的接口也不该在"导出"这种动作里偷偷写库。
        """
        diagnosis_id = _create_diagnosis(client, db_session)
        before = client.get(f"/api/v1/diagnosis/{diagnosis_id}").json()["data"]
        client.post(f"/api/v1/diagnoses/{diagnosis_id}/ticket")
        after = client.get(f"/api/v1/diagnosis/{diagnosis_id}").json()["data"]

        assert before == after, "导出工单改变了诊断内容"
