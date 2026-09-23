"""ZSvirt 适配层：GPU 指标的三个可插拔渠道（`docs/DATA_MODEL.md` §4.2.1）。

赛题明文要求「作品需支持模拟数据或最小化降级模式，以便在缺少特定 GPU 硬件时
复现实验流程」。本模块提供该降级路径，并把"这些数字是真实的还是模拟的"变成
**结构上无法混淆**的事实：

| 渠道 | 何时可用 | `Observation.origin` |
|---|---|---|
| `ZWatchProvider` | ZSvirt `zwatch` 指标 API —— **命题方已确认启用**（X-07 关闭） | `zsvirt-zwatch` |
| `GuestSmiProvider` | VM 内探针执行 `nvidia-smi` | `probe` |
| `SimulatedProvider` | **始终可用**，可复现 | `simulated` |

## 为什么 `origin` 是必填字段

`DATA_MODEL.md` §4.2.1 的诚实性要求是：模拟数据必须**可识别**，不得伪装成真实
采集。让 `origin` 成为 `Observation` 的必填字段，而不是"可选标签"，
是为了让"忘了标注来源"在类型层面就不可能发生 —— 一个忘了标注的模拟读数，
在演示现场会被当成真实 GPU 数据来讲解。

## 关于三个渠道的实现状态

- `ZWatchProvider` **已实现**（2026-09-21）。命题方答复确认 ZWatch 查询能力在测试
  环境已启用、8 个 GET 接口实测可用（关闭外部阻塞 X-07），因此按已确认的路由与
  指标名实现了真实适配。HTTP 与响应解析在 `app/zsvirt/watch.py`，本模块只做
  渠道语义的映射（快照 → `GpuMetricReading`）。
- `GuestSmiProvider` **不由本模块实现**。它的数据实际由成员 A 的探针在 VM 内采集
  （探针已在跑 `nvidia-smi`），因此接入点在 ingest 侧（`origin="probe"`）。
- `SimulatedProvider` 始终可用，是赛题明文要求的降级路径。
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from app.zsvirt.watch import (
    GPU_METRIC_NAMES,
    CardSnapshot,
    GpuDeviceInfo,
    ZWatchClient,
    ZWatchConfig,
    ZWatchSchemaError,
    ZWatchUnavailable,
    latest_snapshot_per_gpu,
)

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


#: 归因口径。直通与 vGPU 切分下"本机占用"是两个不同的量，**不得混为一谈**。
#:
#: - `"partitioned"`：本 VM 有独立的 vGPU 分区，`self_vgpu_mem_usage_pct` 是
#:   该分区的占用 —— 这是原始设计里 GPU 归因的依据。
#: - `"passthrough"`：整卡直通给本 VM（命题方确认的当前环境）。
#:   此时平台侧**没有**"本 VM 在卡上占了多少"这个量（无 MDEV 实例），
#:   因此卡级使用率**同时**代表宿主与本机，两个字段取同一个值，
#:   而"自己超配 vs 邻居干扰"的区分**在直通下不可得**。
AttributionBasis = Literal["partitioned", "passthrough"]


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
    #: 直通模式下它与 `host_mem_usage_pct` 相等，见 `attribution`。
    self_vgpu_mem_usage_pct: float
    utilization_pct: float
    #: 精确已用显存（字节）。**可为 None**：命题方明确 `GpuMemoryUtilization`
    #: 是使用率而非字节数，没有访客侧 `nvidia-smi`/NVML 数据时拿不到准确字节数。
    #: 从百分比 ×总容量反推出来的数字**不是观测值**，不写在这里。
    mem_used_bytes: int | None = None
    mem_total_bytes: int = 0
    #: 温度（℃）。**可为 None**：该指标可能缺采样（见 `watch.py` 的
    #: "读不到 vs 数值为 0" 处理）。
    temperature_c: float | None = None
    #: 本 VM 的 vGPU 配额（字节）与实际分配。超过配额即 `quota_exceeded`。
    #: 直通模式下无配额概念，两者皆为 None。
    quota_bytes: int | None = None
    allocated_bytes: int | None = None
    #: 归因口径。默认按原始设计取 `partitioned`；ZWatch 渠道如实报 `passthrough`。
    attribution: AttributionBasis = "partitioned"
    #: 采集侧的原始标签（ZWatch 的 `HostUuid` / `PciDeviceAddress` /
    #: `GpuSerialNumber`）。平台层 ID 桥接需要它们把读数锚到
    #: `{kind}:zsvirt:{uuid}` —— 见 `app/zsvirt/harvest.py`。
    labels: dict[str, object] = field(default_factory=dict)
    #: 该读数的额外说明（例如"直通模式下这是卡级使用率"）。
    notes: list[str] = field(default_factory=list)

    @property
    def is_simulated(self) -> bool:
        return self.origin == "simulated"

    @property
    def quota_usage_pct(self) -> float | None:
        if not self.quota_bytes:
            return None
        return round(100.0 * (self.allocated_bytes or 0) / self.quota_bytes, 2)

    @property
    def can_attribute(self) -> bool:
        """能否区分"自己超配"与"邻居干扰"。

        **直通模式下为 False**，这是事实而不是缺陷：没有分区就没有"本机占了多少"。
        调用方据此避免输出一个它其实证明不了的结论。
        """
        return self.attribution == "partitioned"


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
                            "self_vgpu_memory_usage": round(reading.self_vgpu_mem_usage_pct, 1),
                            "host_gpu_memory_usage": round(reading.host_mem_usage_pct, 1),
                        },
                        "message": (f"vGPU 显存使用率 {reading.self_vgpu_mem_usage_pct:.1f}%"),
                    }
                )
            elif reading.host_mem_usage_pct >= 90.0 and reading.self_vgpu_mem_usage_pct < 70.0:
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
                            "self_vgpu_memory_usage": round(reading.self_vgpu_mem_usage_pct, 1),
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
                        "message": (f"vGPU 配额使用 {reading.quota_usage_pct:.1f}%"),
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
        (host_lo, host_hi), (self_lo, self_hi), (quota_lo, quota_hi) = _PROFILE_RANGES[self.profile]

        host = round(self._rng.uniform(host_lo, host_hi), 1)
        self_usage = round(self._rng.uniform(self_lo, self_hi), 1)
        quota_usage = round(self._rng.uniform(quota_lo, quota_hi), 1)
        utilization = round(min(99.0, min(self_usage, 100.0) + self._rng.uniform(-5.0, 8.0)), 1)

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
    """ZSvirt `zwatch` 指标 API 渠道（**已实现**，2026-09-21）。

    依据命题方答复：ZWatch 查询能力在测试环境**已启用**，GPU 指标位于
    namespace `ZStack/Host`，可取 `GpuUtilization` / `GpuMemoryUtilization` /
    `GpuTemperature` / `GpuStatus` / `GpuPowerDraw`（关闭外部阻塞 X-07）。

    HTTP 与响应解析在 `app/zsvirt/watch.py`；本类只负责**渠道语义**：
    把一块卡的指标快照映射成 `GpuMetricReading`，并如实标注归因口径。

    ## 两条必须说清楚的限制（都来自命题方答复）

    1. **当前是 GPU 直通，不是 vGPU 切分**。答复明确"本次平台查询未发现 MDEV
       实例"。直通下平台侧没有按虚拟机维度的显存占用，因此
       `attribution="passthrough"`：卡级使用率同时代表宿主与本机，
       "自己超配 vs 邻居干扰"的区分**在本渠道不可得**，要靠访客侧探针补。
       写 `partitioned` 会让我们输出一个证明不了的结论。
    2. **`GpuMemoryUtilization` 是使用率，不是字节数**。答复原话：
       "不应直接写成'已用显存字节数'"。因此 `mem_used_bytes` 保持 `None` ——
       用百分比乘总容量得到的数字是**推算值**，把它当成观测值会污染证据链
       （`DIAGNOSIS_DESIGN.md` §4 要求证据可复算）。

    ## 缓存

    `read()` 结果按 `cache_ttl_sec` 缓存。没有缓存的话，前端的
    `/api/workloads` 15 秒轮询会变成对管理节点的 15 秒一次全量指标查询。
    """

    name: Origin = "zsvirt-zwatch"

    #: 读数缓存时长（秒）。略小于前端 `/api/workloads` 的 15 秒轮询：
    #: 保证每次轮询都拿到新数据，同时挡住同一请求内的重复查询。
    DEFAULT_CACHE_TTL_SEC = 10

    def __init__(
        self,
        *,
        endpoint: str = "",
        auth_token: str = "",
        auth_style: str = "oauth",
        access_key: str = "",
        secret_key: str = "",
        timeout_sec: float = 8.0,
        verify_tls: bool = False,
        cache_ttl_sec: int | None = None,
        gpu_selector: Callable[[CardSnapshot], bool] | None = None,
        client: ZWatchClient | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.auth_token = auth_token
        self.cache_ttl_sec = (
            cache_ttl_sec if cache_ttl_sec is not None else self.DEFAULT_CACHE_TTL_SEC
        )
        #: 在一台宿主有多块卡时挑出本 VM 直通的那块。默认取最新的那一块；
        #: 待拿到 VM↔GPU 绑定关系（外部阻塞 X-09）后由调用方传入精确判据。
        self.gpu_selector = gpu_selector
        self.client = client or ZWatchClient(
            config=ZWatchConfig(
                endpoint=endpoint,
                auth_style=auth_style,
                auth_token=auth_token,
                access_key=access_key,
                secret_key=secret_key,
                timeout_sec=timeout_sec,
                verify_tls=verify_tls,
            )
        )
        self._snapshots: list[CardSnapshot] | None = None
        self._snapshots_at: float = 0.0
        self._devices: list[GpuDeviceInfo] = []

    # ---- 探活 -------------------------------------------------------

    def is_available(self) -> bool:
        """真实探活。**不抛异常**（协议要求：探活不该崩）。

        先看凭据是否齐全（零成本），再发一次强制元数据查询（唯一有网络成本的路径）。
        """
        if not self.client.session.configured:
            return False
        return self.client.ping()

    # ---- 配置信息（供 /api/health 说明"为什么不可用"）----------------

    def describe(self) -> dict[str, object]:
        """渠道状态详情。**用于暴露缺口，不用于粉饰**。"""
        return {
            "endpoint": self.endpoint or None,
            "authStyle": self.client.session.style,
            "credentialsConfigured": self.client.session.configured,
            "attribution": "passthrough",
            "note": (
                "命题方确认当前环境为 GPU 直通：平台侧无按虚拟机维度的显存占用，"
                "归因需结合访客侧探针数据"
            ),
        }

    # ---- 静态资产 ---------------------------------------------------

    def assets(self) -> list[GpuAsset]:
        """GPU 静态资产。

        优先 `QueryGpuDevice`（权威：容量/型号/功耗/驱动），取不到时用指标标签
        兜底 —— 标签里至少有序列号或 PCI 地址，容量缺失时报 0 并在 `model`
        里标明来源，避免把 0 当成一个容量。
        """
        self._ensure_configured()
        devices = self.client.fetch_gpu_devices()
        if devices:
            self._devices = devices
            return [
                GpuAsset(
                    serial_number=d.serial_number,
                    mem_total_bytes=d.mem_total_bytes,
                    power_watts=d.power_watts,
                    is_driver_loaded=d.is_driver_loaded,
                    pci_address=d.pci_address,
                    model=d.model,
                )
                for d in devices
            ]

        snapshots = self._snapshot_list()
        if not snapshots:
            raise GpuMetricsUnavailable(
                "ZWatch 渠道读不到任何 GPU 指标样本，且 QueryGpuDevice 无返回"
                "（检查 ZSVIRT_ENDPOINT / 凭据 / ZWatch 权限）"
            )
        return [
            GpuAsset(
                serial_number=s.gpu_serial or s.gpu_identity,
                # 容量未知就报 0：**不猜**。上层已有 note 机制表达"缺失"，
                # 猜一个容量会让"显存耗尽"的阈值失去意义。
                mem_total_bytes=0,
                power_watts=0,
                is_driver_loaded=True,
                pci_address=s.pci_address or "",
                model="GPU (capacity unknown: QueryGpuDevice unavailable)",
            )
            for s in snapshots
        ]

    # ---- 指标读数 ---------------------------------------------------

    def read(self, *, at: datetime | None = None) -> GpuMetricReading:
        """读一次指标。取不到样本时**抛错**，不返回 0。"""
        self._ensure_configured()
        return self._to_reading(self._pick_snapshot(at=at), at=at)

    def _to_reading(
        self, snapshot: CardSnapshot, *, at: datetime | None = None
    ) -> GpuMetricReading:
        """快照 → 读数。`read()` 与 `read_all()` 共用，避免两处各写一遍
        而慢慢漂移。"""
        del at
        if snapshot.mem_usage_pct is None and snapshot.utilization_pct is None:
            raise GpuMetricsUnavailable(
                f"GPU {snapshot.gpu_identity} 在查询窗口内没有显存/利用率采样"
            )

        mem_pct = snapshot.mem_usage_pct if snapshot.mem_usage_pct is not None else 0.0
        util_pct = snapshot.utilization_pct if snapshot.utilization_pct is not None else 0.0

        total = 0
        for device in self._devices:
            if device.serial_number in (snapshot.gpu_serial, snapshot.gpu_identity) or (
                device.pci_address and device.pci_address == snapshot.pci_address
            ):
                total = device.mem_total_bytes
                break

        return GpuMetricReading(
            sampled_at=snapshot.at,
            origin=self.name,
            gpu_serial=snapshot.gpu_serial or snapshot.gpu_identity,
            host_mem_usage_pct=mem_pct,
            # 直通：卡级使用率就是本机使用率。**不假装**有分区数据。
            self_vgpu_mem_usage_pct=mem_pct,
            utilization_pct=util_pct,
            # 答复明确：使用率 ≠ 字节数。留 None 而不是反推一个看起来精确的数字。
            mem_used_bytes=None,
            mem_total_bytes=total,
            temperature_c=snapshot.temperature_c,
            quota_bytes=None,
            allocated_bytes=None,
            attribution="passthrough",
            labels=dict(snapshot.labels),
            notes=[
                "GPU 直通模式：memUsagePct 是**卡级**使用率，不是本虚拟机占用",
                "memUsedBytes 缺失：GpuMemoryUtilization 是使用率，不是字节数",
            ],
        )

    def read_all(self, *, at: datetime | None = None) -> list[GpuMetricReading]:
        """读**所有**可见 GPU 的读数。

        ZWatch 的 namespace 是 `ZStack/Host`，一块宿主上的卡都会返回。
        单独一个 `read()` 只能给一块卡，而平台层落图需要全部卡。
        """
        self._ensure_configured()
        snapshots = self._snapshot_list()
        if not snapshots:
            raise GpuMetricsUnavailable("ZWatch 渠道在查询窗口内没有返回任何 GPU 指标样本")
        return [self._to_reading(s, at=at) for s in snapshots]

    # ---- 内部 -------------------------------------------------------

    def _ensure_configured(self) -> None:
        """凭据不齐时给出**准确**的失败原因。

        不这么做的话，未配置的渠道会走到"窗口内没有样本"，把"没配"报成
        "环境没数据" —— 排查方向会被整个带偏。
        """
        if not self.client.session.configured:
            raise GpuMetricsUnavailable(
                "ZWatch 渠道凭据未配置：请设置 ZSVIRT_AUTH_TOKEN，"
                "或 ZSVIRT_ACCESS_KEY + ZSVIRT_SECRET_KEY（见 docs/DEPLOYMENT.md §4）"
            )

    def _snapshot_list(self) -> list[CardSnapshot]:
        import time as _time

        now = _time.monotonic()
        if self._snapshots and now - self._snapshots_at < self.cache_ttl_sec:
            return self._snapshots

        try:
            samples = self.client.fetch_samples(metrics=GPU_METRIC_NAMES)
        except ZWatchUnavailable:
            # 传输失败不缓存：下次调用应该重试，而不是把一次网络抖动
            # 记住 10 秒。
            raise

        snapshots = latest_snapshot_per_gpu(samples)
        if snapshots:
            self._snapshots = snapshots
            self._snapshots_at = now
        else:
            # 空结果**不缓存**：否则一次"恰好没有采样"的查询会让渠道
            # 在 TTL 内一直报告"有数据但为空"。
            self._snapshots = None
        return snapshots

    def _pick_snapshot(self, *, at: datetime | None = None) -> CardSnapshot:
        del at  # 查询窗口由 client 处理；这里只选卡
        snapshots = self._snapshot_list()
        if not snapshots:
            raise GpuMetricsUnavailable(
                "ZWatch 渠道在查询窗口内没有返回任何 GPU 指标样本"
                "（可能原因：该窗口无采样、权限不足、或响应形状与假设不符 —— "
                "最后一种会抛 ZWatchSchemaError 而不是走到这里）"
            )
        if self.gpu_selector is not None:
            selected = [s for s in snapshots if self.gpu_selector(s)]
            if not selected:
                raise GpuMetricsUnavailable(
                    f"gpu_selector 未匹配到任何 GPU；候选：{[s.gpu_identity for s in snapshots]}"
                )
            snapshots = selected
        # 默认取最新的一块。多卡宿主上的精确判据依赖 VM↔GPU 绑定（X-09）。
        return max(snapshots, key=lambda s: s.at)


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
    "AttributionBasis",
    "FaultProfile",
    "GpuAsset",
    "ZWatchSchemaError",
    "ZWatchUnavailable",
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
