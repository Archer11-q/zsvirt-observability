"""渠道标注（provenance）与告警恢复开关的回归测试。

## 为什么单独开一个文件

赛题 X-07 的答复要求「一致标明来源，并区分**真实采集 / 真实回放 / 人工模拟**」。
项目为此定义了 `EvidenceSource` 枚举与 `DiagnosisEvidence.source` 字段 ——
但**没有任何真实数据流填过它**：`ingest` 把事件 `source` 一律写成 `probe`
（`EventSource` 只有三种，契约冻结，装不下"渠道"这一维），而
`service._event_evidence` 构造证据时不传 `source`，于是全部落到默认值 `PROBE`。

2026-09-30 在 8080 上跑完整演示路径时实测到的后果：

- 模拟 GPU 渠道产出的 `gpu.memory.exhausted` 事件落库后 `source="probe"`，
  与探针真实采集**完全无法区分**；
- 引擎里"本次诊断包含模拟数据来源的证据"那句提示**永不触发**，
  只在单测手搓 `Evidence` 时可见。一个只在测试里成立的诚实性机制，
  比没有更危险：它让人以为已经做过了。

修复方式：渠道走事件的 `raw.origin`（不动冻结的事件来源枚举），由
`diagnosis/service.py::_evidence_source` 映射成证据的 `source`。本文件锁住它。

## 第二个缺陷：恢复窗口为 0 曾经是"全部立刻恢复"

`reconcile` 里 `cutoff = moment - window`，`window=0` 时 `cutoff == moment`，
于是 `last_fired_at < cutoff` 对**每一条**告警成立 —— 与"关闭恢复"的意图
正好相反。演示环境因此在一个调度周期内丢掉全部 `firing` 告警。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.alerts.engine import ReconcileResult, reconcile
from app.config import Settings
from app.diagnosis.service import _evidence_source
from app.diagnosis.types import EvidenceSource
from app.zsvirt.scenarios import (
    SCENARIO_NAMES,
    SIMULATED_ORIGIN,
    build_batch,
    build_cross_layer_batches,
)


class TestScenarioProvenance:
    """演示载荷必须**自带**渠道标注，而不是靠报告里的一句提示。"""

    def test_every_scenario_event_is_marked_simulated(self) -> None:
        """除 `healthy` 外每个场景都应有事件，且**每条事件**都带渠道标注。

        `healthy` 是**负向基线**（"正常负载一条告警都不该产生"），
        它刻意没有事件 —— 所以这里断言的是"事件集合恰好等于除 healthy 之外
        的全部场景"，而不是"每个场景都有事件"。写成后者的话，测试会逼着
        有人给 healthy 塞一条假事件来让它变绿。
        """
        with_events = {name for name in SCENARIO_NAMES if build_batch(name)["events"]}
        assert with_events == set(SCENARIO_NAMES) - {"healthy"}, (
            f"事件集合不对：{sorted(with_events)}"
        )

        for name in SCENARIO_NAMES:
            for event in build_batch(name)["events"]:
                origin = (event.get("raw") or {}).get("origin")
                assert origin == SIMULATED_ORIGIN, (
                    f"场景 {name} 的事件 {event['type']} 缺少渠道标注（拿到 {origin!r}）"
                )

    def test_infrastructure_resources_carry_origin(self) -> None:
        """拓扑里点开 GPU 就能看到这些数字是谁给的，不必先翻事件的 raw。"""
        batch = build_batch("gpu_self_exhausted")
        marked = {
            r["kind"]: (r.get("attributes") or {}).get("origin")
            for r in batch["resources"]
            if r["kind"] in ("host", "gpu", "vgpu")
        }
        assert marked == {
            "host": SIMULATED_ORIGIN,
            "gpu": SIMULATED_ORIGIN,
            "vgpu": SIMULATED_ORIGIN,
        }

    def test_probe_fixture_replay_is_not_relabelled(self) -> None:
        """成员 A 的样例是**真实回放**，不能被演示载荷改写成"人工模拟"。

        这条与上面两条同等重要：诚实性是**双向**的 ——
        既不能把模拟说成真实，也不能把真实说成模拟。
        """
        _platform, symptom, gpu_root = build_cross_layer_batches()

        assert (gpu_root["events"][0].get("raw") or {}).get("origin") == SIMULATED_ORIGIN
        for event in symptom["events"]:
            assert "origin" not in (event.get("raw") or {}), (
                f"成员 A 的样例事件 {event.get('type')} 被打上了模拟来源标注"
            )

    def test_existing_marker_is_not_overwritten(self) -> None:
        from app.zsvirt.scenarios import _stamp_origin

        payload = {"raw": {"origin": "probe", "trace": 1}}
        result = _stamp_origin(payload, SIMULATED_ORIGIN)
        assert result["raw"]["origin"] == "probe"
        assert result["raw"]["trace"] == 1, "打标注时丢了 raw 里原有的字段"
        assert result is payload


class TestEvidenceSource:
    """事件 → 证据的来源映射。"""

    @pytest.mark.parametrize(
        ("origin", "expected"),
        [
            ("simulated", EvidenceSource.SIMULATED),
            ("zwatch", EvidenceSource.ZWATCH),
            ("zsvirt", EvidenceSource.ZSVIRT),
            ("probe", EvidenceSource.PROBE),
            ("derived", EvidenceSource.DERIVED),
        ],
    )
    def test_known_origins_map_through(self, origin: str, expected: EvidenceSource) -> None:
        event = SimpleNamespace(raw={"origin": origin})
        assert _evidence_source(event) is expected

    @pytest.mark.parametrize(
        "event",
        [
            SimpleNamespace(raw={}),
            SimpleNamespace(raw=None),
            SimpleNamespace(),  # 没有 raw 的历史数据
            SimpleNamespace(raw={"origin": "nonsense"}),
            SimpleNamespace(raw={"origin": 42}),
            SimpleNamespace(raw="not-a-dict"),
        ],
        ids=["empty", "none", "missing", "unknown", "non-string", "wrong-type"],
    )
    def test_unrecognized_falls_back_to_probe(self, event: SimpleNamespace) -> None:
        """未知取值回退到传输层事实（探针上报通道），不猜也不吞。"""
        assert _evidence_source(event) is EvidenceSource.PROBE


class TestEventEvidenceInheritsSource:
    """同一个事件展开出的**每一条**证据必须同源。

    实测过的缺陷：只在"事件"那条证据上标了来源，指标那几条漏了 ——
    同一次诊断里 `container.oom_killed` 是 `simulated`，而它派生的
    `exitCode` 却是 `probe`。同一条证据链自相矛盾比不标更糟：
    看的人会以为其中一条是真的。
    """

    @staticmethod
    def _event(origin: object) -> SimpleNamespace:
        from datetime import UTC, datetime

        return SimpleNamespace(
            id="evt_test",
            type="container.oom_killed",
            occurred_at=datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC),
            resource_id="container:probe:a:ctr",
            metrics={"value": 137, "exitCode": 137, "oomKilledCount": 3},
            raw={"origin": origin},
        )

    def test_metric_evidence_shares_the_event_source(self) -> None:
        from app.diagnosis.service import _event_evidence

        items = _event_evidence(self._event("simulated"))

        kinds = {e.kind for e in items}
        assert len(items) >= 2, "至少应有事件证据与指标证据"
        for item in items:
            assert item.source is EvidenceSource.SIMULATED, (
                f"{item.kind} 证据 {item.name} 的来源是 {item.source}，与事件不一致"
            )
        assert len(kinds) == 2, "事件证据与指标证据都应存在（否则这条测试是空转）"

    def test_unmarked_event_makes_all_evidence_probe(self) -> None:
        from app.diagnosis.service import _event_evidence

        for item in _event_evidence(self._event(None)):
            assert item.source is EvidenceSource.PROBE


class TestRecoveryCanBeDisabled:
    """`ALERT_RECOVERY_AFTER_SEC=0` 必须是"关闭"，不是"全部立刻恢复"。"""

    def test_zero_window_returns_early_without_touching_the_database(self) -> None:
        class _ExplodingSession:
            def __getattr__(self, name: str) -> object:
                raise AssertionError(f"关闭恢复时不得访问数据库（碰到了 session.{name}）")

        result = reconcile(_ExplodingSession(), recovery_after=timedelta(0))  # type: ignore[arg-type]

        assert result.disabled is True
        assert result.resolved == []
        assert result.checked == 0

    @pytest.mark.parametrize("seconds", [0, -1, -600])
    def test_non_positive_window_is_always_disabled(self, seconds: int) -> None:
        result = reconcile(object(), recovery_after=timedelta(seconds=seconds))  # type: ignore[arg-type]
        assert result.disabled is True

    def test_disabled_flag_is_serialized(self) -> None:
        """`disabled` 必须出现在 API/日志里 —— 否则"没恢复"与"关闭了恢复"分不清。"""
        assert ReconcileResult(disabled=True).as_dict() == {
            "checked": 0,
            "resolved": [],
            "disabled": True,
        }

    @pytest.mark.parametrize("value", [0, -1])
    def test_config_maps_non_positive_to_disabled_window(self, value: int) -> None:
        cfg = Settings.model_construct(alert_recovery_after_sec=value)
        assert cfg.alert_recovery_after == timedelta(0)

    def test_config_passes_positive_window_through(self) -> None:
        cfg = Settings.model_construct(alert_recovery_after_sec=600)
        assert cfg.alert_recovery_after == timedelta(minutes=10)
