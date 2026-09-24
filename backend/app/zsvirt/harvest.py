"""平台层命名空间桥接：把 ZWatch 读数落到资源图（关闭 D-101）。

## 这一层在解决什么

到 2026-09-20 为止，`zsvirt_resource_id()`（`app/normalize/ids.py`）**在整个代码库
没有调用方** —— 平台命名空间（`{kind}:zsvirt:{uuid}`）从来没有被真正写入过，
平台层资源只能由探针"代报"到探针命名空间里。跨层关联的最后一公里因此断着
（`docs/DECISIONS.md` D-101，标为 🔴 关键路径阻塞）。

命题方答复确认 ZWatch 已启用，且指标标签带 `HostUuid` / `PciDeviceAddress` /
`GpuSerialNumber`，于是这一层做两件事：

1. **建桥**：把 ZWatch 标签里的平台标识翻译成 `{kind}:zsvirt:{uuid}` 资源 ID；
2. **落图**：把读数作为属性写进 `resource` 表 —— 这正是 `/api/v1/workloads`
   已经在读的位置（`_gpu_usage_for` 读 `memTotalBytes` / `utilizationPct`），
   所以前端不改就能看到真实 GPU 数字。

## 为什么直接写库而不是塞进 ingest

`resource.attributes` 是"来源系统拥有的字段"，而平台层属性的**来源系统就是
ZSVirt**。走 `POST /api/v1/ingest/batch` 必须伪造成探针上报的形态，那会让
`source=probe` 命名空间里多出一批其实是平台侧的资源 —— D-101 描述的正是这个
错误状态。因此这里直接调用 `graph/repository.upsert_resource`，并且**不改变
`status`**（那条铁律：`status` 由来源系统拥有）。

## 仍然做不到的（如实标注，不假装）

- **VM 资源建不出来**：`kind=vm` 需要 ZSvirt 的 VM UUID，而"探针如何获得自己的
  `vmId`"仍是未解决的外部阻塞（X-09，第二轮提问 §5.2 已发问）。`vm_uuid`
  默认为 None，此时**不产出 VM 资源**，并在 `HarvestPlan.unresolved` 里记一条
  原因 —— 而不是编一个 ID 把图连起来。
- **宿主名/型号未知**：`QueryHost` 尚未接入。宿主资源以 `HostUuid` 为标识建立，
  `name` 留空（`name` 可空且"不得用作标识"，DATA_MODEL §3）。
- **直通模式下没有按虚拟机的占用**：见 `AttributionBasis`。落进属性里的
  `memUsagePct` 是**卡级**使用率，`attribution` / `memUsageScope` 会把这一点
  一路带到 API 响应，让前端能如实标注。
- **访客层的精确字节数来自探针，不来自本模块**：`memUsedBytes` 由
  `app/zsvirt/consolidate.py` 从探针命名空间合并进来，并标注
  `memUsedBytesOrigin="probe"`。本模块自己**不换算**字节数（D-105）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.enums import ResourceKind
from app.graph.repository import ensure_edge, upsert_resource
from app.models import Resource
from app.normalize.ids import zsvirt_resource_id
from app.zsvirt import GpuMetricReading
from app.zsvirt.consolidate import (
    HarvestPlan,
    HarvestResult,
    ResourceUpdate,
    consolidate_with_probe_gpus,
    find_probe_gpus,
    zsvirt_vm_resource_id,
)
from app.zsvirt.consolidate import normalize_gpu_identity as _normalize_identity

__all__ = [
    "HarvestPlan",
    "HarvestResult",
    "ResourceUpdate",
    "harvest_once",
    "map_reading",
    "map_readings",
    "persist_plan",
    "zsvirt_host_resource_id",
    "zsvirt_vm_resource_id",
]


def zsvirt_host_resource_id(host_uuid: str) -> str:
    """平台宿主资源 ID。"""
    return zsvirt_resource_id(ResourceKind.HOST.value, host_uuid)


def _host_uuid_of(reading: GpuMetricReading) -> str | None:
    for key in ("HostUuid", "hostUuid"):
        value = reading.labels.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _pci_of(reading: GpuMetricReading) -> str | None:
    for key in ("PciDeviceAddress", "pciDeviceAddress", "pciAddress"):
        value = reading.labels.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def map_reading(
    reading: GpuMetricReading,
    *,
    vm_uuid: str | None = None,
    asset: dict[str, Any] | None = None,
) -> HarvestPlan:
    """把一条 GPU 读数映射成待落库的资源。

    `asset` 是 `QueryGpuDevice` 的静态资产字段（型号/容量/功耗/驱动），可选 ——
    指标接口本身不含这些。

    **不做**的事：不猜 `vm_uuid`、不猜宿主名、不把百分比换算成字节数。
    """
    plan = HarvestPlan(sampled_at=reading.sampled_at)
    asset = asset or {}
    pci_address = _pci_of(reading)

    # ---- 宿主 ----
    host_uuid = _host_uuid_of(reading)
    host_id: str | None = None
    if host_uuid:
        host_id = zsvirt_host_resource_id(host_uuid)
        plan.updates.append(
            ResourceUpdate(
                resource_id=host_id,
                kind=ResourceKind.HOST.value,
                attributes={"hostUuid": host_uuid, "origin": reading.origin},
            )
        )
    else:
        plan.unresolved.append("读数缺少 HostUuid 标签：宿主资源无法建立，GPU 将挂在顶层")

    # ---- GPU ----
    # GPU 的 sourceId 用**序列号**：PCI 地址形如 `0000:01:00.0` 含冒号，
    # 而 `build_resource_id` 明确禁止 sourceId 含冒号（否则 ID 拆不回三段）。
    gpu_key = reading.gpu_serial or pci_address
    if not gpu_key:
        plan.unresolved.append("读数既无 GPU 序列号也无 PCI 地址：GPU 资源无法建立")
        return plan

    if ":" in gpu_key:
        # 只可能是 PCI 地址兜底路径。把冒号换掉而不是报错 —— 但要点明这是降级。
        gpu_key = gpu_key.replace(":", "-")
        plan.unresolved.append(
            f"GPU 无序列号，用 PCI 地址兜底并把冒号替换为连字符（{gpu_key}）："
            "该 ID 与平台侧 `QueryGpuDevice` 返回的 Uuid 不一致，跨层比对时需注意"
        )

    attributes: dict[str, Any] = {
        # 属性名与既有消费者对齐（`app/api/workloads.py::_gpu_usage_for`、
        # `app/zsvirt/scenarios.py`）—— 前端不改就能读到。
        "utilizationPct": reading.utilization_pct,
        "memUsagePct": reading.host_mem_usage_pct,
        "origin": reading.origin,
        "attribution": reading.attribution,
        "sampledAt": reading.sampled_at.isoformat(),
    }
    # 只在真有观测值时写入：写 None 会让"缺失"在库里看起来像一个值。
    if reading.mem_total_bytes:
        attributes["memTotalBytes"] = reading.mem_total_bytes
    if reading.mem_used_bytes is not None:
        attributes["memUsedBytes"] = reading.mem_used_bytes
    if reading.temperature_c is not None:
        attributes["temperatureC"] = reading.temperature_c
    if pci_address:
        attributes["pciAddress"] = pci_address
    if reading.gpu_serial:
        attributes["serialNumber"] = reading.gpu_serial
    if reading.attribution == "passthrough":
        # 如实标注：这是**卡级**使用率，不是本虚拟机占用。
        attributes["memUsageScope"] = "card"

    for key in ("model", "memTotalBytes", "power", "isDriverLoaded"):
        if asset.get(key) is not None and key not in attributes:
            attributes[key] = asset[key]

    gpu_id = zsvirt_resource_id(ResourceKind.GPU.value, gpu_key)
    plan.updates.append(
        ResourceUpdate(
            resource_id=gpu_id,
            kind=ResourceKind.GPU.value,
            attributes=attributes,
            parent_id=host_id,
            # 记录物理标识，供 `consolidate.py` 与探针侧同一张卡对齐。
            # **必须在这里归一化**：nvidia-smi 写 `00000000:00:08.0`，
            # ZWatch 写 `0000:00:08.0`，字面比对会认不出是同一张卡。
            gpu_identity=_normalize_identity(gpu_key),
        )
    )

    # ---- VM ----
    if vm_uuid:
        plan.updates.append(
            ResourceUpdate(
                resource_id=zsvirt_vm_resource_id(vm_uuid),
                kind=ResourceKind.VM.value,
                attributes={"vmUuid": vm_uuid, "origin": reading.origin},
                # GPU 直通拓扑就是 host → gpu → vm。
                parent_id=gpu_id,
            )
        )
    else:
        plan.unresolved.append(
            "未提供 ZSvirt VM UUID，未建立 vm 资源：探针 vmId 的获取方式仍未确定"
            "（外部阻塞 X-09，见 docs/ZSVIRT_QA_ROUND2.md §5.2）"
        )

    return plan


def map_readings(
    readings: list[GpuMetricReading],
    *,
    vm_uuid: str | None = None,
    assets: dict[str, dict[str, Any]] | None = None,
) -> HarvestPlan:
    """批量映射，产出**一份合并后**的计划。

    同一宿主会被多条读数重复产出 —— 这里按 `resource_id` 去重并合并属性，
    避免同一次采集里对同一行写多次。
    """
    assets = assets or {}
    combined = HarvestPlan(sampled_at=readings[0].sampled_at if readings else None)
    by_id: dict[str, ResourceUpdate] = {}

    for reading in readings:
        plan = map_reading(reading, vm_uuid=vm_uuid, asset=assets.get(reading.gpu_serial))
        combined.unresolved.extend(plan.unresolved)
        for update in plan.updates:
            existing = by_id.get(update.resource_id)
            if existing is None:
                by_id[update.resource_id] = update
            else:
                by_id[update.resource_id] = ResourceUpdate(
                    resource_id=update.resource_id,
                    kind=update.kind,
                    attributes={**existing.attributes, **update.attributes},
                    parent_id=existing.parent_id or update.parent_id,
                    name=existing.name or update.name,
                )

    combined.updates = list(by_id.values())
    return combined


def persist_plan(
    session: Session,
    plan: HarvestPlan,
    *,
    seen_at: datetime | None = None,
) -> HarvestResult:
    """把计划落库。**不改变 `status`**（见模块文档）。"""
    moment = seen_at or plan.sampled_at or datetime.now(UTC)
    result = HarvestResult(unresolved=list(plan.unresolved))

    # 父资源先写：GPU 的 parent_id 指向宿主，外键要求父先存在。
    ordered = sorted(plan.updates, key=lambda u: 0 if u.kind == ResourceKind.HOST.value else 1)

    for update in ordered:
        existing = session.get(Resource, update.resource_id)
        existed = existing is not None
        row = upsert_resource(
            session,
            resource_id=update.resource_id,
            kind=update.kind,
            name=update.name,
            parent_id=update.parent_id,
            attributes=update.merge_attributes(existing.attributes if existed else None),
            seen_at=moment,
        )
        result.resources += 1
        (result.updated if existed else result.created).append(row.id)

        # 收编子节点（探针命名空间里同一张卡的节点）。必须在父写入**之后**，
        # 否则外键约束会拒绝 —— 与父资源先写是同一个理由。
        for child_id in update.child_resource_ids:
            ensure_edge(session, update.resource_id, child_id, relation="alias")
            result.reparented.append(child_id)

    return result


def harvest_once(
    session: Session,
    *,
    provider: Any,
    vm_uuid: str | None = None,
    seen_at: datetime | None = None,
) -> HarvestResult:
    """采集一次真实 GPU 指标并落库。

    `provider` 是 `ZWatchProvider`。它内部已带读数缓存，因此这个函数被高频调用
    也不会把管理节点打穿（见 `ZWatchProvider.DEFAULT_CACHE_TTL_SEC`）。
    """
    readings = provider.read_all()
    assets: dict[str, dict[str, Any]] = {}
    for asset in provider.assets():
        assets[asset.serial_number] = {
            "model": asset.model,
            "memTotalBytes": asset.mem_total_bytes or None,
            "power": asset.power_watts or None,
            "isDriverLoaded": asset.is_driver_loaded,
        }

    plan = map_readings(readings, vm_uuid=vm_uuid, assets=assets)

    # 把探针命名空间里同一张卡的节点收编到平台节点下，并合并访客层测量值
    # （精确 `memUsedBytes` 只有 VM 内 `nvidia-smi` 能提供 —— 见 D-105）。
    plan = consolidate_with_probe_gpus(plan, find_probe_gpus(session), include_vm=vm_uuid)

    return persist_plan(session, plan, seen_at=seen_at)
