"""拓扑查询的响应模型（`docs/API_CONTRACT.md` §4.2）。

**节点与边分离**（而不是嵌套树）：前端拓扑渲染需要扁平的
`nodes + edges`（ECharts graph 就是这个形态），嵌套树还要前端再拍平。

`truncated` 是契约要求：裁剪时必须显式告知，前端据此提示"结果被裁剪，
请下钻"，而不是静默显示不完整拓扑（C 的 Q12 明确要求）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TopologyNode(BaseModel):
    """拓扑节点。

    注意 `status` 与 `observability` 是**两个独立字段**（D-032）：
    前者是业务状态（来源系统给出），后者是 B 的观测状态。
    前端据此分别渲染"运行状态"与"数据新鲜度/是否陈旧"。
    """

    id: str
    kind: str
    name: str | None = None
    parentId: str | None = None

    #: 业务状态：running | stopped | error | unknown
    status: str
    #: B 的观测状态：active | stale | gone
    observability: str
    #: 距上次观测的时长（如 "10m"），供前端提示"数据陈旧"
    staleness: str | None = None

    lastSeenAt: str
    #: 占位节点标记（事件引用未上报资源时创建）
    isPlaceholder: bool = False
    attributes: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, Any] = Field(default_factory=dict)


class TopologyEdge(BaseModel):
    parentId: str
    childId: str
    relation: str = "hosts"


class TopologyData(BaseModel):
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]
    #: 是否因上限被裁剪（前端必须提示，不能静默显示不完整拓扑）
    truncated: bool = False
    #: 裁剪说明，便于前端给出可操作的提示（例如"请下钻到某个根节点"）
    truncationReason: str | None = None
    #: 被丢弃的节点数，用于前端展示"还有多少未显示"
    droppedNodes: int = 0
