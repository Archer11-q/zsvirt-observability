"""模拟 GPU 指标渠道测试（`docs/DEVELOPMENT_PLAN.md` 任务 5 的可做部分）。

赛题要求"支持模拟数据或最小化降级模式"。这个渠道的价值全在两点，测试也就围绕它们：

1. **可复现** —— 同一参数必须产生逐字节相同的时间线。演示现场换台机器、
   或者重跑一次，数据必须一样，否则讲解词对不上界面。
2. **可识别** —— 模拟数据的来源必须一路暴露到 API 响应，不能伪装成真实采集。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.zsvirt import (
    SIMULATED_GPU,
    GpuMetricsUnavailable,
    SimulatedProvider,
    ZWatchProvider,
    describe_provider,
    resolve_provider,
)

PROFILES = ("healthy", "self_exhausted", "neighbor_contention", "quota_exceeded")


class TestSimulatedProviderReproducibility:
    def test_same_parameters_give_identical_timelines(self) -> None:
        """**本渠道存在的理由**：演示必须可复现。"""
        a = SimulatedProvider(profile="self_exhausted", seed=42).sample_timeline(samples=5)
        b = SimulatedProvider(profile="self_exhausted", seed=42).sample_timeline(samples=5)

        assert [r.host_mem_usage_pct for r in a.readings] == [
            r.host_mem_usage_pct for r in b.readings
        ]
        assert [r.self_vgpu_mem_usage_pct for r in a.readings] == [
            r.self_vgpu_mem_usage_pct for r in b.readings
        ]

    def test_different_seeds_give_different_timelines(self) -> None:
        """若换个种子结果一样，说明随机源没被真正用上（例如误用了固定值）。"""
        a = SimulatedProvider(profile="healthy", seed=1).sample_timeline(samples=5)
        b = SimulatedProvider(profile="healthy", seed=2).sample_timeline(samples=5)
        assert [r.host_mem_usage_pct for r in a.readings] != [
            r.host_mem_usage_pct for r in b.readings
        ]

    def test_does_not_use_the_global_random_module(self) -> None:
        """用显式 `random.Random(seed)` 实例，避免被同进程的其他代码影响。

        若用全局 `random`，同一测试进程里先跑过的别的测试会改变序列，
        表现为"单独跑结果对、全量跑结果变" —— 那是调试成本最高的一类失败。
        """
        import random

        random.seed(999)
        first = SimulatedProvider(seed=5).sample_timeline(samples=3).readings
        random.seed(1)
        second = SimulatedProvider(seed=5).sample_timeline(samples=3).readings
        assert [r.host_mem_usage_pct for r in first] == [
            r.host_mem_usage_pct for r in second
        ]

    def test_unknown_profile_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown profile"):
            SimulatedProvider(profile="nonsense")  # type: ignore[arg-type]

    def test_sample_timeline_validates_arguments(self) -> None:
        provider = SimulatedProvider()
        with pytest.raises(ValueError, match="samples"):
            provider.sample_timeline(samples=0)
        with pytest.raises(ValueError, match="step_seconds"):
            provider.sample_timeline(step_seconds=0)

    def test_timeline_is_evenly_spaced_from_the_given_start(self) -> None:
        start = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
        timeline = SimulatedProvider().sample_timeline(
            samples=3, step_seconds=20, start=start
        )
        stamps = [r.sampled_at for r in timeline.readings]
        assert stamps[0] == start
        assert (stamps[1] - stamps[0]).total_seconds() == 20
        assert (stamps[2] - stamps[1]).total_seconds() == 20


class TestSimulatedProviderHonesty:
    def test_every_reading_is_marked_simulated(self) -> None:
        """**诚实性要求**：模拟数据必须可识别（`DATA_MODEL.md` §4.2.1）。"""
        timeline = SimulatedProvider(profile="self_exhausted").sample_timeline(samples=3)
        assert all(r.origin == "simulated" for r in timeline.readings)
        assert all(r.is_simulated for r in timeline.readings)

    def test_provider_mode_is_exposed(self) -> None:
        provider = SimulatedProvider()
        described = describe_provider(provider)
        assert described["mode"] == "simulated"
        assert described["simulated"] is True
        assert described["available"] is True

    def test_timeline_notes_say_the_numbers_are_not_real(self) -> None:
        timeline = SimulatedProvider().sample_timeline(samples=1)
        joined = " ".join(timeline.notes)
        assert "simulated" in joined
        assert "不是" in joined, "说明里必须明确写出这些数字不是真实采集的"

    def test_simulated_provider_is_always_available(self) -> None:
        """降级模式的意义就是"始终可用"。"""
        assert SimulatedProvider().is_available() is True


class TestFaultProfiles:
    """四个剖面必须真的产生各自的特征，否则规则无从区分。"""

    @pytest.mark.parametrize("profile", PROFILES)
    def test_values_stay_in_physical_range(self, profile: str) -> None:
        timeline = SimulatedProvider(profile=profile, seed=11).sample_timeline(samples=8)
        for reading in timeline.readings:
            assert 0.0 <= reading.host_mem_usage_pct <= 100.0
            assert 0.0 <= reading.self_vgpu_mem_usage_pct <= 100.0
            assert 0.0 <= reading.utilization_pct <= 100.0
            assert reading.mem_used_bytes <= reading.mem_total_bytes
            assert reading.allocated_bytes is not None

    def test_healthy_produces_no_events(self) -> None:
        """正常剖面**一条事件都不能产生** —— 否则演示一开始就是一片告警。"""
        timeline = SimulatedProvider(profile="healthy", seed=3).sample_timeline(samples=10)
        assert timeline.events() == []

    def test_self_exhausted_produces_memory_exhausted_events(self) -> None:
        timeline = SimulatedProvider(profile="self_exhausted", seed=4).sample_timeline(
            samples=4
        )
        types = {e["type"] for e in timeline.events()}
        assert types == {"gpu.memory.exhausted"}, types

    def test_neighbour_contention_produces_high_utilization_events(self) -> None:
        """邻居剖面：宿主高、本机低 ⇒ 报 `gpu.utilization.high`。

        事件类型用已冻结的取值；"是谁占的"由**指标对比**表达，不由事件类型表达 ——
        F-01 的 17 项里没有邻居争用事件，编一个不存在的类型会让规则成为死数据。
        """
        timeline = SimulatedProvider(profile="neighbor_contention", seed=4).sample_timeline(
            samples=4
        )
        types = {e["type"] for e in timeline.events()}
        assert types == {"gpu.utilization.high"}, types

    def test_quota_exceeded_profile_really_exceeds_the_quota(self) -> None:
        """**回归测试**：该剖面此前产生不出任何事件。

        早期实现把 `allocated_bytes` 从上限 80% 的 `self_pct` 反推，
        于是 `allocated > quota` 永远不成立 —— 剖面存在、不可测。
        """
        timeline = SimulatedProvider(profile="quota_exceeded", seed=6).sample_timeline(
            samples=6
        )
        assert any((r.quota_usage_pct or 0) > 100 for r in timeline.readings)
        types = {e["type"] for e in timeline.events()}
        assert "vgpu.quota.exceeded" in types, types

    def test_neighbour_reading_carries_both_metrics(self) -> None:
        """两条指标必须同时在事件里 —— 归因全靠它们的对比。"""
        timeline = SimulatedProvider(profile="neighbor_contention", seed=8).sample_timeline(
            samples=3
        )
        event = timeline.events()[0]
        assert "host_gpu_memory_usage" in event["metrics"]
        assert "self_vgpu_memory_usage" in event["metrics"]
        assert event["metrics"]["host_gpu_memory_usage"] > 90
        assert event["metrics"]["self_vgpu_memory_usage"] < 70

    def test_events_are_ingest_shaped(self) -> None:
        """事件必须是**上报契约形态**的普通字典，演示脚本可以直接塞进 batch。"""
        timeline = SimulatedProvider(profile="self_exhausted", seed=9).sample_timeline(
            samples=2
        )
        for event in timeline.events():
            assert set(event) <= {"occurredAt", "type", "metrics", "message"}
            assert isinstance(event["metrics"], dict)
            datetime.fromisoformat(event["occurredAt"])


class TestProviderResolution:
    def test_auto_falls_back_to_simulated(self) -> None:
        """ZWatch 未接入（X-07）⇒ `auto` 落到模拟，而不是抛错。

        演示现场不能因为一个未接入的渠道而整体起不来。
        """
        assert resolve_provider(configured="auto").name == "simulated"

    def test_explicit_simulation_wins(self) -> None:
        """`SIMULATED_DATA_ENABLED=true` 强制使用模拟渠道，即使真渠道可用。

        演示需要与真机状态无关的确定性数据源，这个开关就是它的入口。
        """
        assert resolve_provider(simulated_enabled=True).name == "simulated"
        assert resolve_provider(configured="zsvirt-zwatch", simulated_enabled=True).name == (
            "simulated"
        )

    def test_explicit_zwatch_degrades_instead_of_failing(self) -> None:
        provider = resolve_provider(configured="zsvirt-zwatch")
        assert provider.name == "simulated"

    def test_zwatch_provider_reports_unavailable(self) -> None:
        assert ZWatchProvider().is_available() is False

    def test_zwatch_provider_raises_rather_than_returning_zeros(self) -> None:
        """读不到指标必须**抛错**，不能返回 0。

        返回 0 会被上层当成"显存占用 0%"这个**好消息**，而实际是读不到 ——
        这是最坏的一类错误，因为界面看起来一切正常。
        """
        provider = ZWatchProvider()
        with pytest.raises(GpuMetricsUnavailable, match="X-07"):
            provider.read()
        with pytest.raises(GpuMetricsUnavailable):
            provider.assets()

    def test_asset_fields_match_the_zsvirt_source(self) -> None:
        """静态资产字段取自 ZSvirt `GpuDeviceVO` —— 它**没有**利用率/占用/温度。

        这条断言记录了那个事实：性能指标必须来自另一层，不能指望资产 API。
        """
        asset = SimulatedProvider().assets()[0]
        assert asset.serial_number
        assert asset.mem_total_bytes > 0
        assert asset.power_watts > 0
        assert asset.is_driver_loaded is True
        assert asset.model.startswith("NVIDIA")
        # 资产结构里没有性能字段（那正是本模块存在的原因）
        assert not hasattr(asset, "utilization_pct")

    def test_simulated_asset_is_stable(self) -> None:
        """资产是固定的：演示截图与文档能对上。"""
        assert SimulatedProvider().assets()[0] == SIMULATED_GPU
