"""事件存储的数据访问层（`app.events`，L2）。

职责边界（`docs/backend/BACKEND_DESIGN.md` §2.5）：

  - 负责：**只追加**写入、按资源/时间/类型检索、游标分页
  - **不负责**：不修改既有事件；不承担告警状态机

**只追加**是本模块的核心约束：事件是诊断证据的来源，若可被改写，
证据链就失去意义。因此本模块**不提供任何 update / delete 接口**。

分页设计（`docs/API_CONTRACT.md` §2.2、成员 C 的 Q11）：

C 要求"轮询接口必须幂等 + 游标分页稳定，避免重复/漏数据"。这里的做法是
**keyset 分页**而不是 offset：

  - 向后翻页：`ORDER BY occurred_at DESC, id DESC` + `cursor`（严格更旧）
  - 向前追新：`ORDER BY occurred_at ASC, id ASC` + `after`（严格更新）

两条规则合起来保证：

1. 并发追加不会导致漏读或重读（keyset 锚定在具体记录上，offset 会错位）
2. `(occurred_at DESC, id DESC)` 的排序与索引 `ix_event_occurred_id` 等价，
   PostgreSQL 可以直接走索引，不需要额外排序
3. `id` 是 `evt_` + ULID（时间单调有序），作为次级键能给出**全序** ——
   同一毫秒内的事件顺序也确定，否则分页边界处会漏记录
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.enums import SEVERITY_RANK
from app.models import Event


def _apply_filters(
    stmt: Select,
    *,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
    resource_ids: Sequence[str] | None,
    types: Sequence[str] | None,
    min_severity: str | None,
) -> Select:
    """施加过滤条件。

    `minSeverity` 用**白名单展开**而不是比较字符串：严重级别的"大小"是
    业务定义（`SEVERITY_RANK`），字符串序恰好与语义序一致**只是巧合**，
    依赖巧合的代码在新增级别时会静默出错。
    """
    if occurred_from is not None:
        stmt = stmt.where(Event.occurred_at >= occurred_from)
    if occurred_to is not None:
        stmt = stmt.where(Event.occurred_at <= occurred_to)
    if resource_ids:
        stmt = stmt.where(Event.resource_id.in_(list(resource_ids)))
    if types:
        stmt = stmt.where(Event.type.in_(list(types)))
    if min_severity:
        threshold = SEVERITY_RANK.get(min_severity)
        if threshold is None:
            # 未知级别 → 用不可能命中的条件，让调用方拿到空集而不是全量
            return stmt.where(False)
        allowed = [k for k, v in SEVERITY_RANK.items() if v >= threshold]
        stmt = stmt.where(Event.severity.in_(allowed))
    return stmt


def query_events(
    session: Session,
    *,
    limit: int,
    cursor: tuple[datetime, str] | None = None,
    after: tuple[datetime, str] | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    resource_ids: Sequence[str] | None = None,
    types: Sequence[str] | None = None,
    min_severity: str | None = None,
) -> tuple[list[Event], bool]:
    """查询事件。

    返回 `(事件列表, 是否还有下一页)`。

    - 传 `cursor`：向后翻页，返回**严格更旧**的记录，按时间**降序**
    - 传 `after`：向前追新（轮询用），返回**严格更新**的记录，按时间**升序**
    - 两者都传：以 `cursor` 为准（向后翻页语义更明确；混用属调用方错误，
      在 API 层会被拒绝）

    `cursor` 与 `after` 互斥，是为了避免"既想往回翻又想追新"的歧义语义。
    """
    # 多取一条用于判断 has_more，避免额外一次 count 查询
    fetch_limit = limit + 1

    stmt = select(Event)
    stmt = _apply_filters(
        stmt,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        resource_ids=resource_ids,
        types=types,
        min_severity=min_severity,
    )

    forward = after is not None and cursor is None

    if forward:
        assert after is not None
        ts, item_id = after
        stmt = stmt.where(
            or_(
                Event.occurred_at > ts,
                and_(Event.occurred_at == ts, Event.id > item_id),
            )
        )
        stmt = stmt.order_by(Event.occurred_at.asc(), Event.id.asc())
    else:
        if cursor is not None:
            ts, item_id = cursor
            stmt = stmt.where(
                or_(
                    Event.occurred_at < ts,
                    and_(Event.occurred_at == ts, Event.id < item_id),
                )
            )
        stmt = stmt.order_by(Event.occurred_at.desc(), Event.id.desc())

    rows = list(session.execute(stmt.limit(fetch_limit)).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]

    if forward:
        # 追新时内部用升序取"最旧的 N 条新记录"，直接返回即为时间升序，
        # 正合前端顺序处理的需要。
        pass

    return rows, has_more


def count_events(
    session: Session,
    *,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    resource_ids: Sequence[str] | None = None,
    types: Sequence[str] | None = None,
    min_severity: str | None = None,
) -> int:
    """统计符合条件的事件数（用于告警/诊断的计数与前端角标）。"""
    stmt = select(func.count()).select_from(Event)
    stmt = _apply_filters(
        stmt,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        resource_ids=resource_ids,
        types=types,
        min_severity=min_severity,
    )
    return int(session.execute(stmt).scalar() or 0)


def get_events_by_ids(session: Session, event_ids: Iterable[str]) -> list[Event]:
    """按 id 批量取事件（供告警证据展开）。

    保持输入顺序：诊断/告警详情里证据的展示顺序对运维理解因果链很重要。
    """
    ids = list(event_ids)
    if not ids:
        return []
    rows = list(session.execute(select(Event).where(Event.id.in_(ids))).scalars())
    by_id = {e.id: e for e in rows}
    return [by_id[i] for i in ids if i in by_id]


def latest_event_at(session: Session) -> datetime | None:
    """最近一条事件的发生时间（供健康检查与前端"数据截止"提示）。"""
    return session.execute(select(func.max(Event.occurred_at))).scalar()


__all__ = [
    "count_events",
    "get_events_by_ids",
    "latest_event_at",
    "query_events",
]
