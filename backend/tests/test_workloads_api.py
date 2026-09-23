"""工作负载端点测试（`docs/API_CONTRACT.md` §4.3，C 的 Q10）。

这是一个**最容易写出"好看但骗人"的接口**的地方，所以测试的重点不是字段齐全，
而是**缺失时是否诚实**：

| 场景 | 期望 | 反面（错误做法） |
|---|---|---|
| GPU 指标没采到 | `null` + `note` 说明原因 | 填 0，前端显示"显存占用 0%" |
| 服务未绑定 GPU | `null` + "未绑定" | 报上一个无关的 GPU |
| 模拟数据 | `gpuProviderMode="simulated"` | 冒充真实采集 |

计数口径也在测试里固定下来：链路（GPU/VM/容器）上的事件与告警**归到链顶的
AI 服务**名下 —— 只数服务节点自身的计数会让整个视图全是 0，跨层关联也就没意义了。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import _factories as f
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

#: 显式统计窗口。**不用真实 `now`** —— 事件固定落在 2026-09-17，
#: 若窗口依赖运行时时钟，这个测试会在"真实日期偏离夹具日期"后开始失败。
SINCE = "2026-09-16T00:00:00+00:00"
UNTIL = "2026-09-18T00:00:00+00:00"


def list_workloads(client: TestClient, **params: Any) -> dict[str, Any]:
    query = {"since": SINCE, "to": UNTIL}
    query.update(params)
    r = client.get("/api/v1/workloads", params=query)
    assert r.status_code == 200, r.text
    return r.json()["data"]


class TestWorkloadShape:
    def test_every_field_c_named_is_present(self, client: TestClient, db_session: Session) -> None:
        """C 的 Q10 点名的字段逐个核对。"""
        f.build_chain(db_session)
        db_session.commit()

        data = list_workloads(client)
        assert len(data["items"]) == 1
        item = data["items"][0]

        for field in (
            "id",
            "name",
            "status",
            "observability",
            "attributes",
            "resourceUsage",
            "eventCount",
            "alertCount",
            "diagnosisId",
        ):
            assert field in item, f"缺少 C 点名的字段 {field}"

        assert item["id"] == f.AIS
        assert item["name"] == "vllm-qwen"
        assert item["status"] == "running"
        # attributes 透传探针上报的业务属性
        assert item["attributes"]["framework"] == "vllm"
        assert item["attributes"]["modelName"] == "Qwen2.5-7B-Instruct"

    def test_window_and_provider_mode_are_reported(
        self, client: TestClient, db_session: Session
    ) -> None:
        """`gpuProviderMode` 必须暴露 —— 前端要能区分真实指标与模拟数据。"""
        f.build_chain(db_session)
        db_session.commit()

        data = list_workloads(client)
        assert data["gpuProviderMode"] in ("zsvirt-zwatch", "guest-smi", "simulated", "auto")
        assert data["windowFrom"].startswith("2026-09-16")
        assert data["windowTo"].startswith("2026-09-18")

    def test_provider_mode_says_simulated_when_enabled(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """开启模拟数据时对外必须报 `simulated`（诚实性要求）。"""
        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "simulated_data_enabled", True)
        # `gpu_provider_mode` 是 property，读的是上面这个字段，因此无需再补丁

        f.build_chain(db_session)
        db_session.commit()

        assert list_workloads(client)["gpuProviderMode"] == "simulated"

    def test_status_and_observability_are_independent(
        self, client: TestClient, db_session: Session
    ) -> None:
        """D-032：`running` 与 `stale` 可以同时为真，不得合并成一个字段。"""
        from app.graph.repository import mark_stale

        f.build_chain(db_session, seen_at=f.NOW - timedelta(hours=2))
        mark_stale(db_session, older_than_seconds=60, now=f.NOW)
        db_session.commit()

        item = list_workloads(client)["items"][0]
        assert item["status"] == "running", "业务状态仍由来源系统给出"
        assert item["observability"] == "stale", "观测状态由 B 判定"
        assert item["staleness"] is not None, "陈旧时必须给出时长，前端才能提示"


class TestResourceUsageHonesty:
    def test_missing_gpu_metrics_are_null_not_zero(
        self, client: TestClient, db_session: Session
    ) -> None:
        """**本端点最重要的测试**：没采到就报 null + 说明，绝不填 0。

        GPU 容量来自 ZSvirt 资产 API（静态、权威），即使利用率采不到也应给出；
        占用与利用率需要 ZWatch 或探针，缺失时必须为 null。
        """
        f.build_chain(db_session, gpu_attributes={"memTotalBytes": 24 * 1024**3})
        db_session.commit()

        usage = list_workloads(client)["items"][0]["resourceUsage"]
        assert usage["gpuMemoryTotalBytes"] == 24 * 1024**3
        assert usage["gpuMemoryUsedBytes"] is None, "未采集到不得报 0"
        assert usage["gpuUtilizationPct"] is None, "未采集到不得报 0"
        assert usage["note"], "缺失必须有说明，否则前端只能猜"
        assert "memUsedBytes" in usage["note"]
        assert "utilizationPct" in usage["note"]

    def test_present_metrics_are_reported(self, client: TestClient, db_session: Session) -> None:
        f.build_chain(
            db_session,
            gpu_attributes={
                "memTotalBytes": 24 * 1024**3,
                "memUsedBytes": 22 * 1024**3,
                "utilizationPct": 98,
            },
        )
        db_session.commit()

        usage = list_workloads(client)["items"][0]["resourceUsage"]
        assert usage["gpuMemoryUsedBytes"] == 22 * 1024**3
        assert usage["gpuUtilizationPct"] == pytest.approx(98.0)
        assert usage["note"] is None, "指标齐全时不该有告警性说明"

    def test_legacy_attribute_names_are_accepted(
        self, client: TestClient, db_session: Session
    ) -> None:
        """容忍 ZSvirt 资产 API 的原始字段名与带百分号的字符串。"""
        f.build_chain(
            db_session,
            gpu_attributes={
                "memory": 24 * 1024**3,
                "mem_used_bytes": 8 * 1024**3,
                "utilization_pct": "37.5%",
            },
        )
        db_session.commit()

        usage = list_workloads(client)["items"][0]["resourceUsage"]
        assert usage["gpuMemoryTotalBytes"] == 24 * 1024**3
        assert usage["gpuMemoryUsedBytes"] == 8 * 1024**3
        assert usage["gpuUtilizationPct"] == pytest.approx(37.5)

    def test_zero_usage_is_reported_as_zero_not_as_missing(
        self, client: TestClient, db_session: Session
    ) -> None:
        """0 是**合法值**（显存占用 0 是好事），不能被当成"缺失"。

        实现里若用 `x = value or x` 做"取第一个非空"，0 会被后续值覆盖 ——
        这条测试就是为那个坑写的。
        """
        f.build_chain(
            db_session,
            gpu_attributes={
                "memTotalBytes": 24 * 1024**3,
                "memUsedBytes": 0,
                "utilizationPct": 0,
            },
        )
        db_session.commit()

        usage = list_workloads(client)["items"][0]["resourceUsage"]
        assert usage["gpuMemoryUsedBytes"] == 0
        assert usage["gpuUtilizationPct"] == pytest.approx(0.0)
        assert usage["note"] is None

    def test_each_workload_gets_its_own_usage(
        self, client: TestClient, db_session: Session
    ) -> None:
        """指标必须落在**每个**工作负载上，不能因为遍历顺序而漏掉某个服务。

        （同一容器下的两个服务确实共享同一块 GPU，因此两者数值相同是正确的；
        这里断言的是"都被解析到了"，而不是"数值不同"。）
        """
        from app.enums import ResourceKind, ResourceStatus
        from app.graph.repository import upsert_resource

        f.build_chain(
            db_session,
            gpu_attributes={
                "memTotalBytes": 24 * 1024**3,
                "memUsedBytes": 4 * 1024**3,
            },
        )
        upsert_resource(
            db_session,
            resource_id=f.AIS2,
            kind=ResourceKind.AI_SERVICE.value,
            parent_id=f.CTR,
            name="embed",
            status=ResourceStatus.RUNNING.value,
            seen_at=f.NOW,
        )
        db_session.commit()

        items = {i["id"]: i for i in list_workloads(client)["items"]}
        assert set(items) == {f.AIS, f.AIS2}
        for wid in (f.AIS, f.AIS2):
            assert items[wid]["resourceUsage"]["gpuMemoryUsedBytes"] == 4 * 1024**3, wid

    def test_service_without_gpu_reports_reason(
        self, client: TestClient, db_session: Session
    ) -> None:
        """未绑定 GPU 的服务给出原因，而不是空白或 0。"""
        f.build_chain(db_session)
        # 一个孤立的 ai_service（没有容器/GPU 父链）
        from app.enums import ResourceKind, ResourceStatus
        from app.graph.repository import upsert_resource

        upsert_resource(
            db_session,
            resource_id="ai_service:probe:probe-x:standalone",
            kind=ResourceKind.AI_SERVICE.value,
            name="standalone",
            status=ResourceStatus.RUNNING.value,
            seen_at=f.NOW,
        )
        db_session.commit()

        items = {i["id"]: i for i in list_workloads(client)["items"]}
        usage = items["ai_service:probe:probe-x:standalone"]["resourceUsage"]
        assert usage["gpuMemoryUsedBytes"] is None
        assert "无 GPU" in usage["note"]


class TestRollupCounts:
    def test_events_on_any_chain_member_roll_up_to_the_service(
        self, client: TestClient, db_session: Session
    ) -> None:
        """跨层关联的落地：GPU/VM/容器上的事件算到 AI 服务头上。"""
        f.build_chain(db_session)
        f.add_event(db_session, "evt_a", resource_id=f.GPU, occurred_at=f.NOW)
        f.add_event(db_session, "evt_b", resource_id=f.VM, occurred_at=f.NOW)
        f.add_event(db_session, "evt_c", resource_id=f.CTR, occurred_at=f.NOW)
        f.add_event(db_session, "evt_d", resource_id=f.AIS, occurred_at=f.NOW)
        db_session.commit()

        item = list_workloads(client)["items"][0]
        assert item["eventCount"] == 4, "只数服务节点会得到 1，视图就失去意义"

    def test_events_outside_window_are_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        f.build_chain(db_session)
        f.add_event(db_session, "evt_in", occurred_at=f.NOW)
        f.add_event(
            db_session,
            "evt_out",
            occurred_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        )
        db_session.commit()

        assert list_workloads(client)["items"][0]["eventCount"] == 1

    def test_alert_counts_split_active_from_total(
        self, client: TestClient, db_session: Session
    ) -> None:
        """`alertCount` 是窗口内总数，`firingAlertCount` 只算未恢复的。"""
        from app.enums import AlertState

        f.build_chain(db_session)
        f.add_event(db_session, "evt_1", occurred_at=f.NOW)
        f.add_alert(db_session, "alert_firing", evidence=["evt_1"], state=AlertState.FIRING.value)
        f.add_alert(db_session, "alert_acked", evidence=["evt_1"], state=AlertState.ACKED.value)
        f.add_alert(
            db_session, "alert_resolved", evidence=["evt_1"], state=AlertState.RESOLVED.value
        )
        db_session.commit()

        item = list_workloads(client)["items"][0]
        assert item["alertCount"] == 3
        assert item["firingAlertCount"] == 2, "firing + acked 都算未恢复"

    def test_alert_on_gpu_rolls_up_to_the_service(
        self, client: TestClient, db_session: Session
    ) -> None:
        f.build_chain(db_session)
        f.add_event(db_session, "evt_g", resource_id=f.GPU, occurred_at=f.NOW)
        f.add_alert(db_session, "alert_gpu", resource_id=f.GPU, evidence=["evt_g"])
        db_session.commit()

        item = list_workloads(client)["items"][0]
        assert item["alertCount"] == 1

    def test_counts_are_zero_when_nothing_happened(
        self, client: TestClient, db_session: Session
    ) -> None:
        """计数为 0 是**真实信息**（什么都没发生），与指标缺失不同。"""
        f.build_chain(db_session)
        db_session.commit()

        item = list_workloads(client)["items"][0]
        assert item["eventCount"] == 0
        assert item["alertCount"] == 0
        assert item["firingAlertCount"] == 0
        assert item["diagnosisId"] is None
        assert item["rootCause"] is None

    def test_linked_diagnosis_is_surfaced(self, client: TestClient, db_session: Session) -> None:
        """证据落在链路下层的诊断，也要出现在链顶服务卡片上。

        这正是"跨层关联"的意义：诊断为"容器的 GPU 显存耗尽"，业务方在
        **AI 服务的负载总览**里就应该看到这条结论。若只认
        `affected_resources == 工作负载 id`，下层的诊断会全部不可见。
        """
        from app.models import Diagnosis as DiagnosisRow

        f.build_chain(db_session)
        db_session.add(
            DiagnosisRow(
                id="diag_01JTEST",
                created_at=f.NOW,
                trigger={"anchorResourceId": f.AIS},
                root_cause="GPU_MEMORY_EXHAUSTED",
                confidence=0.92,
                confidence_breakdown=[
                    {"ruleId": "R-GPU-MEM-001", "contribution": 0.92, "observed": "98%"}
                ],
                affected_resources=[f.CTR],
                potentially_affected=[f.GPU],
                on_chain=[f.VM],
                evidence=[{"type": "event", "name": "gpu.memory.exhausted"}],
                recommendation=[],
                rule_set_version="rs-test-0.1.0",
                notes=[],
            )
        )
        db_session.commit()

        item = list_workloads(client)["items"][0]
        assert item["diagnosisId"] == "diag_01JTEST"
        assert item["rootCause"] == "GPU_MEMORY_EXHAUSTED"

    def test_unrelated_diagnosis_is_not_linked(
        self, client: TestClient, db_session: Session
    ) -> None:
        """另一条链路上的诊断不得挂到这个服务上 —— 否则"关联"就是噪声。"""
        from app.models import Diagnosis as DiagnosisRow

        f.build_chain(db_session)
        db_session.add(
            DiagnosisRow(
                id="diag_01JOTHER",
                created_at=f.NOW,
                trigger={"anchorResourceId": "vm:zsvirt:other"},
                root_cause="VM_DISK_IO_SATURATED",
                confidence=0.6,
                confidence_breakdown=[],
                affected_resources=["container:probe:probe-y:other"],
                potentially_affected=["vm:zsvirt:other"],
                on_chain=[],
                evidence=[{"type": "event", "name": "vm.disk.io_saturated"}],
                recommendation=[],
                rule_set_version="rs-test-0.1.0",
                notes=[],
            )
        )
        db_session.commit()

        assert list_workloads(client)["items"][0]["diagnosisId"] is None

    def test_most_recent_diagnosis_wins(self, client: TestClient, db_session: Session) -> None:
        from app.models import Diagnosis as DiagnosisRow

        f.build_chain(db_session)
        for diag_id, moment, cause in (
            ("diag_01JOLD", f.NOW - timedelta(hours=3), "GPU_MEMORY_EXHAUSTED"),
            ("diag_01JNEW", f.NOW - timedelta(minutes=5), "GPU_NEIGHBOR_CONTENTION"),
        ):
            db_session.add(
                DiagnosisRow(
                    id=diag_id,
                    created_at=moment,
                    trigger={"anchorResourceId": f.AIS},
                    root_cause=cause,
                    confidence=0.5,
                    confidence_breakdown=[],
                    affected_resources=[f.CTR],
                    potentially_affected=[],
                    on_chain=[],
                    evidence=[{"type": "event", "name": "x"}],
                    recommendation=[],
                    rule_set_version="rs-test-0.1.0",
                    notes=[],
                )
            )
        db_session.commit()

        item = list_workloads(client)["items"][0]
        assert item["diagnosisId"] == "diag_01JNEW"
        assert item["rootCause"] == "GPU_NEIGHBOR_CONTENTION"

    def test_diagnosis_on_the_service_itself_is_linked(
        self, client: TestClient, db_session: Session
    ) -> None:
        from app.models import Diagnosis as DiagnosisRow

        f.build_chain(db_session)
        db_session.add(
            DiagnosisRow(
                id="diag_01JSELF",
                created_at=f.NOW,
                trigger={"anchorResourceId": f.AIS},
                root_cause="VM_MEMORY_EXHAUSTED",
                confidence=0.55,
                confidence_breakdown=[],
                affected_resources=[f.AIS],
                potentially_affected=[],
                on_chain=[],
                evidence=[{"type": "event", "name": "vm.memory.high"}],
                recommendation=[],
                rule_set_version="rs-test-0.1.0",
                notes=[],
            )
        )
        db_session.commit()

        item = list_workloads(client)["items"][0]
        assert item["diagnosisId"] == "diag_01JSELF"
        assert item["rootCause"] == "VM_MEMORY_EXHAUSTED"


class TestFiltersAndErrors:
    def test_kind_defaults_to_ai_service(self, client: TestClient, db_session: Session) -> None:
        """契约把语义定为 ai_service —— 容器不得混进负载总览。"""
        f.build_chain(db_session)
        db_session.commit()

        items = list_workloads(client)["items"]
        assert [i["id"] for i in items] == [f.AIS]

    def test_kind_override_allows_other_layers(
        self, client: TestClient, db_session: Session
    ) -> None:
        """联调时借用同一套聚合看 vm 层。"""
        f.build_chain(db_session)
        db_session.commit()

        items = list_workloads(client, kind="vm")["items"]
        assert [i["id"] for i in items] == [f.VM]

    def test_invalid_kind_returns_400(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/workloads", params={"since": SINCE, "to": UNTIL, "kind": "nonsense"}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_placeholder_kind_rejected(self, client: TestClient) -> None:
        """`unresolved` 是占位类型，不是可查询层级。"""
        r = client.get(
            "/api/v1/workloads",
            params={"since": SINCE, "to": UNTIL, "kind": "unresolved"},
        )
        assert r.status_code == 400

    def test_inverted_window_returns_400(self, client: TestClient) -> None:
        r = client.get("/api/v1/workloads", params={"since": UNTIL, "to": SINCE})
        assert r.status_code == 400
        assert "since 必须早于 to" in r.json()["error"]["message"]

    def test_limit_is_enforced(self, client: TestClient) -> None:
        r = client.get("/api/v1/workloads", params={"since": SINCE, "to": UNTIL, "limit": 0})
        assert r.status_code == 422, "limit 越界由 FastAPI 参数校验拒绝"
