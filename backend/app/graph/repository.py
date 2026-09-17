"""资源图的数据访问层。

设计原则（`docs/backend/BACKEND_DESIGN.md` §2.4）：

  - 本层只做读写与事务，**不自行编造资源关系**
  - **不物理删除节点** —— 只把 `observability` 标为 `gone`（`DATA_MODEL.md` §7）
  - **不修改 `status`** —— 那是来源系统的字段，只由 ingest / zsvirt 适配层写入

`resource.parent_id` 与 `resource_edge` 是**冗余但有意为之**：
前者便于单点查父，后者便于整图遍历（避免 N+1）。两者必须同步维护，
因此所有写操作都经过本模块，不散落到调用方。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.enums import Observability, ResourceKind, ResourceStatus
from app.graph.algorithms import Edge, GraphError, build_child_index, build_parent_index
from app.models import Resource, ResourceEdge


def placeholder_resource_id(kind: str, source: str, source_id: str) -> str:
    """占位资源 ID。

    当事件引用了尚未上报的资源时（成员 A 的 Q1 允许极端竞态出现孤儿引用），
    B 把事件挂到占位节点，而不是丢数据或报错。
    """
    return f"{ResourceKind.UNRESOLVED.value}:{source}:{source_id}"


def upsert_resource(
    session: Session,
    *,
    resource_id: str,
    kind: str,
    name: str | None = None,
    parent_id: str | None = None,
    status: str | None = None,
    attributes: dict[str, Any] | None = None,
    labels: dict[str, Any] | None = None,
    seen_at: datetime | None = None,
    is_placeholder: bool = False,
) -> Resource:
    """插入或更新资源。

    **关键：冲突时不覆盖 `status`**（除非显式传入）。

    理由：`status` 由来源系统拥有。若 B 在每次 upsert 时都用默认值覆盖它，
    那么「ZSvirt 报 running → 探针 upsert 把它写成 unknown」这类
    静默数据损坏就会发生。因此：

    - 首次插入：用传入的 `status`，未给则 `unknown`
    - 后续更新：**只更新 B 自己拥有的字段**（`last_seen_at` / `observability` /
      可选的 `name`/`attributes`/`labels`/`parent_id`），`status` 仅在显式传入时更新

    同理 `first_seen_at` 只在插入时写入，更新时不覆盖（否则"首次观测时间"
    会被不断推后，失去意义）。
    """
    now = seen_at or datetime.now(UTC)

    values: dict[str, Any] = {
        "id": resource_id,
        "kind": kind,
        "name": name,
        "parent_id": parent_id,
        "status": status or ResourceStatus.UNKNOWN.value,
        "observability": Observability.ACTIVE.value,
        "first_seen_at": now,
        "last_seen_at": now,
        "attributes": attributes or {},
        "labels": labels or {},
        "is_placeholder": is_placeholder,
    }

    stmt = pg_insert(Resource).values(**values)

    # 更新集刻意不含 first_seen_at（保持首次观测时间不变）
    # 与 status（除非调用方显式传入）
    update_set: dict[str, Any] = {
        "last_seen_at": now,
        # 重新观测到资源 → 观测状态回到 active（可能此前因超时被标 stale）
        "observability": Observability.ACTIVE.value,
    }
    if name is not None:
        update_set["name"] = name
    if parent_id is not None:
        update_set["parent_id"] = parent_id
    if attributes:
        update_set["attributes"] = attributes
    if labels:
        update_set["labels"] = labels
    if status is not None:
        # 仅在来源系统明确给出状态时才更新
        update_set["status"] = status
    if is_placeholder:
        update_set["is_placeholder"] = True

    stmt = stmt.on_conflict_do_update(index_elements=[Resource.id], set_=update_set)
    session.execute(stmt)
    session.flush()

    # 必须让 identity map 中的旧对象失效再读回。
    #
    # `session.execute(pg_insert(...))` 属于 Core 执行路径：SQLAlchemy 不知道
    # 它改动了 ORM 对象，因此 identity map 里仍缓存着**上一次读到的旧值**。
    # 不失效的话，`session.get()` 会直接返回旧对象，
    # 表现为"last_seen_at 没有推进"这类静默错误（本模块单测抓到过）。
    session.expire_all()
    resource = session.get(Resource, resource_id)
    assert resource is not None  # 刚 upsert，必然存在
    return resource


def set_status(session: Session, resource_id: str, status: str) -> Resource | None:
    """显式设置**业务状态**（只应由来源系统的适配层调用）。

    与 `upsert_resource` 分开，是为了让"谁有权改 status"在代码上可审计 ——
    `status` 是来源系统的字段，B 不得推测（D-032）。
    """
    resource = session.get(Resource, resource_id)
    if resource is None:
        return None
    resource.status = status
    session.flush()
    return resource


def ensure_edge(session: Session, parent_id: str, child_id: str, relation: str = "hosts") -> None:
    """建立父子关系（幂等），并同步 `child.parent_id`。

    自环与缺失端点被拒绝：自环是脏数据，缺失端点说明调用顺序错误。
    """
    if parent_id == child_id:
        raise GraphError(f"self loop rejected: {parent_id}")

    child = session.get(Resource, child_id)
    if child is None:
        raise GraphError(f"child resource not found: {child_id}")
    parent = session.get(Resource, parent_id)
    if parent is None:
        raise GraphError(f"parent resource not found: {parent_id}")

    existing = session.get(ResourceEdge, {"parent_id": parent_id, "child_id": child_id})
    if existing is None:
        session.add(ResourceEdge(parent_id=parent_id, child_id=child_id, relation=relation))
    if child.parent_id != parent_id:
        child.parent_id = parent_id
    session.flush()


def load_graph(
    session: Session,
    *,
    root_id: str | None = None,
    max_depth: int = 8,
    include_stale: bool = False,
    kinds: Sequence[str] | None = None,
) -> tuple[list[Resource], list[Edge]]:
    """加载资源图（节点 + 边）。

    从 `root_id` 开始做上下双向闭包，`max_depth` 限制层数。
    `root_id=None` 时返回全部（仍受 `max_depth` 约束）。

    **`kinds` 过滤的是返回的节点，不是遍历范围** —— 这点很重要：
    若在 SQL 层先按 kind 剪节点，从 HOST 出发就找不到 GPU（HOST 本身不是
    GPU），遍历会因为缺少中间节点而返回空集。所以顺序必须是
    「先取全图 → 遍历定位子图 → 最后按 kind 过滤返回结果」。

    返回的边已按 `app.graph.algorithms.Edge` 形态给出，可直接交给
    `impact_scope` / `descendants` 等纯函数使用 —— 避免在 repository 里
    再实现一套遍历。
    """
    # 注意：这里**不**按 kinds 过滤 —— 遍历需要完整的节点集合
    stmt = select(Resource)
    if not include_stale:
        stmt = stmt.where(Resource.observability != Observability.GONE.value)

    all_resources = list(session.execute(stmt).scalars())
    by_id = {r.id: r for r in all_resources}

    edge_stmt = select(ResourceEdge)
    if by_id:
        edge_stmt = edge_stmt.where(
            ResourceEdge.parent_id.in_(by_id.keys()),
            ResourceEdge.child_id.in_(by_id.keys()),
        )
    else:
        edge_stmt = edge_stmt.where(False)
    edges = [
        Edge(parent_id=e.parent_id, child_id=e.child_id, relation=e.relation)
        for e in session.execute(edge_stmt).scalars()
    ]

    if root_id is None:
        nodes = all_resources
        kept_edges = edges
    else:
        parent_index = build_parent_index(edges)
        child_index = build_child_index(edges)

        # 双向闭包：从 root 向上与向下各走 max_depth 层
        reachable = {root_id}
        frontier = [root_id]
        for _ in range(max_depth):
            nxt: list[str] = []
            for node in frontier:
                neighbours = list(child_index.get(node, []))
                parent = parent_index.get(node)
                if parent is not None:
                    neighbours.append(parent)
                for neighbour in neighbours:
                    if neighbour not in reachable:
                        reachable.add(neighbour)
                        nxt.append(neighbour)
            frontier = nxt
            if not frontier:
                break

        nodes = [by_id[rid] for rid in reachable if rid in by_id]
        kept = {r.id for r in nodes}
        kept_edges = [e for e in edges if e.parent_id in kept and e.child_id in kept]

    # 最后才按 kind 过滤返回的节点，并同步裁剪边
    if kinds:
        wanted = set(kinds)
        nodes = [r for r in nodes if r.kind in wanted]
        ids = {r.id for r in nodes}
        kept_edges = [e for e in kept_edges if e.parent_id in ids and e.child_id in ids]

    return nodes, kept_edges


def list_roots(session: Session, *, limit: int = 200) -> list[Resource]:
    """无父节点的资源（通常是宿主机）。"""
    child_ids = select(ResourceEdge.child_id)
    stmt = (
        select(Resource)
        .where(Resource.id.not_in(child_ids))
        .where(Resource.observability != Observability.GONE.value)
        .order_by(Resource.kind, Resource.id)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def mark_stale(session: Session, *, older_than_seconds: int, now: datetime | None = None) -> int:
    """把长时间未观测到的资源标为 `stale`。

    **不删除** —— 删除会让历史事件变成孤儿，破坏可追溯性（`DATA_MODEL.md` §7）。
    返回受影响的资源数。
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=older_than_seconds)
    stmt = (
        select(Resource)
        .where(Resource.last_seen_at < cutoff)
        .where(Resource.observability == Observability.ACTIVE.value)
        .where(Resource.observability != Observability.GONE.value)
    )
    affected = 0
    for resource in session.execute(stmt).scalars():
        resource.observability = Observability.STALE.value
        affected += 1
    session.flush()
    return affected


