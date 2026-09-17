"""资源 ID 拼装与解析。

契约见 `docs/DATA_MODEL.md` §3、`ADR-0002`（已 Accepted）。

格式：`{kind}:{source}:{sourceId}`

来源归属（权威方）：

| kind | source | 说明 |
|---|---|---|
| `host` / `gpu` / `vgpu` / `vm` | `zsvirt` | 平台侧权威定义 |
| `container` / `process` / `ai_service` / `agent` / `task` | `probe` | VM 内探针权威定义 |
| 任意 | `manual` | 演示期手工登记，不阻塞联调 |

**关键约束**（ADR-0002）：

1. ID **不透明** —— 消费方（成员 C）不得解析其结构，只能整体比对与传递。
   本模块提供 `parse_resource_id` 仅供 B 内部使用。
2. ID **不可变** —— 资源重建视为新资源，不复用旧 ID。
3. ID **不承载语义** —— 不得从中推断状态、时间或健康度。

探针侧 `sourceId` 规则由成员 A 确认（`API_CONTRACT.md` §3.3.1）：

| kind | `sourceId` | 稳定性来源 |
|---|---|---|
| `container` | 容器 ID 前 12 位（或唯一容器名） | 容器生命周期内不变 |
| `process` | `starttime + pid` | **避免 pid 复用冲突** |
| `ai_service` | 服务名 + 监听端口 | 服务实例稳定标识 |
| `agent` / `task` | 探针分配的 ULID | 全局唯一 |
"""

from __future__ import annotations

from dataclasses import dataclass

from app.graph.algorithms import RESOURCE_KINDS

SOURCE_ZSVIRT = "zsvirt"
SOURCE_PROBE = "probe"
SOURCE_MANUAL = "manual"

VALID_SOURCES = frozenset({SOURCE_ZSVIRT, SOURCE_PROBE, SOURCE_MANUAL})

#: 每种资源类型的权威来源。用于校验拼装是否合理。
AUTHORITATIVE_SOURCE: dict[str, str] = {
    "host": SOURCE_ZSVIRT,
    "gpu": SOURCE_ZSVIRT,
    "vgpu": SOURCE_ZSVIRT,
    "vm": SOURCE_ZSVIRT,
    "container": SOURCE_PROBE,
    "process": SOURCE_PROBE,
    "ai_service": SOURCE_PROBE,
    "agent": SOURCE_PROBE,
    "task": SOURCE_PROBE,
}


class InvalidResourceId(ValueError):
    """资源 ID 格式非法。"""


@dataclass(frozen=True)
class ResourceRef:
    """探针上报时的资源引用（用 `sourceId`，不含全局 ID）。"""

    kind: str
    source_id: str


def build_resource_id(kind: str, source: str, source_id: str) -> str:
    """拼装全局资源 ID。

    校验收紧但不越界：

    - `kind` 必须是已冻结的枚举值（F-04）
    - `source` 必须是 `zsvirt` / `probe` / `manual`
    - `source_id` 不得为空，且**不得包含冒号**（否则 ID 无法解析回三段）

    注意：不校验 `kind` 与 `source` 的搭配是否"权威"—— `manual` 允许登记
    任意类型（演示期兜底），因此搭配校验交由调用方按需调用
    `check_authoritative`。
    """
    if kind not in RESOURCE_KINDS:
        raise InvalidResourceId(f"unknown resource kind: {kind!r}")
    if source not in VALID_SOURCES:
        raise InvalidResourceId(f"unknown source: {source!r}")
    if not source_id:
        raise InvalidResourceId("source_id must not be empty")
    if ":" in source_id:
        raise InvalidResourceId(
            f"source_id must not contain ':' (got {source_id!r}); "
            "otherwise the id cannot be split back into three parts"
        )
    return f"{kind}:{source}:{source_id}"


def parse_resource_id(resource_id: str) -> tuple[str, str, str]:
    """把全局 ID 拆回 `(kind, source, remainder)`。

    `maxsplit=2`：前三段固定是 kind / source / 其余，
    因此探针的四段 ID 会把 `{agentId}:{sourceId}` 一起留在第三段。

    **仅供 B 内部使用**。成员 C 不得解析 ID 结构（ADR-0002 约束）。
    """
    parts = resource_id.split(":", 2)
    if len(parts) != 3:
        raise InvalidResourceId(f"malformed resource id: {resource_id!r}")
    kind, source, remainder = parts
    if kind not in RESOURCE_KINDS:
        raise InvalidResourceId(f"unknown resource kind in id: {kind!r}")
    if source not in VALID_SOURCES:
        raise InvalidResourceId(f"unknown source in id: {source!r}")
    return kind, source, remainder


def check_authoritative(kind: str, source: str) -> bool:
    """校验 `kind` 与 `source` 的搭配是否符合权威归属。

    `manual` 一律放行（演示期手工登记）。返回 False 不抛异常，
    由调用方决定是记警告还是拒收。
    """
    if source == SOURCE_MANUAL:
        return True
    expected = AUTHORITATIVE_SOURCE.get(kind)
    return expected is None or expected == source


def probe_resource_id(agent_id: str, ref: ResourceRef) -> str:
    """探针侧资源 → 全局 ID。

    契约：`{kind}:probe:{agentId}:{sourceId}`（`API_CONTRACT.md` §3.3.1）
    —— **共四段**，`agentId` 与 `sourceId` 各自占一段。

    因此这里不能用 `build_resource_id` 拼装（它会把 col3 限制为不含冒号）。
    改为手工拼四段，同时分别校验两段各自合法（不得含冒号、不得为空），
    这样既满足契约形状，又保证每一段仍可解析。
    """
    if ref.kind not in RESOURCE_KINDS:
        raise InvalidResourceId(f"unknown resource kind: {ref.kind!r}")
    if not agent_id:
        raise InvalidResourceId("agent_id must not be empty")
    if ":" in agent_id:
        raise InvalidResourceId(f"agent_id must not contain ':' (got {agent_id!r})")
    if not ref.source_id:
        raise InvalidResourceId("source_id must not be empty")
    if ":" in ref.source_id:
        # 注意：process 的 A 侧规则是 `starttime + pid`，用 `:` 连接会让
        # sourceId 含冒号，从而产生第五段。约定用 `.` 连接（见 §3.3.1）。
        raise InvalidResourceId(
            f"source_id must not contain ':' (got {ref.source_id!r}); "
            "process 的 starttime+pid 请用 '.' 连接，例如 '12345.678'"
        )
    return f"{ref.kind}:{SOURCE_PROBE}:{agent_id}:{ref.source_id}"


def zsvirt_resource_id(kind: str, uuid: str) -> str:
    """ZSvirt 平台资源 → 全局 ID。"""
    return build_resource_id(kind, SOURCE_ZSVIRT, uuid)
