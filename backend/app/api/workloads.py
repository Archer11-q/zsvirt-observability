"""`GET /api/v1/workloads` —— AI 服务工作负载总览。

契约：`docs/API_CONTRACT.md` §4.3；语义由成员 C 的 Q10 定为
**`ai_service` 层级的业务聚合视图** —— 不是容器，也不是 Agent/Task。
"负载总览"回答的是"哪个 AI 服务在跑、健不健康"。

**这个端点最重要的设计约束是"不编造"**：

1. `resourceUsage` 的 GPU 指标**可以为 `null`**。当 ZWatch 不可用、探针未上报
   GPU 属性时，B 返回 `null` + `note` 说明原因，**绝不用 0 顶替** ——
   0 会被前端渲染成"显存占用 0%"，那是错误信息而不是缺失信息。
2. `gpuProviderMode` 必须出现在响应里，来源与 `/api/health` 的
   `gpuProvider.mode` **同一处定义**（`Settings.gpu_provider_mode`），
   前端据此区分真实指标与模拟数据（`DATA_MODEL.md` §4.2.1 诚实性要求）。

**计数口径**：`eventCount` / `alertCount` 统计的是**该服务所在链路**上的事件与
告警 —— 不只是 `ai_service` 节点自身的。跨层关联的意义就在于"GPU 上的问题算到
这个服务头上"：只数本节点会得到一片 0，工作负载视图也就没用了。

**方向性是本模块最容易犯的错**：工作负载位于链路的**末端**，链路成员都在它的
**祖先**方向。因此归属解析必须沿父链向上走（`_resolve_owners`）；第一版沿子链
向下走，结果所有计数恒为 0 且不报任何错。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.common import build_meta, invalid_argument
from app.api.schemas.diagnosis import (
    WorkloadData,
    WorkloadListData,
    WorkloadResourceUsage,
)
from app.config import get_settings
from app.db import get_db
from app.diagnosis import service as diagnosis_service
from app.enums import AlertState, Observability, ResourceKind
from app.graph.repository import format_staleness
from app.models import Alert, Event, Resource

router = APIRouter(tags=["workloads"])

DbSession = Annotated[Session, Depends(get_db)]

DEFAULT_LIMIT = 200
MAX_LIMIT = 500

#: 默认统计窗口：近 24 小时。足以覆盖一次故障演示，又不至于把全量历史拉进来。
DEFAULT_SINCE = timedelta(hours=24)

#: 父链回溯深度上限。链路最长 7 层，这里留余量；同时防住脏数据造成的环。
MAX_CHAIN_DEPTH = 12

#: `resourceUsage` 里 GPU 指标缺失时的说明。
#: **必须给出可执行的下一步**（"来源需 …"），而不是只说"没有数据"。
GPU_METRIC_SOURCE_HINT = (
    "来源需 ZWatch 指标 API 或虚拟机内探针的 nvidia-smi（见 DATA_MODEL.md §4.2.1）"
)


@router.get("/api/v1/workloads")
def list_workloads(
    session: DbSession,
    since: datetime | None = Query(default=None, description="统计窗口起点（默认近 24 小时）"),
    to: datetime | None = Query(default=None, description="统计窗口终点（默认现在）"),
    kind: str | None = Query(
        default=None, description="聚合层级，默认 ai_service（C 的 Q10）"
    ),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> Any:
    """工作负载列表。"""
    now = datetime.now(UTC)
    window_to = to or now
    window_from = since or (window_to - DEFAULT_SINCE)

    if window_from >= window_to:
        return invalid_argument("统计窗口非法：since 必须早于 to")

    # 契约把语义定为 ai_service；允许传其他 kind 只是为了让联调时能借用同一套
    # 聚合逻辑查看 vm / container 层级。`unresolved` 是占位类型，不是真实层级。
    target_kind = kind or ResourceKind.AI_SERVICE.value
    valid_kinds = {k.value for k in ResourceKind} - {ResourceKind.UNRESOLVED.value}
    if target_kind not in valid_kinds:
        return invalid_argument(f"kind 非法：{target_kind!r}。允许值：{sorted(valid_kinds)}")

    workloads = list(
        session.execute(
            select(Resource)
            .where(Resource.kind == target_kind)
            .order_by(Resource.name, Resource.id)
            .limit(limit)
        ).scalars()
    )
    workload_ids = [w.id for w in workloads]

    # 一次算好"哪些资源属于哪个工作负载"，计数与 GPU 归集共用同一份结果 ——
    # 若两处各算一遍，"链路成员"就有了两个定义点，迟早漂移。
    owners = _resolve_owners(workload_ids, _load_parent_map(session))

    event_counts, alert_counts, firing_counts = _count_rollups(
        session, owners, window_from, window_to
    )
    diagnoses = diagnosis_service.latest_diagnoses_per_owner(session, owners)
    gpu_usage = _gpu_usage_for(session, owners, workload_ids)

    items = [
        _to_workload(
            w,
            now=now,
            event_count=event_counts.get(w.id, 0),
            alert_count=alert_counts.get(w.id, 0),
            firing_count=firing_counts.get(w.id, 0),
            diagnosis=diagnoses.get(w.id),
            usage=gpu_usage.get(w.id),
        )
        for w in workloads
    ]

    data = WorkloadListData(
        items=items,
        windowFrom=window_from.astimezone(UTC).isoformat(),
        windowTo=window_to.astimezone(UTC).isoformat(),
        gpuProviderMode=get_settings().gpu_provider_mode,
    )

    return {
        "data": data.model_dump(mode="json"),
        "meta": build_meta().model_dump(mode="json"),
    }


def _load_parent_map(session: Session) -> dict[str, str]:
    """`资源 ID → 父资源 ID` 的映射（全表一次取回）。

    资源表规模是"集群里有多少个对象"量级（演示环境几十到几百），一次取回远比
    每条事件发一次递归查询划算。**读的是 `resource.parent_id` 而不是
    `resource_edge`**：两者由 `graph.repository` 在同一事务内维护，单点查父正是
    `parent_id` 的设计用途（见 `models.ResourceEdge` 注释）。
    """
    rows = session.execute(select(Resource.id, Resource.parent_id)).all()
    return {rid: pid for rid, pid in rows if pid}


def _resolve_owners(
    workload_ids: list[str], parents: dict[str, str]
) -> dict[str, list[str]]:
    """反向映射：`资源 ID → 拥有它的工作负载 ID 列表`（**一对多**）。

    这就是"跨层关联"在这个端点上的落地：GPU / vGPU / VM / CONTAINER 上的
    事件与告警，最终都归到与之相关的 AI 服务名下。

    两条必须遵守的规则：

    1. **沿父链向上走，不能沿子链向下走。** 工作负载是链路的**末端**节点
       （AI 服务挂在容器下），链路成员全在**祖先**方向；从工作负载向下遍历
       子节点只会匹配到它自己，于是计数恒为 0、GPU 永远解析不到，而且不会
       报错 —— 只是安静地给出无意义的 0。
    2. **共享资源属于全部相关服务**，不是"先登记者独占"。同一容器下跑两个
       AI 服务时，若让第一个服务独占整条祖先链，第二个服务会显示"未绑定
       GPU"、事件计数为 0 —— 那是**错误信息**。共享资源的数字确实无法拆到
       单个服务头上，但"共享一个数字"远比"谎报没有"诚实。
    """
    owners: dict[str, list[str]] = {}

    for workload_id in workload_ids:
        current = workload_id
        seen: set[str] = {workload_id}
        depth = 0
        while depth < MAX_CHAIN_DEPTH:
            parent = parents.get(current)
            if parent is None or parent in seen:
                break
            seen.add(parent)
            owners.setdefault(parent, []).append(workload_id)
            current = parent
            depth += 1

    for workload_id in workload_ids:
        owners.setdefault(workload_id, []).append(workload_id)
    return owners


def _count_rollups(
    session: Session,
    owners: dict[str, list[str]],
    window_from: datetime,
    window_to: datetime,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """按资源统计窗口内的事件/告警数，再按归属归并到工作负载。

    在 SQL 里按 `resource_id` 聚合（走 `ix_event_resource_occurred` 与
    `alert.resource_id` 索引），然后在内存里做归属归并 —— 比在 SQL 里拼一张
    "资源→工作负载"的临时表简单得多，也不需要递归 CTE。
    """
    event_rows = session.execute(
        select(Event.resource_id, func.count())
        .where(Event.occurred_at >= window_from, Event.occurred_at <= window_to)
        .group_by(Event.resource_id)
    ).all()

    alert_rows = session.execute(
        select(Alert.resource_id, Alert.state, func.count())
        .where(Alert.last_fired_at >= window_from, Alert.last_fired_at <= window_to)
        .group_by(Alert.resource_id, Alert.state)
    ).all()

    event_counts: dict[str, int] = {}
    for resource_id, count in event_rows:
        # 共享资源上的事件对**每个**相关服务都计数：容器上的重启确实同时影响它
        # 承载的两个服务，只算一个会低估爆炸半径；只算"先登记者"则会让另一个
        # 服务显示 0 —— 那是错误信息。
        for owner in owners.get(resource_id, ()):
            event_counts[owner] = event_counts.get(owner, 0) + int(count)

    alert_counts: dict[str, int] = {}
    firing_counts: dict[str, int] = {}
    for resource_id, state, count in alert_rows:
        for owner in owners.get(resource_id, ()):
            alert_counts[owner] = alert_counts.get(owner, 0) + int(count)
            # `firing` 与 `acked` 都算"未恢复"：前者是待处理，后者是处理中
            if state in (AlertState.FIRING.value, AlertState.ACKED.value):
                firing_counts[owner] = firing_counts.get(owner, 0) + int(count)

    return event_counts, alert_counts, firing_counts


def _gpu_usage_for(
    session: Session,
    owners: dict[str, list[str]],
    workload_ids: list[str],
) -> dict[str, WorkloadResourceUsage]:
    """取每个工作负载**所在链路**上的 GPU 占用。

    **只取真实存在的属性，缺失就返回带 `note` 的空对象。** 这是本函数存在的
    全部意义：如果为了"界面好看"而填 0 或做估算，整个 GPU 归因结论的可信度都会
    被拉低 —— 而 GPU 归因是命题的评分重点（可观测与诊断 25%）。

    `memTotalBytes` 来自 ZSvirt 资产 API（权威、静态容量），
    `memUsedBytes` / `utilizationPct` 只在 L2（ZWatch）或 L3（探针）提供时才有。
    """
    if not workload_ids:
        return {}

    # 归属表已经给出了"哪些资源属于哪个工作负载"，这里只需挑出其中的 GPU / vGPU。
    # 资源类型一次查全表取回 —— 逐个 `session.get` 会变成 N 次查询。
    gpu_kinds = (ResourceKind.GPU.value, ResourceKind.VGPU.value)
    kind_by_id = dict(session.execute(select(Resource.id, Resource.kind)).all())
    member_gpus: dict[str, list[str]] = {wid: [] for wid in workload_ids}
    for resource_id, resource_owners in owners.items():
        if kind_by_id.get(resource_id) not in gpu_kinds:
            continue
        for owner in resource_owners:
            if owner in member_gpus:
                member_gpus[owner].append(resource_id)

    wanted = sorted({g for gpus in member_gpus.values() for g in gpus})
    if not wanted:
        return {
            wid: WorkloadResourceUsage(
                note="链路中无 GPU / vGPU 资源（该服务未绑定 GPU）"
            )
            for wid in workload_ids
        }

    gpu_rows = {
        r.id: r
        for r in session.execute(select(Resource).where(Resource.id.in_(wanted))).scalars()
    }

    result: dict[str, WorkloadResourceUsage] = {}
    for wid, gpus in member_gpus.items():
        if not gpus:
            result[wid] = WorkloadResourceUsage(
                note="链路中无 GPU / vGPU 资源（该服务未绑定 GPU）"
            )
            continue

        # `or` 会让合法的 0 被后面的值覆盖（"显存占用 0"是好状态，不是缺失），
        # 所以用 _pick_* 做显式的"第一个非 None"。
        totals: list[int | None] = []
        useds: list[int | None] = []
        utils: list[float | None] = []
        for gid in gpus:
            row = gpu_rows.get(gid)
            attrs = (row.attributes or {}) if row is not None else {}
            totals.append(_first_int(attrs, "memTotalBytes", "memory", "mem_total_bytes"))
            useds.append(_first_int(attrs, "memUsedBytes", "mem_used_bytes"))
            utils.append(_first_float(attrs, "utilizationPct", "utilization_pct"))

        total = _pick_int(totals)
        used = _pick_int(useds)
        util = _pick_float(utils)

        missing = [
            name
            for name, value in (("memUsedBytes", used), ("utilizationPct", util))
            if value is None
        ]
        note = None
        if missing:
            note = (
                "以下指标未采集到，故为 null（不以 0 代替）："
                + ", ".join(missing)
                + "。" + GPU_METRIC_SOURCE_HINT
            )

        result[wid] = WorkloadResourceUsage(
            gpuMemoryUsedBytes=used,
            gpuMemoryTotalBytes=total,
            gpuUtilizationPct=util,
            note=note,
        )
    return result


def _pick_int(values: list[int | None]) -> int | None:
    """取第一个非 None 的整数。显式写出是因为 `or` 会吞掉合法的 0。"""
    for value in values:
        if value is not None:
            return value
    return None


def _pick_float(values: list[float | None]) -> float | None:
    """取第一个非 None 的浮点数（理由同 `_pick_int`）。"""
    for value in values:
        if value is not None:
            return value
    return None


def _first_int(attrs: dict[str, Any], *keys: str) -> int | None:
    """按优先级取第一个可转为 int 的属性值。"""
    for key in keys:
        if attrs.get(key) is None:
            continue
        try:
            return int(attrs[key])
        except (TypeError, ValueError):
            continue
    return None


def _first_float(attrs: dict[str, Any], *keys: str) -> float | None:
    """按优先级取第一个可转为 float 的属性值（容忍 `"98%"` 这类字符串）。"""
    for key in keys:
        raw = attrs.get(key)
        if raw is None:
            continue
        try:
            return float(str(raw).rstrip("%"))
        except (TypeError, ValueError):
            continue
    return None


def _to_workload(
    resource: Resource,
    *,
    now: datetime,
    event_count: int,
    alert_count: int,
    firing_count: int,
    diagnosis: Any,
    usage: WorkloadResourceUsage | None,
) -> WorkloadData:
    """ORM 资源 → 工作负载视图。

    `staleness` 只在非 active 时给出（与拓扑端点同一口径，走同一个
    `format_staleness`）：正常资源显示"0s 前"只是噪声。
    """
    staleness = None
    if resource.observability != Observability.ACTIVE.value:
        staleness = format_staleness(resource.last_seen_at, now)

    return WorkloadData(
        id=resource.id,
        name=resource.name,
        status=resource.status,
        observability=resource.observability,
        staleness=staleness,
        attributes=resource.attributes or {},
        resourceUsage=usage or WorkloadResourceUsage(note="未解析到链路上的 GPU 资源"),
        eventCount=event_count,
        alertCount=alert_count,
        firingAlertCount=firing_count,
        diagnosisId=diagnosis.id if diagnosis is not None else None,
        rootCause=diagnosis.root_cause if diagnosis is not None else None,
        parentId=resource.parent_id,
    )


__all__ = ["DEFAULT_LIMIT", "DEFAULT_SINCE", "MAX_LIMIT", "router"]
