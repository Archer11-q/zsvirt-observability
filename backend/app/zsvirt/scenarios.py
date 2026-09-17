"""三类赛题故障场景的**可复现**定义（演示与联调共用）。

`docs/backend/DIAGNOSIS_DESIGN.md` §5 定义了三个场景，本模块把它们变成可以直接
灌进 `POST /api/v1/ingest/batch` 的载荷：

| 场景 | 关键事件 | 期望根因 |
|---|---|---|
| `gpu_self_exhausted` | `gpu.memory.exhausted` | `GPU_MEMORY_EXHAUSTED` |
| `gpu_neighbor_contention` | `gpu.utilization.high` + 指标对比 | `GPU_NEIGHBOR_CONTENTION` |
| `container_oom` | `container.oom_killed` + `container.restart` | `CONTAINER_MEMORY_LIMIT` |
| `network_failure` | `container.network.unreachable` + `agent.task.failed` | `NETWORK_UNREACHABLE` |
| `healthy` | 无异常 | 无告警、无诊断 |

## 与 `tests/_scenarios.py` 的分工

| | 本模块 | `tests/_scenarios.py` |
|---|---|---|
| 面向 | 演示、现场复现、手工联调 | 自动化测试 |
| 数据来源 | `SimulatedProvider`（指标由渠道产生） | 手写固定值 |
| 变更频率 | 跟着演示脚本走 | 跟着测试走 |

两者刻意不合并：测试夹具要求"永不因演示需要而改动"，而演示数据会随讲解重点调整。
把它们耦合起来会让"改演示"看起来像"改测试断言"。

## 可复现性

所有场景的 `batchId`、时间戳、GPU 指标都由固定参数决定，因此**同一台机器上
重复运行得到同样的数字**。指标来自 `SimulatedProvider(profile=..., seed=...)`，
种子常量在 `_SEEDS` 里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.enums import EventType, ResourceKind
from app.zsvirt import FaultProfile, SimulatedProvider

#: 演示用的固定时间基准（与 `app.demo.DEMO_NOW` 一致）。
_SCENARIO_NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)

#: 探针标识与 VM。**必须是 B 能识别的全局 ID 形态**（`vm:zsvirt:<uuid>`），
#: 否则会退化成占位节点，症状是"所有资源都挂在 unresolved 下"。
DEMO_AGENT_ID = "demo-agent"
DEMO_VM_ID = "vm:zsvirt:demo0000-0000-4000-8000-000000000001"

#: 每个场景的模拟种子。固定值 —— 演示必须可复现。
_SEEDS: dict[str, int] = {
    "healthy": 1001,
    "gpu_self_exhausted": 2002,
    "gpu_neighbor_contention": 3003,
    "container_oom": 4004,
    "network_failure": 5005,
}

#: 场景 → （模拟剖面，说明）。`None` 表示该场景不由 GPU 渠道驱动。
_SCENARIOS: dict[str, tuple[FaultProfile | None, str]] = {
    "healthy": ("healthy", "正常负载：指标平稳，**不产生任何告警**（负向基线）"),
    "gpu_self_exhausted": (
        "self_exhausted",
        "场景一：本机 vGPU 显存被自己打满 → GPU_MEMORY_EXHAUSTED（自己超配）",
    ),
    "gpu_neighbor_contention": (
        "neighbor_contention",
        "场景一（归因区分）：宿主显存满而本机占用很低 → GPU_NEIGHBOR_CONTENTION（邻居占用）",
    ),
    "container_oom": (
        None,
        "场景二：容器被 OOM Killer 终止并反复重启 → CONTAINER_MEMORY_LIMIT",
    ),
    "network_failure": (
        None,
        "场景三：容器网络不可达导致智能体任务失败 → NETWORK_UNREACHABLE",
    ),
}

SCENARIO_NAMES: tuple[str, ...] = tuple(_SCENARIOS)

#: 工作负载层资源的后缀。默认按场景区分，让三类故障成为**独立事故**；
#: 显式传 `suffix=""` 可以让它们共用同一套资源，用来**演示事故聚合**行为。
DEFAULT_SUFFIX_BY_SCENARIO: dict[str, str] = {
    "healthy": "healthy",
    "gpu_self_exhausted": "gpu-self",
    "gpu_neighbor_contention": "gpu-neigh",
    "container_oom": "oom",
    "network_failure": "net",
}


def scenario_suffix(name: str, override: str | None = None) -> str:
    """该场景工作负载层资源的后缀。"""
    if override is not None:
        return override
    return DEFAULT_SUFFIX_BY_SCENARIO.get(name, name)


def describe_scenarios() -> dict[str, str]:
    """`场景名 → 说明`，供 `python -m app.demo list` 与文档使用。"""
    return {name: description for name, (_, description) in _SCENARIOS.items()}


def _chain_resources(
    *,
    suffix: str,
    gpu_attributes: dict[str, Any] | None = None,
    container_attributes: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """标准链路：host → gpu → vgpu → vm → container → ai_service → agent。

    **必须完整上报**：链条上少一环就会让 `parentSourceId` 指向不存在的 id，
    表现为外键违约导致整批 503（这个错踩过）。

    `suffix` 只加在**工作负载层**（容器 / AI 服务 / 智能体）上，基础设施层
    （host / gpu / vgpu / vm）保持共享 —— 真实部署就是一台机器上跑多个工作负载。

    为什么必须区分：三类故障若共用同一个容器、且时间戳相同，事故聚合会（正确地）
    把它们并成**一次**事故、只给**一个**根因。但一个容器不可能在同一瞬间既 OOM
    又网络不可达 —— 那是夹具不真实，不是算法错。真实场景下一台故障对应一个工作负载。
    """
    return [
        {
            "kind": ResourceKind.HOST.value,
            "sourceId": "demo-host",
            "name": "demo-node-01",
            "status": "running",
        },
        {
            "kind": ResourceKind.GPU.value,
            "sourceId": "demo-gpu0",
            "name": "A10-0",
            "parentSourceId": "demo-host",
            "status": "running",
            "attributes": gpu_attributes or {},
        },
        {
            "kind": ResourceKind.VGPU.value,
            "sourceId": "demo-vgpu0",
            "name": "A10-0-1g",
            "parentSourceId": "demo-gpu0",
            "status": "running",
        },
        {
            "kind": ResourceKind.VM.value,
            "sourceId": "self",
            "name": "demo-vm",
            "parentSourceId": "demo-vgpu0",
            "status": "running",
        },
        {
            "kind": ResourceKind.CONTAINER.value,
            "sourceId": f"demo-ctr-{suffix}",
            "name": f"demo-ctr-{suffix}",
            "parentSourceId": "self",
            "status": "running",
            "attributes": container_attributes or {"image": "vllm/vllm-openai"},
        },
        {
            "kind": ResourceKind.AI_SERVICE.value,
            "sourceId": f"demo-svc-{suffix}",
            "name": f"demo-svc-{suffix}",
            "parentSourceId": f"demo-ctr-{suffix}",
            "status": "running",
            "attributes": {
                "framework": "vllm",
                "modelName": "Qwen2.5-7B-Instruct",
                "endpoint": "http://127.0.0.1:8000/v1",
                "concurrency": 8,
            },
        },
        {
            "kind": ResourceKind.AGENT.value,
            "sourceId": f"demo-agent-{suffix}",
            "name": f"demo-agent-{suffix}",
            "parentSourceId": f"demo-svc-{suffix}",
            "status": "running",
        },
    ]


def _gpu_events(profile: FaultProfile, *, at: datetime, samples: int = 3) -> list[dict[str, Any]]:
    """用模拟渠道产生 GPU 事件（指标由渠道给出，不由本模块硬编码）。

    GPU 故障的资源是**共享的 vGPU**（基础设施层），因此这里不加 suffix：
    显存耗尽本来就是整卡的资源问题，不是某个工作负载私有的。
    """
    provider = SimulatedProvider(profile=profile, seed=_SEEDS[_scenario_for_profile(profile)])
    timeline = provider.sample_timeline(samples=samples, step_seconds=15, start=at)
    events: list[dict[str, Any]] = []
    for raw in timeline.events():
        event = dict(raw)
        event["resourceRef"] = {"kind": ResourceKind.VGPU.value, "sourceId": "demo-vgpu0"}
        events.append(event)
    return events


def _scenario_for_profile(profile: FaultProfile) -> str:
    for name, (candidate, _) in _SCENARIOS.items():
        if candidate == profile:
            return name
    raise KeyError(profile)


def _gpu_attributes(profile: FaultProfile) -> dict[str, Any]:
    """GPU 静态资产 + 首次读数，让工作负载视图能显示真实容量。"""
    provider = SimulatedProvider(profile=profile, seed=_SEEDS[_scenario_for_profile(profile)])
    reading = provider.read(at=_SCENARIO_NOW)
    asset = provider.assets()[0]
    return {
        # 资产来自 ZSvirt 资产 API 的字段（权威、静态）
        "serialNumber": asset.serial_number,
        "memTotalBytes": asset.mem_total_bytes,
        "power": asset.power_watts,
        "isDriverLoaded": asset.is_driver_loaded,
        "model": asset.model,
        # 性能字段来自指标渠道（origin 标注在事件上）
        "memUsedBytes": reading.mem_used_bytes,
        "utilizationPct": reading.utilization_pct,
        "temperatureC": reading.temperature_c,
    }


def _batch(
    name: str,
    *,
    moment: datetime,
    batch_id: str | None,
    resources: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """（`resources` 已由调用方按 `suffix` 构造好，这里只拼头部。）"""
    """上报批次的公共头部。

    三处（三类场景）共用一份，避免 `vmId` 形态或 `agentVersion` 改动时漏改一处 ——
    这个项目已经因为"同一概念两处定义"出过两次缺陷。
    """
    # 自检：资源 id 里出现字面量 "None" 说明某处 suffix 没传下来。
    # 这种 id 是**合法**的，因此不会被任何校验拦住 —— 只会让多个场景静默共用
    # 同一个资源，进而被事故聚合合并（实际发生过）。
    for resource in resources:
        if "None" in str(resource.get("sourceId", "")):
            raise AssertionError(
                f"场景 {name} 的资源 sourceId 含字面量 'None'：{resource['sourceId']!r}；"
                "某个 suffix 参数没传下来"
            )

    return {
        "agentId": DEMO_AGENT_ID,
        "vmId": DEMO_VM_ID,
        "agentVersion": "demo-1.0.0",
        "batchId": batch_id or f"demo-{name}",
        "sentAt": moment.isoformat(),
        "resources": resources,
        "events": events,
    }


def _event(
    *,
    kind: str,
    source_id: str,
    event_type: EventType,
    at: datetime,
    severity: str | None = None,
    metrics: dict[str, Any] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "occurredAt": at.isoformat(),
        "resourceRef": {"kind": kind, "sourceId": source_id},
        "type": event_type.value,
        "metrics": metrics or {},
    }
    if severity is not None:
        payload["severity"] = severity
    if message is not None:
        payload["message"] = message
    return payload


def build_batch(
    name: str,
    *,
    at: datetime | None = None,
    batch_id: str | None = None,
    suffix: str | None = None,
) -> dict[str, Any]:
    """构造指定场景的上报批次。

    `batchId` 默认按场景名固定 —— 演示脚本重复运行时走幂等回放（不会重复写事件），
    这也顺带演示了 A 的 Q2 幂等语义。需要真正重放请显式传不同的 `batch_id`。

    `suffix` 控制工作负载层资源的命名（见 `_chain_resources`）。默认每个场景
    一套，互不干扰；显式传 `suffix=""` 可让多个场景共用同一批资源。
    """
    if name not in _SCENARIOS:
        raise KeyError(f"未知场景 {name!r}；可用：{list(SCENARIO_NAMES)}")

    profile, _ = _SCENARIOS[name]
    moment = at or _SCENARIO_NOW
    # 必须在分支之前算好：放在 GPU 分支之后会让那个分支读到参数 `suffix`
    # （默认 None），于是资源 id 变成 `demo-ctr-None` —— 一个**合法**的 id，
    # 因此不会被任何校验拦住，只会让多个场景静默共用同一个资源。
    workload_suffix = scenario_suffix(name, suffix)

    if profile is not None:
        # GPU 驱动的场景：事件来自模拟渠道，资源按同一个 suffix 命名
        resources = _chain_resources(
            suffix=workload_suffix, gpu_attributes=_gpu_attributes(profile)
        )
        events = _gpu_events(profile, at=moment)
        return _batch(
            name, moment=moment, batch_id=batch_id, resources=resources, events=events
        )

    if name == "container_oom":
        resources = _chain_resources(
            suffix=workload_suffix,
            container_attributes={
                "image": "vllm/vllm-openai",
                # 限额偏低是这个场景的**注入方式**（DIAGNOSIS_DESIGN §5 场景二）
                "memLimitBytes": 2 * 1024**3,
                "restartCount": 3,
                "oomKilledCount": 1,
            }
        )
        events = [
            _event(
                kind=ResourceKind.CONTAINER.value,
                source_id=f"demo-ctr-{workload_suffix}",
                event_type=EventType.CONTAINER_OOM_KILLED,
                at=moment,
                metrics={"exitCode": 137, "oomKilledCount": 1},
                message="容器被内核 OOM Killer 终止（exit 137）",
            ),
            _event(
                kind=ResourceKind.CONTAINER.value,
                source_id=f"demo-ctr-{workload_suffix}",
                event_type=EventType.CONTAINER_RESTART,
                at=moment + timedelta(seconds=20),
                metrics={"restartCount": 3},
                message="容器重启计数 3",
            ),
            _event(
                kind=ResourceKind.AI_SERVICE.value,
                source_id=f"demo-svc-{workload_suffix}",
                event_type=EventType.INFERENCE_ERROR,
                at=moment + timedelta(seconds=30),
                metrics={"count": 7},
                message="推理请求失败 x7（容器不可用）",
            ),
        ]
        return _batch(name, moment=moment, batch_id=batch_id, resources=resources, events=events)

    if name == "network_failure":
        resources = _chain_resources(suffix=workload_suffix)
        events = [
            _event(
                kind=ResourceKind.CONTAINER.value,
                source_id=f"demo-ctr-{workload_suffix}",
                event_type=EventType.CONTAINER_NETWORK_UNREACHABLE,
                at=moment,
                message="连接目标服务 10.0.0.9:8000 超时",
            ),
            _event(
                kind=ResourceKind.AGENT.value,
                source_id=f"demo-agent-{workload_suffix}",
                event_type=EventType.AGENT_NETWORK_TIMEOUT,
                at=moment + timedelta(seconds=10),
                metrics={"count": 4},
                message="智能体网络超时 x4",
            ),
            _event(
                kind=ResourceKind.AGENT.value,
                source_id=f"demo-agent-{workload_suffix}",
                event_type=EventType.AGENT_TASK_FAILED,
                at=moment + timedelta(seconds=20),
                metrics={"count": 5, "retryCount": 5},
                message="智能体任务失败，已重试 5 次",
            ),
        ]
        return _batch(name, moment=moment, batch_id=batch_id, resources=resources, events=events)

    raise AssertionError(f"场景 {name!r} 没有实现")


__all__ = [
    "DEMO_AGENT_ID",
    "DEMO_VM_ID",
    "DEFAULT_SUFFIX_BY_SCENARIO",
    "SCENARIO_NAMES",
    "build_batch",
    "describe_scenarios",
    "scenario_suffix",
]