def mark_gone(session: Session, *, older_than_seconds: int, now: datetime | None = None) -> int:
    """把极长时间未观测到的资源标为 `gone`（仍不删除）。"""
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=older_than_seconds)
    stmt = (
        select(Resource)
        .where(Resource.last_seen_at < cutoff)
        .where(Resource.observability == Observability.STALE.value)
    )
    affected = 0
    for resource in session.execute(stmt).scalars():
        resource.observability = Observability.GONE.value
        affected += 1
    session.flush()
    return affected


def count_unresolved(session: Session) -> int:
    """占位资源数量 —— 暴露为健康指标（`/api/health` 的 unresolvedEvents）。

    这是链路完整性的可观测信号：持续增长说明探针上报顺序有问题，
    或资源被 ZSvirt 侧删除后探针仍在引用。
    """
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(Resource)
        .where(Resource.kind == ResourceKind.UNRESOLVED.value)
    )
    return int(session.execute(stmt).scalar() or 0)


def delete_all_edges_for(session: Session, resource_id: str) -> int:
    """移除某资源的全部关系边（仅用于修正错误关系，不删除节点）。"""
    result = session.execute(
        delete(ResourceEdge).where(
            (ResourceEdge.parent_id == resource_id) | (ResourceEdge.child_id == resource_id)
        )
    )
    session.flush()
    return int(result.rowcount or 0)


__all__ = [
    "count_unresolved",
    "delete_all_edges_for",
    "ensure_edge",
    "list_roots",
    "load_graph",
    "mark_gone",
    "mark_stale",
    "placeholder_resource_id",
    "set_status",
    "upsert_resource",
]
