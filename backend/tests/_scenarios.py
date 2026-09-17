"""三类赛题故障场景的事件夹具（告警引擎验收用）。

**为什么放在测试里而不是 `deploy/`**：`deploy/` 里的脚本是给演示用的，
面向"在真机上把故障注进去"；这里的夹具是给**测试**用的，面向"给定这样一串
事件，告警引擎必须产生这样的告警"。两者的生命周期和变更频率完全不同 ——
混在一起会让"改测试夹具"看起来像"改演示脚本"。

夹具按 `DIAGNOSIS_DESIGN.md` §5 的三类场景组织，事件类型只用**已冻结**的
`EventType` 取值（写错会被 `assert_rules_are_consistent` 抓到，但测试夹具本身
也应该用枚举而不是字符串字面量，否则改枚举名时静默失效）。

上报载荷走 `POST /api/v1/ingest/batch` 的真实契约（A 的 Q1–Q7），
因此这些测试同时也在验证"探针按契约上报 → 告警自动产生"这条端到端链路。
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

from app.enums import EventType, ResourceKind

#: 夹具里的固定"现在"。用固定时刻让时间窗断言可复现。
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)

VM_ID = "vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"
AGENT_ID = "probe-x"

#: 每个批次一个唯一 batchId —— 否则第二次上报会被幂等逻辑当成重放，
#: 表现是"第二次上报没有产生任何告警"，看起来像引擎坏了。
_BATCH_COUNTER = itertools.count(1)


def next_batch_id() -> str:
    return f"batch-{next(_BATCH_COUNTER):06d}"


def resources(
    *,
    gpu_mem_used: int | None = None,
    gpu_mem_total: int | None = None,
    container_mem_limit: int | None = None,
) -> list[dict[str, Any]]:
    """标准链路资源：vGPU → VM → 容器 → AI 服务，加上 GPU 与 Agent。"""
    gpu_attrs: dict[str, Any] = {}
    if gpu_mem_used is not None:
        gpu_attrs["memUsedBytes"] = gpu_mem_used
    if gpu_mem_total is not None:
        gpu_attrs["memTotalBytes"] = gpu_mem_total

    container_attrs: dict[str, Any] = {"image": "vllm/vllm-openai"}
    if container_mem_limit is not None:
        container_attrs["memLimitBytes"] = container_mem_limit

    return [
        {
            # 宿主机必须上报：`vgpu0` 声明了 `parentSourceId: "gpu0"`，
            # 而 `gpu0` 又挂在宿主机下。缺了链条上的任何一环都会让
            # `resource.parent_id` 指向一个尚未创建的 id → 外键违约 → 整批 503。
            # （这个错误我第一版就犯了，症状是"所有场景测试全挂但原因是同一个"。）
            "kind": ResourceKind.HOST.value,
            "sourceId": "host0",
            "name": "node-01",
            "status": "running",
        },
        {
            "kind": ResourceKind.GPU.value,
            "sourceId": "gpu0",
            "name": "A10-0",
            "parentSourceId": "host0",
            "status": "running",
            "attributes": gpu_attrs,
        },
        {
            "kind": ResourceKind.VGPU.value,
            "sourceId": "vgpu0",
            "name": "A10-0-1g",
            "parentSourceId": "gpu0",
            "status": "running",
        },
        {
            "kind": ResourceKind.VM.value,
            "sourceId": "self",
            "name": "vm0",
            "parentSourceId": "vgpu0",
            "status": "running",
        },
        {
            "kind": ResourceKind.CONTAINER.value,
            "sourceId": "web-0",
            "name": "vllm-web-0",
            "parentSourceId": "self",
            "status": "running",
            "attributes": container_attrs,
        },
        {
            "kind": ResourceKind.AI_SERVICE.value,
            "sourceId": "vllm",
            "name": "vllm-qwen",
            "parentSourceId": "web-0",
            "status": "running",
            "attributes": {"framework": "vllm", "modelName": "Qwen2.5-7B-Instruct"},
        },
        {
            "kind": ResourceKind.AGENT.value,
            "sourceId": "agent-1",
            "name": "agent-1",
            # 智能体挂在 **AI 服务**下（层级 `PARENT_CHILD_ALLOWED` 就是这么定的）。
            # 第一版误写成挂在容器下，于是 `agent.sourceId` 被解析成
            # `agent:probe:probe-x:agent-1`、父资源被解析成
            # `ai_service:probe:probe-x:web-0` —— 两个 id 都不是我预想的那个，
            # 因为父资源按**父 kind 推断**、而我的 sourceId 与 kind 是错配的。
            "parentSourceId": "vllm",
            "status": "running",
        },
    ]


def _event(
    *,
    kind: str,
    source_id: str,
    event_type: str,
    at: datetime,
    severity: str | None = None,
    metrics: dict[str, Any] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "occurredAt": at.isoformat(),
        "resourceRef": {"kind": kind, "sourceId": source_id},
        "type": event_type,
        "metrics": metrics or {},
    }
    if severity is not None:
        payload["severity"] = severity
    if message is not None:
        payload["message"] = message
    return payload


def batch(
    events: list[dict[str, Any]],
    *,
    resources_: list[dict[str, Any]] | None = None,
    sent_at: datetime | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """组装一份上报批次。"""
    moment = sent_at or NOW
    return {
        "agentId": AGENT_ID,
        "vmId": VM_ID,
        "agentVersion": "0.1.0",
        "batchId": batch_id or next_batch_id(),
        "sentAt": moment.isoformat(),
        "resources": resources_ if resources_ is not None else resources(),
        "events": events,
    }


# ================================================================ 场景一


def scenario_gpu_memory_exhausted(
    *, at: datetime | None = None, batch_id: str | None = None
) -> dict[str, Any]:
    """场景一：GPU / 资源瓶颈 —— vGPU 显存耗尽 + 推理超时。

    根因层在 GPU/vGPU，症状层在 AI 服务。告警应覆盖**两层各一条**：
    `R-GPU-MEM-001`（vGPU 上的显存耗尽）与 `R-AIS-TIMEOUT-020`。
    """
    moment = at or NOW
    return batch(
        [
            _event(
                kind=ResourceKind.VGPU.value,
                source_id="vgpu0",
                event_type=EventType.GPU_MEMORY_EXHAUSTED.value,
                at=moment,
                metrics={"value": "98", "gpu_memory_usage": 98},
                message="vGPU 显存使用率 98%",
            ),
            _event(
                kind=ResourceKind.AI_SERVICE.value,
                source_id="vllm",
                event_type=EventType.INFERENCE_TIMEOUT.value,
                at=moment + timedelta(seconds=5),
                metrics={"count": 12},
                message="推理请求超时 x12",
            ),
        ],
        resources_=resources(gpu_mem_used=22 * 1024**3, gpu_mem_total=24 * 1024**3),
        sent_at=moment,
        batch_id=batch_id,
    )


# ================================================================ 场景二


def scenario_container_oom(
    *, at: datetime | None = None, batch_id: str | None = None
) -> dict[str, Any]:
    """场景二：容器 / 进程异常 —— 容器被 OOM Killer 终止 + 反复重启。

    两条不同规则（`R-CTR-OOM-010` / `R-CTR-RESTART-011`），因此应产生**两条**
    告警 —— 这同时验证了"聚合按规则分别进行"，不会把不同规则的证据混进一条。
    """
    moment = at or NOW
    return batch(
        [
            _event(
                kind=ResourceKind.CONTAINER.value,
                source_id="web-0",
                event_type=EventType.CONTAINER_OOM_KILLED.value,
                at=moment,
                metrics={"exitCode": 137, "oomKilledCount": 1},
                message="容器被内核 OOM Killer 终止（exit 137）",
            ),
            _event(
                kind=ResourceKind.CONTAINER.value,
                source_id="web-0",
                event_type=EventType.CONTAINER_RESTART.value,
                at=moment + timedelta(seconds=3),
                metrics={"restartCount": 3},
                message="容器重启计数 3",
            ),
        ],
        resources_=resources(container_mem_limit=2 * 1024**3),
        sent_at=moment,
        batch_id=batch_id,
    )


# ================================================================ 场景三


def scenario_network_failure(
    *, at: datetime | None = None, batch_id: str | None = None
) -> dict[str, Any]:
    """场景三：应用 / Agent 异常 —— 容器网络不可达 + 智能体任务失败。"""
    moment = at or NOW
    return batch(
        [
            _event(
                kind=ResourceKind.CONTAINER.value,
                source_id="web-0",
                event_type=EventType.CONTAINER_NETWORK_UNREACHABLE.value,
                at=moment,
                message="连接目标服务 10.0.0.9:8000 超时",
            ),
            _event(
                kind=ResourceKind.AGENT.value,
                source_id="agent-1",
                event_type=EventType.AGENT_TASK_FAILED.value,
                at=moment + timedelta(seconds=2),
                metrics={"retryCount": 5},
                message="智能体任务失败，已重试 5 次",
            ),
        ],
        sent_at=moment,
        batch_id=batch_id,
    )


# ================================================================ 反例


def scenario_benign(*, at: datetime | None = None, batch_id: str | None = None) -> dict[str, Any]:
    """无异常事件（正常心跳）—— **不得产生任何告警**。

    这条负向夹具比正向的更重要：一个"什么都报警"的引擎在演示时看起来
    "很能干"，实际上等于没有告警。
    """
    moment = at or NOW
    return batch(
        [
            _event(
                kind=ResourceKind.AI_SERVICE.value,
                source_id="vllm",
                event_type=EventType.INGEST_RESOURCE_UNRESOLVED.value,
                at=moment,
                message="占位资源已解析",
            )
        ],
        sent_at=moment,
        batch_id=batch_id,
    )


def scenario_wrong_layer(
    *, at: datetime | None = None, batch_id: str | None = None
) -> dict[str, Any]:
    """容器事件落在 **VM** 资源上 —— 资源类型不匹配，不得产生容器告警。

    验证规则里的 `resource_kinds` 真的生效。若它被忽略，这条会错误地触发
    `R-CTR-OOM-010`，而前端会把"容器 OOM"显示在一个虚拟机上。
    """
    moment = at or NOW
    return batch(
        [
            _event(
                kind=ResourceKind.VM.value,
                source_id="self",
                event_type=EventType.CONTAINER_OOM_KILLED.value,
                at=moment,
                message="（错误分层）容器 OOM 事件挂在 VM 上",
            )
        ],
        sent_at=moment,
        batch_id=batch_id,
    )


def single_event_batch(
    *,
    event_type: str,
    severity: str | None = None,
    kind: str = ResourceKind.CONTAINER.value,
    source_id: str = "web-0",
    metrics: dict[str, Any] | None = None,
    at: datetime | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """单事件批次。用于验证"某一条规则的边界行为"，避免每次都搬整套场景。"""
    moment = at or NOW
    return batch(
        [
            _event(
                kind=kind,
                source_id=source_id,
                event_type=event_type,
                at=moment,
                severity=severity,
                metrics=metrics,
            )
        ],
        sent_at=moment,
        batch_id=batch_id,
    )


__all__ = [
    "AGENT_ID",
    "NOW",
    "VM_ID",
    "batch",
    "next_batch_id",
    "resources",
    "scenario_benign",
    "scenario_container_oom",
    "scenario_gpu_memory_exhausted",
    "scenario_network_failure",
    "scenario_wrong_layer",
    "single_event_batch",
]
