"""Consolidate the guest (probe) and platform (zsvirt) views of one GPU card.

## The defect this fixes

2026-09-23: A shipped guest-layer GPU collection (`nvidia-smi`) and B shipped the
ZWatch platform channel. Both report the same physical card, and — verified end
to end — the graph ended up with **two unrelated nodes** for it:

    gpu:probe:probe-3f2a9c10:GPU-3f2a9c10-...    parent=None        memUsedBytes=32641751450
    gpu:zsvirt:GPU-3f2a9c10-...                  parent=host:zsvirt  memUsagePct=95.0

Both are keyed by the *same* GPU UUID (the one identifier that does line up),
but the namespaces differ, so nothing merges them. The consequences:

- the workload overview reports "链路中无 GPU / vGPU 资源", because the ai_service's
  chain reaches neither node;
- the one measurement only the guest can produce (`memUsedBytes`) sits on an
  orphan node, and the one only the platform can produce (card-level usage) sits
  on another — so the cross-layer conclusion they jointly support is unreachable.

Per `DATA_MODEL.md` §3 the platform namespace is authoritative for `host`/`gpu`/
`vm`. So the fix is **not** to relocate the probe node: it is to make the platform
node canonical and (a) take the probe node as its child, and (b) merge the
guest-layer measurement onto it with its provenance recorded.

## What stays honest

- Nothing is invented: `memUsedBytes` here is A's **measured** byte count, and
  `memUsedBytesOrigin` says so. `memUsagePct` remains the **card-level** platform
  percentage, and `memUsageScope=card` continues to say that (D-104/D-105).
- If no probe GPU matches, behaviour is unchanged and the plan records why.

## Still open (external)

The probe GPU carries no parent either (`parentSourceId: null`) because the probe
cannot yet tell which ZSvirt VM it is in — external blocker **X-09**. Attaching it
under the platform GPU is a *structural* improvement; the chain is still missing
its `vm` tier until X-09 is answered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import ResourceKind
from app.models import Resource
from app.normalize.ids import SOURCE_PROBE, zsvirt_resource_id

__all__ = [
    "HarvestPlan",
    "HarvestResult",
    "ProbeGpu",
    "ResourceUpdate",
    "consolidate_with_probe_gpus",
    "find_probe_gpus",
    "normalize_gpu_identity",
    "zsvirt_vm_resource_id",
]


def zsvirt_vm_resource_id(vm_uuid: str) -> str:
    """平台虚拟机资源 ID。

    ⚠️ `vm_uuid` 的**获取方式尚未确定**（外部阻塞 X-09）：探针需要知道自己
    在哪台 VM 里。本函数只负责拼装，不负责获取。

    定义在这里而不是 `harvest.py`：合并逻辑需要它来改写 VM 的父节点，而
    `harvest.py` 反过来依赖本模块 —— 单一定义点必须放在依赖链的下游。
    """
    return zsvirt_resource_id(ResourceKind.VM.value, vm_uuid)


@dataclass(frozen=True)
class ResourceUpdate:
    """一次待落库的资源写入（纯数据，便于测试断言）。"""

    resource_id: str
    kind: str
    attributes: dict[str, Any]
    parent_id: str | None = None
    name: str | None = None
    #: 归一化后的 GPU 物理标识（UUID 或 PCI 地址）。平台侧与探针侧靠它对上
    #: 同一张卡；`None` 表示该资源不是 GPU，或拿不到标识。
    gpu_identity: str | None = None
    #: 应该挂在本资源**下面**的既有资源 id。用于把探针命名空间里同一张卡的
    #: 节点收编为子节点，而不是留下一个孤立的重复节点。
    child_resource_ids: list[str] = field(default_factory=list)

    def merge_attributes(self, existing: dict[str, Any] | None) -> dict[str, Any]:
        """与已有属性合并。

        合并而不是覆盖：属性来自多个接口（资产接口写型号容量，指标接口写
        利用率），互相覆盖会让"上次采到的容量"凭空消失。
        """
        merged = dict(existing or {})
        merged.update(self.attributes)
        return merged


@dataclass
class HarvestPlan:
    """一次采集的落库计划。"""

    updates: list[ResourceUpdate] = field(default_factory=list)
    #: 采集到但**建不出资源**的东西，附原因。必须暴露 —— 见 `harvest.py` 模块文档。
    unresolved: list[str] = field(default_factory=list)
    sampled_at: datetime | None = None

    @property
    def is_empty(self) -> bool:
        return not self.updates


@dataclass
class HarvestResult:
    """落库结果。"""

    resources: int = 0
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    #: 被收编到平台节点下的探针侧资源（同一张卡的访客视图）。
    reparented: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "resources": self.resources,
            "created": self.created,
            "updated": self.updated,
            "unresolved": self.unresolved,
            "reparented": self.reparented,
        }


#: 采集侧对同一标识的不同写法。至少要能认出这两种：
#:
#: - nvidia-smi（成员 A）：`00000000:00:08.0`（域是 8 位十六进制）
#: - ZWatch 标签（平台侧）：`0000:00:08.0`（域是 4 位）
#:
#: 归一化只做"去歧义"的最小处理：小写、去掉前导零域里的零、去掉空格。
#: **不做**任何猜测性改写 —— 认不出来就当作不同卡，交给 `unresolved` 说明。
_PCI_PATTERN = re.compile(r"^([0-9a-f]{4,8}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])$")


def normalize_gpu_identity(value: str | None) -> str | None:
    """把 GPU 标识（UUID 或 PCI 地址）归一化成可比较的形式。"""
    if not value:
        return None
    text = value.strip().lower()
    if not text:
        return None
    match = _PCI_PATTERN.match(text)
    if match:
        # PCI 地址：去掉域的前导零，得到 `0:00:08.0` 这种规范写法
        domain = match.group(1).lstrip("0") or "0"
        return f"{domain}:{match.group(2)}:{match.group(3)}.{match.group(4)}"
    return text


@dataclass(frozen=True)
class ProbeGpu:
    """一个来自探针命名空间的 GPU 资源。"""

    resource_id: str
    identity: str
    normalized_identity: str
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def mem_used_bytes(self) -> int | None:
        raw = self.attributes.get("memUsedBytes")
        return int(raw) if isinstance(raw, (int, float)) else None

    @property
    def mem_total_bytes(self) -> int | None:
        raw = self.attributes.get("memTotalBytes")
        return int(raw) if isinstance(raw, (int, float)) else None

    @property
    def model(self) -> str | None:
        raw = self.attributes.get("model")
        return raw if isinstance(raw, str) and raw.strip() else None


def find_probe_gpus(session: Session) -> list[ProbeGpu]:
    """读出所有探针命名空间里的 `gpu` 资源。

    按 `kind` 过滤**在 SQL 里**做（`id` 前缀匹配会让 `LIKE 'gpu:%'` 顺带扫到
    别的类型，而且索引用不上）。
    """
    rows = session.execute(
        select(Resource).where(Resource.kind == ResourceKind.GPU.value)
    ).scalars()

    found: list[ProbeGpu] = []
    for row in rows:
        parts = row.id.split(":")
        # `{kind}:{source}:...`；只认探针来源，平台来源由本模块产出、不需要再合并
        if len(parts) < 3 or parts[1] != SOURCE_PROBE:
            continue
        attrs = dict(row.attributes or {})
        # 优先用 attributes 里的 uuid（A 两边都带），其次用 sourceId 段
        raw_identity = attrs.get("uuid") or (parts[-1] if len(parts) >= 4 else None)
        normalized = normalize_gpu_identity(raw_identity if isinstance(raw_identity, str) else None)
        if not normalized:
            continue
        found.append(
            ProbeGpu(
                resource_id=row.id,
                identity=str(raw_identity),
                normalized_identity=normalized,
                attributes=attrs,
            )
        )
    return found


def consolidate_with_probe_gpus(
    plan: HarvestPlan,
    probe_gpus: list[ProbeGpu],
    *,
    include_vm: str | None = None,
) -> HarvestPlan:
    """把探针侧同一张卡挂到平台节点下，并合并访客层测量值。

    `plan` 必须是 `map_readings()` 的产物（其 GPU update 带 `gpu_identity`）。
    `include_vm` 给出 ZSvirt VM UUID 时，VM 会改挂到平台 GPU 下（`host→gpu→vm`），
    与 `map_reading` 的拓扑一致；否则保持原样。
    """
    if not probe_gpus:
        return plan

    by_identity: dict[str, ProbeGpu] = {p.normalized_identity: p for p in probe_gpus}

    updates: list[ResourceUpdate] = []
    for update in plan.updates:
        if update.kind != ResourceKind.GPU.value or not update.gpu_identity:
            updates.append(update)
            continue

        match = by_identity.get(update.gpu_identity)
        if match is None:
            updates.append(update)
            plan.unresolved.append(
                f"平台 GPU {update.resource_id} 在探针命名空间里没有对应资源："
                "访客层测量值（精确已用显存）不可得，只有卡级百分比"
            )
            continue

        merged_attrs = dict(update.attributes)
        if match.mem_used_bytes is not None:
            # **这是测量值，不是推算值**：来自 VM 内 nvidia-smi，因此可以写进
            # `memUsedBytes`（D-105 禁止的是"用百分比乘总容量反推"）。
            # 同时记录来源，避免它被误认为 ZWatch 提供的字节数。
            merged_attrs["memUsedBytes"] = match.mem_used_bytes
            merged_attrs["memUsedBytesOrigin"] = "probe"
        if match.mem_total_bytes and not merged_attrs.get("memTotalBytes"):
            merged_attrs["memTotalBytes"] = match.mem_total_bytes
        if match.model:
            # 资产接口的型号更权威（平台侧），只有它缺位时才用探针侧的
            merged_attrs.setdefault("model", match.model)

        updates.append(
            ResourceUpdate(
                resource_id=update.resource_id,
                kind=update.kind,
                attributes=merged_attrs,
                # 平台节点仍是权威；探针节点作为**子**节点保留，因为它的 ID 已经
                # 出现在历史事件里，删掉会让那些引用变成悬空。
                parent_id=update.parent_id,
                name=update.name,
                gpu_identity=update.gpu_identity,
                child_resource_ids=[*(update.child_resource_ids or []), match.resource_id],
            )
        )

    consolidated = HarvestPlan(
        updates=updates,
        unresolved=list(plan.unresolved),
        sampled_at=plan.sampled_at,
    )

    if include_vm:
        vm_id = zsvirt_vm_resource_id(include_vm)
        consolidated.updates = [
            ResourceUpdate(
                resource_id=u.resource_id,
                kind=u.kind,
                attributes=u.attributes,
                parent_id=(
                    next(
                        (
                            x.resource_id
                            for x in consolidated.updates
                            if x.kind == ResourceKind.GPU.value
                        ),
                        u.parent_id,
                    )
                    if u.resource_id == vm_id
                    else u.parent_id
                ),
                name=u.name,
                gpu_identity=u.gpu_identity,
                child_resource_ids=u.child_resource_ids,
            )
            for u in consolidated.updates
        ]

    return consolidated
