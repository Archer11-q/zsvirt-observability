"""接入服务：把一批上报落库。

契约真源：`docs/API_CONTRACT.md` §3。本模块落实成员 A 已确认的 7 项语义：

| 语义 | 位置 | A 的诉求 |
|---|---|---|
| 至少一次投递 → `batchId` 幂等 | `_existing_result` | Q2：重复批次返回**与首次一致**的结果（200，不是脏数据） |
| 逐条返回 resource 接受结果 | `ResourceResult` | Q1：A 据此维护「已确认资源」缓存 |
| 孤儿引用挂占位节点 | `_resolve_resource_id` | Q1 兜底：不丢数据、不报错 |
| 5s/1MB 批量上限 | `validate_batch_size` | Q4 |
| 时钟漂移 `receivedAt − sentAt` | `metrics.observe_clock_drift` | Q3：暴露为健康指标 |
| 迟到批次按 `occurredAt` 追加 | 不检查时间先后 | Q6：**不因迟到丢弃或告警** |
| 可选 Bearer Token | API 层 | Q7 |

另有两条来自其他决策：

- **脱敏**：`raw` / `attributes` / `message` 入库前过脱敏管道（`docs/SENSITIVE_DATA.md`）
- **事件类型比对**：非枚举类型不拒收但计数（见 `schemas.py` 的说明）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ulid import ULID

from app.alerts.engine import evaluate_events
from app.graph.repository import placeholder_resource_id, upsert_resource
from app.ingest.limits import BatchRateLimiter, IngestMetrics, metrics, rate_limiter
from app.ingest.schemas import (
    DEFAULT_MAX_EVENTS,
    DEFAULT_MAX_PAYLOAD_BYTES,
    DEFAULT_MAX_RESOURCES,
    AcceptedCounts,
    IngestBatch,
    IngestResult,
    RejectedItem,
    ResourceResult,
)
from app.models import Event, Resource
from app.models import IngestBatch as IngestBatchRow
from app.normalize.ids import InvalidResourceId, probe_resource_id
from app.normalize.ids import ResourceRef as ProbeRef
from app.normalize.sensitive import filter_sensitive, mask_cmdline, mask_secrets_in_text


class BatchTooLarge(Exception):
    """超出批量上限 —— 应返回 400 INVALID_ARGUMENT。"""


def validate_batch_size(
    batch: IngestBatch,
    *,
    max_events: int = DEFAULT_MAX_EVENTS,
    max_resources: int = DEFAULT_MAX_RESOURCES,
) -> None:
    """校验批量上限（成员 A 的 Q4 已确认值）。

    超限**不静默截断** —— 静默截断会让探针以为数据已送达。
    返回 400 并给出实际数量与上限，让 A 能明确地分批。
    """
    if len(batch.events) > max_events:
        raise BatchTooLarge(f"events 数量 {len(batch.events)} 超过上限 {max_events}")
    if len(batch.resources) > max_resources:
        raise BatchTooLarge(f"resources 数量 {len(batch.resources)} 超过上限 {max_resources}")


def validate_payload_size(payload_bytes: int, *, limit: int = DEFAULT_MAX_PAYLOAD_BYTES) -> None:
    """校验单批载荷大小（1MB）。"""
    if payload_bytes > limit:
        raise BatchTooLarge(f"载荷 {payload_bytes} 字节超过上限 {limit} 字节")


def _existing_result(session: Session, batch_id: str) -> dict[str, Any] | None:
    """查询该批次是否已处理过（幂等去重，成员 A 的 Q2）。

    返回首次的响应摘要，调用方直接回放 —— 这样重复批次得到
    **与首次完全一致**的结果，而不是"已忽略"之类的新语义。
    """
    row = session.get(IngestBatchRow, batch_id)
    return row.result if row is not None else None


def _replay_result(cached: dict[str, Any]) -> IngestResult:
    """回放首次结果，并把 `duplicate` 显式置为 `True`。

    **不能只做 `model_validate(cached)`** —— 存下来的摘要里 `duplicate` 是
    首次响应的值 `False`，直接校验回放会把这个 `False` 一起带出来，
    于是重复批次被上报为"非重复"。A 正是靠这个字段判断该不该重新发送，
    所以必须在此处显式覆盖。

    同时把 `duplicate` 写回摘要，保证后续再次重复时语义一致。
    """
    result = IngestResult.model_validate(cached)
    if result.duplicate is not True:
        result = result.model_copy(update={"duplicate": True})
    return result


def _resolve_resource_id(
    session: Session,
    *,
    kind: str,
    source_id: str,
    agent_id: str,
    known: dict[tuple[str, str], str],
) -> tuple[str, bool]:
    """把探针的 `(kind, sourceId)` 解析为全局 ID。

    返回 `(global_id, is_placeholder)`。

    策略：优先用已上报的资源；若不存在，**不报错**，而是返回占位 ID ——
    成员 A 的 Q1 允许极端竞态出现孤儿引用，B 的职责是兜底而不是拒收。
    """
    key = (kind, source_id)
    if key in known:
        return known[key], False

    try:
        global_id = probe_resource_id(agent_id, ProbeRef(kind, source_id))
    except InvalidResourceId:
        # 形态非法（例如 sourceId 含冒号）→ 用占位 ID 保证数据不丢
        return placeholder_resource_id(kind, "probe", f"{agent_id}:{source_id}"), True

    return global_id, True


def ingest_batch(
    session: Session,
    *,
    batch: IngestBatch,
    received_at: datetime | None = None,
    payload_bytes: int = 0,
    limiter: BatchRateLimiter | None = None,
    run_metrics: IngestMetrics | None = None,
) -> tuple[IngestResult, bool]:
    """处理一批上报。

    返回 `(结果, 是否重复批次)`。调用方负责提交事务。

    **幂等**：若 `batchId` 已存在，直接回放首次结果，并把 `duplicate` 置为
    `True`（A 的 Q2：重复批次返回与首次一致的结果，但必须能区分这是重复投递）。
    """
    now = received_at or datetime.now(UTC)
    limiter = limiter or rate_limiter
    run_metrics = run_metrics or metrics

    # ---- 幂等：重复批次回放首次结果（A 的 Q2）----
    cached = _existing_result(session, batch.batchId)
    if cached is not None:
        run_metrics.batches_duplicate += 1
        return _replay_result(cached), True

    validate_batch_size(batch)
    if payload_bytes:
        validate_payload_size(payload_bytes)

    # ---- 时钟漂移（A 的 Q3）----
    run_metrics.observe_clock_drift(now, batch.sentAt)

    rejected: list[RejectedItem] = []
    resource_results: list[ResourceResult] = []
    known_ids: dict[tuple[str, str], str] = {}

    # ---- 先把 vmId 对应的 VM 登记进 known，事件与资源都能挂上 ----
    vm_id = batch.vmId
    known_ids[("vm", "self")] = vm_id

    # ---- 资源：逐条处理，部分接受 ----
    for index, item in enumerate(batch.resources):
        try:
            global_id = probe_resource_id(batch.agentId, ProbeRef(item.kind, item.sourceId))
        except InvalidResourceId as exc:
            rejected.append(
                RejectedItem(index=index, reason="INVALID_RESOURCE_ID", detail=str(exc))
            )
            resource_results.append(
                ResourceResult(
                    index=index, kind=item.kind, sourceId=item.sourceId, result="rejected"
                )
            )
            continue

        # 父资源可能未上报 → 先解析（可能得到占位）
        parent_global: str | None = None
        if item.parentSourceId is not None:
            # 父资源的 kind 未给出，按层级推断（探针只需给 sourceId）
            parent_kind = _infer_parent_kind(item.kind)
            parent_global, _ = _resolve_resource_id(
                session,
                kind=parent_kind,
                source_id=item.parentSourceId,
                agent_id=batch.agentId,
                known=known_ids,
            )

        # 脱敏：attributes 里的 cmdline 单独掩码（保留诊断价值）
        attrs = _redact_attributes(item.attributes)

        upsert_resource(
            session,
            resource_id=global_id,
            kind=item.kind,
            name=item.name,
            parent_id=parent_global,
            status=item.status,
            attributes=attrs,
            labels=item.labels,
            seen_at=item.lastSeenAt or now,
        )

        known_ids[(item.kind, item.sourceId)] = global_id
        resource_results.append(
            ResourceResult(
                index=index,
                kind=item.kind,
                sourceId=item.sourceId,
                globalId=global_id,
                result="accepted",
            )
        )

    # ---- 事件 ----
    accepted_events = 0
    unknown_types = 0
    #: 本批**真正写入**的事件对象。告警引擎需要对它们求值，因此必须留存引用，
    #: 不能只累加计数。
    written_events: list[Event] = []
    for event in batch.events:
        ref = event.resourceRef
        global_resource_id, is_placeholder = _resolve_resource_id(
            session,
            kind=ref.kind,
            source_id=ref.sourceId,
            agent_id=batch.agentId,
            known=known_ids,
        )
        if is_placeholder:
            # 孤儿引用：登记占位节点，让事件能挂上（A 的 Q1 兜底）。
            # 不丢数据、不报错 —— 计数并暴露为健康指标。
            if session.get(Resource, global_resource_id) is None:
                upsert_resource(
                    session,
                    resource_id=global_resource_id,
                    kind="unresolved",
                    name=ref.sourceId,
                    seen_at=now,
                    is_placeholder=True,
                )
            run_metrics.unresolved_events += 1

        if not event.is_known_type():
            unknown_types += 1
            run_metrics.unknown_event_types += 1

        # 脱敏：raw 递归清理，message 掩码连接串与密钥形态
        raw = _redact_raw(event.raw)

        written = Event(
            id=f"evt_{ULID()}",
            occurred_at=event.occurredAt,
            received_at=now,
            source="probe",
            resource_id=global_resource_id,
            type=event.type,
            severity=event.resolved_severity(),
            message=mask_secrets_in_text(event.message) if event.message else None,
            metrics=event.metrics,
            raw=raw,
            trace_id=event.traceId,
            batch_id=batch.batchId,
        )
        session.add(written)
        written_events.append(written)
        accepted_events += 1

    # ---- 告警引擎：对**本批新写入的事件**求值 ----
    #
    # 位置是刻意的：在事件写入之后、批次行落库之前。
    #   - 之后：规则需要事件已带 id（`evidenceEventIds` 必填，红线）
    #   - 之前：摘要因此成为幂等缓存的一部分，重复批次会回放同样的告警计数
    #
    # 传入的是本批**新写入**的事件，不是"窗口内的事件" —— 重复批次在函数开头
    # 就已回放返回，不会走到这里，因此 `count` 不会被重复投递灌水。
    #
    # 与事件同事务：告警和支撑它的事件要么一起可见，要么一起不可见。
    alert_evaluation = evaluate_events(session, written_events, now=now)

    result = IngestResult(
        batchId=batch.batchId,
        duplicate=False,
        accepted=AcceptedCounts(
            resources=len([r for r in resource_results if r.result == "accepted"]),
            events=accepted_events,
        ),
        resources=resource_results,
        rejected=rejected,
        acceptedUnknownTypes=unknown_types,
        alerts=alert_evaluation.as_dict(),
    )

    # ---- 记录批次（供幂等回放）----
    session.add(
        IngestBatchRow(
            batch_id=batch.batchId,
            agent_id=batch.agentId,
            vm_id=vm_id,
            agent_version=batch.agentVersion,
            sent_at=batch.sentAt,
            received_at=now,
            accepted_resources=result.accepted.resources,
            accepted_events=accepted_events,
            rejected_count=len(rejected),
            result=result.model_dump(mode="json"),
        )
    )

    try:
        session.flush()
    except IntegrityError:
        # 并发下同一 batchId 被两个请求同时处理：另一个已写入。
        # 回滚本次并回放已存在的结果 —— 仍然满足"重复批次返回与首次一致"。
        session.rollback()
        cached_after_race = _existing_result(session, batch.batchId)
        if cached_after_race is not None:
            run_metrics.batches_duplicate += 1
            return _replay_result(cached_after_race), True
        raise

    run_metrics.batches_accepted += 1
    run_metrics.rejected_items += len(rejected)
    run_metrics.last_batch_at = now
    return result, False


def _infer_parent_kind(kind: str) -> str:
    """由子类型推断父类型（探针只需给 `parentSourceId`）。

    这是刻意的便利化：让探针只给 `sourceId` 而不必重复给出父类型，
    同时保持层级可预测。层级定义见 `app.graph.algorithms.PARENT_CHILD_ALLOWED`。
    """
    parents = {
        "gpu": "host",
        "vgpu": "gpu",
        "vm": "vgpu",
        "container": "vm",
        "process": "vm",
        "ai_service": "container",
        "agent": "ai_service",
        "task": "agent",
    }
    return parents.get(kind, "vm")


def _redact_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """对资源属性做脱敏。

    `cmdline` 特殊处理：**掩码参数值而不是整个删掉** —— 保留命令结构
    对诊断有价值（能看出跑的是什么服务），只是不暴露凭据。
    """
    cleaned = filter_sensitive(attributes)
    if isinstance(cleaned, dict) and "cmdline" in cleaned:
        cmd = cleaned["cmdline"]
        if isinstance(cmd, list):
            cleaned["cmdline"] = mask_cmdline(cmd)
        elif isinstance(cmd, str):
            cleaned["cmdline"] = " ".join(mask_cmdline(cmd.split()))
    return cleaned


def _redact_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """对事件原始载荷做脱敏（递归）。"""
    return filter_sensitive(raw)


__all__ = [
    "BatchTooLarge",
    "ingest_batch",
    "validate_batch_size",
    "validate_payload_size",
]
