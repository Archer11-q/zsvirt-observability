"""测试数据工厂。

存在的理由：`client` 夹具与"建一条 HOST→…→AI_SERVICE 链路"的代码已经在
`test_topology_api.py` 与 `test_events_alerts_api.py` 里各写了一份，再加诊断与
工作负载两个文件就是第四、第五份。链路一旦调整（例如新增 `task` 层），
五份副本里漏改一份就会得到"某个端点看不到新资源"这类难查的假失败。

因此本模块是**新测试的唯一入口**；既有文件保持原样（它们已验证通过，
迁移它们属于无收益的改动风险），后续如需重构可逐步收敛到这里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.enums import AlertState, EventSource, ResourceKind, ResourceStatus, Severity
from app.graph.repository import ensure_edge, upsert_resource
from app.models import Alert, Event

#: 所有测试的固定"现在"。用固定时刻而不是 `datetime.now()`，
#: 时间窗断言才不会在跨越整点/日界时随机失败。
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)

#: 标准链路：HOST → GPU → VGPU → VM → CONTAINER → AI_SERVICE
HOST = "host:zsvirt:h0"
GPU = "gpu:zsvirt:g0"
VGPU = "vgpu:zsvirt:v0"
VM = "vm:zsvirt:vm0"
CTR = "container:probe:probe-x:web-0"
AIS = "ai_service:probe:probe-x:vllm"

#: 第二个 AI 服务，用于验证聚合不会把两个服务的数据混在一起
AIS2 = "ai_service:probe:probe-x:embed"

CHAIN = ((HOST, GPU), (GPU, VGPU), (VGPU, VM), (VM, CTR), (CTR, AIS))


def build_chain(
    session: Session,
    *,
    seen_at: datetime = NOW,
    gpu_attributes: dict[str, Any] | None = None,
    with_service: bool = True,
) -> None:
    """建立标准链路（可选带 GPU 属性）。

    `gpu_attributes=None` 表示"这台机器的 GPU 指标没采到" —— 这是演示环境里
    最常见的情况（无 zwatch），工作负载端点必须能优雅地报告缺失而不是报 0。
    """
    upsert_resource(
        session,
        resource_id=HOST,
        kind=ResourceKind.HOST.value,
        name="node-01",
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
    )
    upsert_resource(
        session,
        resource_id=GPU,
        kind=ResourceKind.GPU.value,
        parent_id=HOST,
        name="A10-0",
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
        attributes=gpu_attributes or {},
    )
    upsert_resource(
        session,
        resource_id=VGPU,
        kind=ResourceKind.VGPU.value,
        parent_id=GPU,
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
    )
    upsert_resource(
        session,
        resource_id=VM,
        kind=ResourceKind.VM.value,
        parent_id=VGPU,
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
    )
    upsert_resource(
        session,
        resource_id=CTR,
        kind=ResourceKind.CONTAINER.value,
        parent_id=VM,
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
        attributes={"image": "vllm/vllm-openai"},
    )
    if with_service:
        upsert_resource(
            session,
            resource_id=AIS,
            kind=ResourceKind.AI_SERVICE.value,
            parent_id=CTR,
            name="vllm-qwen",
            status=ResourceStatus.RUNNING.value,
            seen_at=seen_at,
            attributes={
                "framework": "vllm",
                "modelName": "Qwen2.5-7B-Instruct",
                "endpoint": "http://127.0.0.1:8000/v1",
                "concurrency": 8,
                "qps": 3.5,
            },
        )
    for parent, child in CHAIN:
        ensure_edge(session, parent, child)
    session.flush()


def add_event(
    session: Session,
    event_id: str,
    *,
    resource_id: str = CTR,
    type: str = "gpu.memory.exhausted",
    severity: str = Severity.ERROR.value,
    occurred_at: datetime | None = None,
    value: Any = None,
    count: int | None = None,
    source: str = EventSource.PROBE.value,
    message: str | None = None,
) -> str:
    """插入一条事件。`value` / `count` 写进 `metrics`（诊断证据从这里取）。"""
    metrics: dict[str, Any] = {}
    if value is not None:
        metrics["value"] = value
    if count is not None:
        metrics["count"] = count

    moment = occurred_at or NOW
    session.add(
        Event(
            id=event_id,
            occurred_at=moment,
            received_at=moment,
            source=source,
            resource_id=resource_id,
            type=type,
            severity=severity,
            message=message or f"{type} on {resource_id}",
            metrics=metrics,
            raw={},
        )
    )
    session.flush()
    return event_id


def add_alert(
    session: Session,
    alert_id: str,
    *,
    resource_id: str = CTR,
    evidence: list[str],
    state: str = AlertState.FIRING.value,
    severity: str = Severity.CRITICAL.value,
    rule_id: str = "R-GPU-MEM-001",
    fired_at: datetime | None = None,
    count: int = 1,
) -> str:
    """插入一条告警。`evidence` 必填 —— 数据库 CHECK 也会拒绝空数组。"""
    moment = fired_at or NOW
    session.add(
        Alert(
            id=alert_id,
            rule_id=rule_id,
            resource_id=resource_id,
            severity=severity,
            state=state,
            first_fired_at=moment,
            last_fired_at=moment,
            count=count,
            evidence_event_ids=evidence,
            aggregation_key=f"{rule_id}:{resource_id}",
            title=f"{rule_id} on {resource_id}",
        )
    )
    session.flush()
    return alert_id


def window_from(
    moment: datetime = NOW, *, before_minutes: int = 30, after_minutes: int = 30
) -> dict[str, str]:
    """构造一个覆盖 `moment` 附近的诊断时间窗（ISO8601，UTC）。"""
    return {
        "from": (moment - timedelta(minutes=before_minutes)).isoformat(),
        "to": (moment + timedelta(minutes=after_minutes)).isoformat(),
    }
