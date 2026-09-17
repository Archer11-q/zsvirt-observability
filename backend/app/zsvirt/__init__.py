"""ZSvirt 适配层：GPU 指标的三个可插拔渠道（`docs/DATA_MODEL.md` §4.2.1）。

赛题明文要求「作品需支持模拟数据或最小化降级模式，以便在缺少特定 GPU 硬件时
复现实验流程」。本模块提供该降级路径，并把"这些数字是真实的还是模拟的"变成
**结构上无法混淆**的事实：

| 渠道 | 何时可用 | `Observation.origin` |
|---|---|---|
| `ZWatchProvider` | ZSvirt premium `zwatch` 指标 API —— **待命题方确认是否启用**（X-07） | `zsvirt-zwatch` |
| `GuestSmiProvider` | VM 内探针执行 `nvidia-smi` | `probe` |
| `SimulatedProvider` | **始终可用**，可复现 | `simulated` |

## 为什么 `origin` 是必填字段

`DATA_MODEL.md` §4.2.1 的诚实性要求是：模拟数据必须**可识别**，不得伪装成真实
采集。让 `origin` 成为 `Observation` 的必填字段，而不是"可选标签"，
是为了让"忘了标注来源"在类型层面就不可能发生 —— 一个忘了标注的模拟读数，
在演示现场会被当成真实 GPU 数据来讲解。

## 关于未实现的 ZWatchProvider / GuestSmiProvider

它们此刻**不写空壳代码**。原因：ZSvirt 的 `zwatch` 接口是否在测试环境启用尚未
确认（外部阻塞 X-07），而按未确认的接口写适配器，产出的代码无法验证、
只能靠猜。本模块把三者的**边界**（`GpuMetricsProvider` 协议）定下来，
等接口确认后填入实现即可，届时 `resolve_provider()` 的 `auto` 分支不需要改。
`GuestSmiProvider` 的数据实际由成员 A 的探针采集（探针已在 VM 内跑 `nvidia-smi`），
因此它的接入点在 ingest 侧（`origin="probe"`），不在本模块。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

#: 指标来源标识。取值与 `docs/DATA_MODEL.md` §4.2.1 的三层分工一致。
Origin = Literal["zsvirt-zwatch", "probe", "simulated"]

#: 故障剖面。与三类赛题场景对应（`DIAGNOSIS_DESIGN.md` §5）。
FaultProfile = Literal[
    "healthy",  # 正常
    "self_exhausted",  # 场景一：本机 vGPU 显存被自己打满
    "neighbor_contention",  # 场景一（另一种）：宿主显存被邻居占满，本机占用很低
    "quota_exceeded",  # 场景一（配额）：vGPU 配额被超过
]


class GpuMetricsUnavailable(RuntimeError):
    """该渠道当前不可用。

    **必须显式抛出而不是返回 0 或空**：调用方要能区分"指标读数是 0"
    （一个好消息）与"读不到指标"（一个需要暴露的缺口）。
    """


@dataclass(frozen=True)
class GpuAsset:
    """GPU 静态资产（ZSvirt 资产 API 的字段，权威且不随负载变化）。

    字段名取自 ZSvirt 源码的 `GpuDeviceVO`（`QueryGpuDevice`）——
    序列号 / 显存容量 / 功耗 / 驱动状态。**源码已证实它没有利用率、实时占用与温度**，
    因此性能指标必须来自另一层（ZWatch 或探针），这也是本节存在的原因。
    """

    serial_number: str
    mem_total_bytes: int
    power_watts: int
    is_driver_loaded: bool
    pci_address: str
    model: str


@dataclass(frozen=True)
class GpuMetricReading:
    """一次 GPU 指标读数。**`origin` 必填**（见模块文档）。"""

    sampled_at: datetime
    origin: Origin
    gpu_serial: str
    #: 整卡显存使用率（%）
    host_mem_usage_pct: float
    #: 本 VM 的 vGPU 显存使用率（%）。**这才是归因的关键**：
    #: 宿主高 + 本机低 ⇒ 邻居争用；宿主高 + 本机也高 ⇒ 本机超配。
    self_vgpu_mem_usage_pct: float
    utilization_pct: float
    mem_used_bytes: int
    mem_total_bytes: int
    temperature_c: float
    #: 本 VM 的 vGPU 配额（字节）与实际分配。超过配额即 `quota_exceeded`。
    quota_bytes: int | None = None
    allocated_bytes: int | None = None

    @property
    def is_simulated(self) -> bool:
        return self.origin == "simulated"

    @property
    def quota_usage_pct(self) -> float | None:
        if not self.quota_bytes:
            return None
        return round(100.0 * (self.allocated_bytes or 0) / self.quota_bytes, 2)


@dataclass
class ScenarioTimeline:
    """一个故障场景的指标时间线。

    `events()` 把时间线里"越界"的样本翻译成事件，让演示脚本不必自己判断阈值 ——
    阈值是规则数据（`app/diagnosis/rules.py`），这里只负责产生读数。
    """

    profile: FaultProfile
    readings: list[GpuMetricReading] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def events(self) -> list[dict]:
        """把时间线翻译成**上报契约形态**的事件列表。

        返回的字典可直接放进 `POST /api/v1/ingest/batch` 的 `events` 字段
        （除 `resourceRef`，那由调用方按自己的资源命名空间填）。
        """
        out: list[dict] = []
        for index, reading in enumerate(self.readings):
            if reading.self_vgpu_mem_usage_pct >= 90.0:
                out.append(
                    {
                        "occurredAt": reading.sampled_at.isoformat(),
                        "type": "gpu.memory.exhausted",
                        "metrics": {
                            "value": round(reading.self_vgpu_mem_usage_pct, 1),
                            "self_vgpu_memory_usage": round(
                                reading.self_vgpu_mem_usage_pct, 1
                            ),
                            "host_gpu_memory_usage": round(reading.host_mem_usage_pct, 1),
                        },
                        "message": (
                            f"vGPU 显存使用率 {reading.self_vgpu_mem_usage_pct:.1f}%"
                        ),
                    }
                )
            elif (
                reading.host_mem_usage_pct >= 90.0
                and reading.self_vgpu_mem_usage_pct < 70.0
            ):
                # 宿主有压力但本机占用低 → 邻居争用。
                # 事件类型用**已冻结**的 `gpu.utilization.high`（GPU 算力/资源告急），
                # 而"是谁占的"由指标对比得出，不由事件类型表达 ——
                # F-01 的 17 项里没有邻居争用事件，编一个不存在的类型会让规则成为死数据。
                out.append(
                    {
                        "occurredAt": reading.sampled_at.isoformat(),
                        "type": "gpu.utilization.high",
                        "metrics": {
                            "value": round(reading.host_mem_usage_pct, 1),
                            "host_gpu_memory_usage": round(reading.host_mem_usage_pct, 1),
                            "self_vgpu_memory_usage": round(
                                reading.self_vgpu_mem_usage_pct, 1
                            ),
                        },
                        "message": (
                            f"宿主 GPU 显存 {reading.host_mem_usage_pct:.1f}%，"
                            f"本机 vGPU 仅 {reading.self_vgpu_mem_usage_pct:.1f}%"
                        ),
                    }
                )

            if (
                reading.quota_bytes
                and reading.allocated_bytes
                and reading.allocated_bytes > reading.quota_bytes
            ):
                out.append(
                    {
                        "occurredAt": reading.sampled_at.isoformat(),
                        "type": "vgpu.quota.exceeded",
                        "metrics": {
                            "value": round(reading.quota_usage_pct or 0.0, 1),
                            "quota_bytes": reading.quota_bytes,
                            "allocated_bytes": reading.allocated_bytes,
                        },
                        "message": (
                            f"vGPU 配额使用 {reading.quota_usage_pct:.1f}%"
                        ),
                    }
                )
            del index
        return out


class GpuMetricsProvider(Protocol):
    """GPU 指标渠道的统一边界。

    三个实现（ZWatch / 探针 / 模拟）都只回答两个问题：**静态资产是什么**、
    **性能指标是多少**。上层的 `gpuProviderMode` 直接暴露 `name`，
    因此"当前用的是哪个渠道"对前端永远可见。
    """

    name: Origin

    def is_available(self) -> bool:
        """渠道当前是否可用。**不抛异常** —— 探活不该崩。"""
        ...

    def assets(self) -> list[GpuAsset]:
        """GPU 静态资产清单。不可用时抛 `GpuMetricsUnavailable`。"""
        ...

    def read(self, *, at: datetime | None = None) -> GpuMetricReading:
        """读一次性能指标。不可用时抛 `GpuMetricsUnavailable`。"""
        ...


# ================================================================ 模拟渠道


#: 模拟 GPU 的资产。固定值 —— 演示时的截图与文档能对上。
SIMULATED_GPU = GpuAsset(
    serial_number="SIM-A10-0001",
    mem_total_bytes=24 * 1024**3,
    power_watts=150,
    is_driver_loaded=True,
    pci_address="0000:01:00.0",
    model="NVIDIA A10 (simulated)",
)

#: 各故障剖面的指标区间 `(host_pct, self_pct, quota_pct)`。
#:
#: 前两个区间就是"GPU 归因"的全部依据：
#:   - `self_exhausted`：本机占用高 ⇒ 自己把显存打满了
#:   - `neighbor_contention`：宿主高、本机低 ⇒ 压力来自同宿主的其他负载
#:
#: 第三个是**配额使用率**，允许超过 100 —— 超配只有在百分比能超过 100 时才表达得出来。
#: 早先的实现把 `allocated_bytes` 从 `self_pct`（上限 80）反推，于是
#: `allocated > quota` 永远不成立，`quota_exceeded` 剖面**产生不出任何事件**。
_PROFILE_RANGES: dict[
    FaultProfile, tuple[tuple[float, float], tuple[float, float], tuple[float, float]]
] = {
    "healthy": ((20.0, 35.0), (15.0, 30.0), (18.0, 32.0)),
    "self_exhausted": ((80.0, 99.0), (88.0, 99.0), (88.0, 99.0)),
    "neighbor_contention": ((90.0, 99.0), (8.0, 22.0), (8.0, 22.0)),
    "quota_exceeded": ((30.0, 45.0), (60.0, 80.0), (105.0, 130.0)),
}


class SimulatedProvider:
    """可复现的模拟 GPU 指标渠道。

    **可复现是本类存在的全部理由**：演示需要在任何机器上给出**同一份**数据。
    因此随机数来自显式种子的 `random.Random(seed)` 实例 —— 不是全局 `random`，
    也不是 `time`。同一 `(profile, seed, step_seconds, samples)` 必然产生逐字节
    相同的时间线，`sample_timeline` 的测试就断言这一点。

    **不假装真实**：`name` 是 `"simulated"`，每条读数的 `origin` 也是 `simulated`，
    上层的 `gpuProviderMode` 会把它一路暴露到 API 响应。演示时必须明确说明
    这些数字是模拟的 —— 赛题要求的"可识别"就是这个意思。
    """

    name: Origin = "simulated"

    def __init__(self, *, profile: FaultProfile = "healthy", seed: int = 20260917) -> None:
        if profile not in _PROFILE_RANGES:
            raise ValueError(f"unknown profile: {profile!r}；允许：{sorted(_PROFILE_RANGES)}")
        self.profile = profile
        self.seed = seed
        self._rng = random.Random(f"{seed}:{profile}")

    def is_available(self) -> bool:
        """模拟渠道**永远可用** —— 这正是降级模式的意义。"""
        return True

    def assets(self) -> list[GpuAsset]:
        return [SIMULATED_GPU]

    def read(self, *, at: datetime | None = None) -> GpuMetricReading:
        moment = at or datetime.now(UTC)
        (host_lo, host_hi), (self_lo, self_hi), (quota_lo, quota_hi) = _PROFILE_RANGES[
            self.profile
        ]

        host = round(self._rng.uniform(host_lo, host_hi), 1)
        self_usage = round(self._rng.uniform(self_lo, self_hi), 1)
        quota_usage = round(self._rng.uniform(quota_lo, quota_hi), 1)
        utilization = round(
            min(99.0, min(self_usage, 100.0) + self._rng.uniform(-5.0, 8.0)), 1
        )

        total = SIMULATED_GPU.mem_total_bytes
        # 整卡已用 = 宿主使用率；本机占用按本机百分比折算
        mem_used = int(total * host / 100.0)
        quota = total // 4  # 1g 切分
        # 配额占用**不**从 `self_usage` 反推（那样永远超不过配额）：
        # 由配额使用率推出字节数，于是超配是可表达的。
        allocated = int(quota * quota_usage / 100.0)

        return GpuMetricReading(
            sampled_at=moment,
            origin=self.name,
            gpu_serial=SIMULATED_GPU.serial_number,
            host_mem_usage_pct=host,
            # 展示值封顶 100：物理上不可能用掉超过卡上分给自己的那部分，
            # 超出配额的部分会溢出到宿主内存 —— 超配由 `quota_usage_pct` 表达。
            self_vgpu_mem_usage_pct=min(self_usage, 100.0),
            utilization_pct=utilization,
            mem_used_bytes=mem_used,
            mem_total_bytes=total,
            temperature_c=round(self._rng.uniform(45.0, 78.0), 1),
            quota_bytes=quota,
            allocated_bytes=allocated,
        )

    def sample_timeline(
        self,
        *,
        samples: int = 6,
        step_seconds: int = 30,
        start: datetime | None = None,
    ) -> ScenarioTimeline:
        """按固定步长采样一条时间线。**同一参数 → 同一结果**。"""
        if samples < 1:
            raise ValueError("samples must be >= 1")
        if step_seconds < 1:
            raise ValueError("step_seconds must be >= 1")

        first = start or datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
        readings = [
            self.read(at=first + timedelta(seconds=step_seconds * i)) for i in range(samples)
        ]
        return ScenarioTimeline(
            profile=self.profile,
            readings=readings,
            notes=[
                f"模拟渠道（SimulatedProvider），profile={self.profile}，seed={self.seed}",
                "这些数字**不是**真实采集的：origin=simulated 会出现在每条读数与 API 响应里",
            ],
        )


# ================================================================ 渠道解析


class ZWatchProvider:
    """ZSvirt premium `zwatch` 指标 API 渠道（**未实现，等待接口确认**）。

    不写占位实现的原因：`zwatch` 是否在测试环境启用尚未确认（外部阻塞 X-07），
    按未确认的接口写适配器无法验证。保留类是为了让 `resolve_provider()`
    的装配点稳定 —— 接口确认后只填 `read()`，不改调用方。

    **`is_available()` 恒为 False**，因此它永远不会被 `auto` 选中，
    也不会给出任何看似真实的读数。
    """

    name: Origin = "zsvirt-zwatch"

    def __init__(self, *, endpoint: str = "", auth_token: str = "") -> None:
        self.endpoint = endpoint
        self.auth_token = auth_token

    def is_available(self) -> bool:
        # 接口未确认（X-07）之前，这个渠道一律不可用 —— 宁可降级到模拟并**说明**，
        # 也不要对着一个猜出来的 URL 发请求然后返回空数据。
        return False

    def assets(self) -> list[GpuAsset]:
        raise GpuMetricsUnavailable(
            "ZWatch 指标渠道尚未接入：ZSvirt premium `zwatch` 是否在测试环境启用待确认"
            "（见 docs/DECISIONS.md 外部阻塞 X-07）"
        )

    def read(self, *, at: datetime | None = None) -> GpuMetricReading:
        del at
        raise GpuMetricsUnavailable(
            "ZWatch 指标渠道尚未接入：ZSvirt premium `zwatch` 是否在测试环境启用待确认"
            "（见 docs/DECISIONS.md 外部阻塞 X-07）"
        )


def resolve_provider(
    *,
    configured: str = "auto",
    simulated_enabled: bool = False,
    zsvirt_endpoint: str = "",
    zsvirt_auth_token: str = "",
) -> GpuMetricsProvider:
    """按配置选择 GPU 指标渠道。

    `auto` 的判定顺序是**从真实到降级**：ZWatch（真实）→ 模拟。探针渠道不在
    这里选 —— 它的数据由成员 A 的探针推过来（`origin="probe"`），接入点在 ingest 侧。

    `SIMULATED_DATA_ENABLED=true` 会**强制**使用模拟渠道，即使 ZWatch 可用：
    演示时需要一个确定性的、与真机状态无关的数据源，这个开关就是它的入口。
    """
    if simulated_enabled or configured == "simulated":
        return SimulatedProvider()

    zwatch = ZWatchProvider(endpoint=zsvirt_endpoint, auth_token=zsvirt_auth_token)
    if configured in ("auto", "zsvirt-zwatch") and zwatch.is_available():
        return zwatch

    if configured == "zsvirt-zwatch":
        # 显式指定了 ZWatch 但它不可用：**降级到模拟并让调用方看到**
        # （`gpuProviderMode` 会是 simulated）。不抛异常 —— 演示现场不能因为
        # 一个未接入的渠道而整体起不来。
        return SimulatedProvider()

    return SimulatedProvider()


def describe_provider(provider: GpuMetricsProvider) -> dict[str, object]:
    """把渠道状态整理成可放进 `/api/health` 的字典。

    与 `Settings.gpu_provider_mode` 的语义一致：`simulated_enabled` 优先。
    """
    return {
        "mode": provider.name,
        "available": provider.is_available(),
        "simulated": provider.name == "simulated",
    }


__all__ = [
    "SIMULATED_GPU",
    "FaultProfile",
    "GpuAsset",
    "GpuMetricReading",
    "GpuMetricsProvider",
    "GpuMetricsUnavailable",
    "Origin",
    "ScenarioTimeline",
    "SimulatedProvider",
    "ZWatchProvider",
    "describe_provider",
    "resolve_provider",
]
