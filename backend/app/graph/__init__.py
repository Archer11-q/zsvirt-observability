"""资源图（L3）。

存储与查询见 `app.graph.repository`；纯遍历算法见 `app.graph.algorithms`。

职责边界（docs/backend/BACKEND_DESIGN.md §2.4）：
  - 负责：节点与边的增删改、parentId 关系维护、上下游检索、
          observability 生命周期、影响范围传播
  - 不负责：不自行编造资源关系；不物理删除节点；不修改 status
            （status 是来源系统的字段，只由 ingest / zsvirt 适配层写入）
"""

from app.graph.algorithms import (
    PARENT_CHILD_ALLOWED,
    RESOURCE_KINDS,
    Edge,
    GraphError,
    ImpactScope,
    ancestors,
    build_child_index,
    build_parent_index,
    chain_intermediates,
    clean_parent_chain,
    correlation_path,
    descendants,
    impact_scope,
    neighbors,
    validate_edge,
)

__all__ = [
    "PARENT_CHILD_ALLOWED",
    "RESOURCE_KINDS",
    "Edge",
    "GraphError",
    "ImpactScope",
    "ancestors",
    "build_child_index",
    "build_parent_index",
    "chain_intermediates",
    "clean_parent_chain",
    "correlation_path",
    "descendants",
    "impact_scope",
    "neighbors",
    "validate_edge",
]
