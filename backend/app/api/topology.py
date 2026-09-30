"""`GET /api/v1/topology` —— 资源拓扑查询。

契约：`docs/API_CONTRACT.md` §4.2；成员 C 的 Q12 明确要求：

- 单次响应上限 **节点 ≤ 200、边 ≤ 400**
- 超限时用 `rootId` / `depth` / `kinds` 服务端裁剪
- 必须返回 `truncated: true`，C 据此提示「结果被裁剪，请下钻」，
  而不是静默显示不完整拓扑

响应为**扁平的 `nodes` + `edges`**（ECharts graph 的形态），而不是嵌套树 ——
嵌套树还要前端再拍平，且不利于增量渲染。

`includeStale` 的语义见 `docs/DATA_MODEL.md` §4.1：节点同时带
`status`（业务状态）与 `observability`（B 的观测状态），两者独立。
**`stale` 不会被混进 `status`** —— 这是 C 提出的 D-032 的核心约定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.common import build_meta, error_response, invalid_argument
from app.api.schemas.topology import TopologyData, TopologyEdge, TopologyNode
from app.db import get_db
from app.enums import REAL_RESOURCE_KINDS
from app.graph.repository import (
    MAX_TOPOLOGY_EDGES,
    MAX_TOPOLOGY_NODES,
    format_staleness,
    load_graph,
    trim_topology,
)

router = APIRouter(tags=["topology"])

DbSession = Annotated[Session, Depends(get_db)]

#: depth 上限。超过会导致一次查询扫全库，与"服务端裁剪"的初衷相悖。
MAX_DEPTH = 8
DEFAULT_DEPTH = 3

#: `kinds` 参数上限（防止 URL 过长与无效枚举）
MAX_KINDS = len(REAL_RESOURCE_KINDS)


@router.get("/api/v1/topology")
def get_topology(
    session: DbSession,
    rootId: str | None = Query(default=None, description="从该资源开始；省略则返回全部根节点"),
    depth: int = Query(default=DEFAULT_DEPTH, ge=0, le=MAX_DEPTH),
    kinds: str | None = Query(default=None, description="逗号分隔的资源类型过滤"),
    includeStale: bool = Query(default=False, description="是否包含 gone 资源（排障用）"),
) -> Any:
    """查询资源拓扑。"""
    kind_list = _parse_kinds(kinds)
    if kind_list is _INVALID:
        return invalid_argument(
            f"kinds 含未知资源类型。允许值：{sorted(REAL_RESOURCE_KINDS)}",
        )

    now = datetime.now(UTC)

    if rootId is None:
        # 未指定根节点：加载全图（仍受 max_depth 之外的规模上限保护）
        nodes, edges = load_graph(
            session,
            root_id=None,
            include_stale=includeStale,
            kinds=kind_list,
        )
    else:
        # 指定根节点：先确认它存在，避免前端拿到空图却不知道原因
        from app.models import Resource

        if session.get(Resource, rootId) is None:
            return error_response(
                404,
                "RESOURCE_NOT_FOUND",
                f"资源不存在：{rootId}",
                details={"rootId": rootId},
            )
        nodes, edges = load_graph(
            session,
            root_id=rootId,
            max_depth=depth,
            include_stale=includeStale,
            kinds=kind_list,
        )

    kept_nodes, kept_edges, dropped, reason = trim_topology(nodes, edges)
    truncated = dropped > 0 or (reason is not None)

    data = TopologyData(
        nodes=[_to_node(r, now) for r in kept_nodes],
        edges=[
            TopologyEdge(parentId=e.parent_id, childId=e.child_id, relation=e.relation)
            for e in kept_edges
        ],
        truncated=truncated,
        truncationReason=reason,
        droppedNodes=dropped,
    )

    return {
        "data": data.model_dump(mode="json"),
        "meta": build_meta().model_dump(mode="json"),
    }


class _Invalid:
    """哨兵类型：把"解析失败"与"未提供"区分开（`None` 表示未提供）。"""


_INVALID = _Invalid()


def _parse_kinds(kinds: str | None) -> list[str] | None | _Invalid:
    """解析逗号分隔的 kinds 参数。

    返回 `None` 表示未提供；返回 `_INVALID` 表示含非法值。
    **非法值不静默忽略** —— 否则前端会以为是"没有这种资源"，
    而实际是拼错了枚举名。
    """
    if not kinds:
        return None
    parts = [p.strip() for p in kinds.split(",") if p.strip()]
    if not parts:
        return None
    if len(parts) > MAX_KINDS:
        return _INVALID
    allowed = set(REAL_RESOURCE_KINDS)
    if any(p not in allowed for p in parts):
        return _INVALID
    return parts


#: 距上次观测多久开始报 `staleness`。低于它保持 `None`。
#:
#: 300 秒的理由：轮询类采集（探针 10s、平台同步 30s）正常运行时年龄是秒级；
#: 超过 5 分钟已经不是抖动。而"0s 前"这类噪声才是原实现要避免的东西 ——
#: 所以这里去掉的是噪声，不是信息。
TOPOLOGY_STALENESS_WARN_SEC = 300


def _to_node(resource: Any, now: datetime) -> TopologyNode:
    """ORM 资源 → 拓扑节点。

    **`staleness` 与 `observability` 解耦。** 早先只在 `observability != active`
    时给出它，理由是"正常资源不必显示 0s 前"。但后台陈旧标记默认关闭（演示数据
    时间基准固定在过去，开启会全体变 stale），于是 `observability` 永远是
    `active`、`staleness` **永远是空** —— 一个契约承诺给前端的字段永久为空。

    更要紧的是耦合本身就不对：「多久没听到这个资源的心跳」是一个**关于时间的事实**，
    不是判决。挂在判决上会让界面在越过阈值**之前**无法预警，也无法显示年龄。

    现在的规则：年龄超过 `TOPOLOGY_STALENESS_WARN_SEC` 就给出，与
    `observability` 无关；低于阈值仍为 `None`（保留原来去噪声的意图）。
    `isStale` 另给一个布尔，免得前端去解析 `"13d"` 这种字符串来判断是否高亮。
    """
    age_seconds = max(0, int((now - resource.last_seen_at).total_seconds()))
    over_threshold = age_seconds >= TOPOLOGY_STALENESS_WARN_SEC
    staleness = format_staleness(resource.last_seen_at, now) if over_threshold else None

    return TopologyNode(
        id=resource.id,
        kind=resource.kind,
        name=resource.name,
        parentId=resource.parent_id,
        status=resource.status,
        observability=resource.observability,
        staleness=staleness,
        #: 是否需要前端提示"数据陈旧"。布尔而不是让前端比字符串。
        isStale=over_threshold,
        lastSeenAt=resource.last_seen_at.isoformat(),
        isPlaceholder=resource.is_placeholder,
        attributes=resource.attributes or {},
        labels=resource.labels or {},
    )


__all__ = ["MAX_DEPTH", "MAX_TOPOLOGY_EDGES", "MAX_TOPOLOGY_NODES", "router"]
