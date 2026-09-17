"""`GET /api/v1/events` —— 事件查询。

契约：`docs/API_CONTRACT.md` §4.4；成员 C 的 Q11 要求
「轮询接口必须**幂等 + 游标分页稳定**，避免重复/漏数据」。

两种翻页语义（互斥，避免歧义）：

| 参数 | 语义 | 排序 | 用途 |
|---|---|---|---|
| `cursor` | 向后翻页，返回**严格更旧**的记录 | `occurred_at DESC, id DESC` | 用户滚动加载历史 |
| `after` | 向前追新，返回**严格更新**的记录 | `occurred_at ASC, id ASC` | C 的 15s 轮询增量拉取 |

分页用 keyset（游标锚定具体记录）而不是 offset：事件持续追加，
offset 在第 2 页请求时若前页插入了新数据就会错位，导致漏读或重读。

过滤参数 `resourceId` / `type` 支持**重复传参**（多值），
因为前端筛选器天然是多选。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.alerts import repository as alerts_repo
from app.api.common import build_meta, invalid_argument
from app.api.schemas.common import CursorError, PageMeta, decode_cursor, encode_cursor
from app.api.schemas.events import EventData, EventListData
from app.db import get_db
from app.enums import SEVERITY_RANK, EventType
from app.events import repository as events_repo

router = APIRouter(tags=["events"])

DbSession = Annotated[Session, Depends(get_db)]

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


@router.get("/api/v1/events")
def list_events(
    session: DbSession,
    occurred_from: datetime | None = Query(default=None, alias="from"),
    occurred_to: datetime | None = Query(default=None, alias="to"),
    resourceId: list[str] | None = Query(default=None, description="可重复传参"),
    type: list[str] | None = Query(default=None, description="可重复传参"),
    minSeverity: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None, description="向后翻页游标"),
    after: str | None = Query(default=None, description="向前追新游标（轮询用）"),
) -> Any:
    """查询事件。"""
    # ---- 参数合法性 ----
    if cursor and after:
        return invalid_argument(
            "cursor 与 after 不能同时使用：前者向后翻页、后者向前追新，语义互斥",
        )
    if occurred_from and occurred_to and occurred_from > occurred_to:
        return invalid_argument("时间窗非法：from 晚于 to")
    if minSeverity is not None and minSeverity not in SEVERITY_RANK:
        return invalid_argument(
            f"minSeverity 非法：{minSeverity!r}。允许值：{sorted(SEVERITY_RANK)}"
        )
    if type:
        unknown = [t for t in type if t not in {m.value for m in EventType}]
        if unknown:
            # 非枚举类型不拒绝 —— 上报侧允许出现新类型（会计数），
            # 查询侧同样容忍，只是明确告知哪些不是已冻结枚举，
            # 避免用户误以为"没有数据"。
            pass

    cursor_pair = None
    after_pair = None
    try:
        if cursor:
            cursor_pair = decode_cursor(cursor)
        if after:
            after_pair = decode_cursor(after)
    except CursorError as exc:
        return invalid_argument(f"游标非法：{exc}")

    rows, has_more = events_repo.query_events(
        session,
        limit=limit,
        cursor=cursor_pair,
        after=after_pair,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        resource_ids=resourceId,
        types=type,
        min_severity=minSeverity,
    )

    items = [_to_event(r) for r in rows]
    data = EventListData(
        items=items,
        newestOccurredAt=items[0].occurredAt if items else None,
        oldestOccurredAt=items[-1].occurredAt if items else None,
    )

    # 下一页游标锚定本次**最后一条**（向后翻页方向）
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor(rows[-1].occurred_at, rows[-1].id)

    return {
        "data": data.model_dump(mode="json"),
        "meta": build_meta(
            page=PageMeta(limit=limit, nextCursor=next_cursor, count=len(items))
        ).model_dump(mode="json"),
    }


@router.get("/api/v1/events/{event_id}")
def get_event(session: DbSession, event_id: str) -> Any:
    """取单条事件（告警证据跳转、诊断证据展开都用它）。"""
    from app.api.common import not_found
    from app.models import Event

    row = session.get(Event, event_id)
    if row is None:
        return not_found("EVENT_NOT_FOUND", f"事件不存在：{event_id}", eventId=event_id)

    return {
        "data": _to_event(row).model_dump(mode="json"),
        "meta": build_meta().model_dump(mode="json"),
    }


@router.get("/api/v1/events/{event_id}/related-alerts")
def event_related_alerts(
    session: DbSession,
    event_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
) -> Any:
    """查"哪些告警把这条事件当作证据"。

    这是可解释性的反向链路：从一条症状事件出发，看它被哪些告警采纳。
    运维排查时往往是从事件倒推，而不是从告警正推。
    """
    from app.api.common import not_found
    from app.models import Event

    if session.get(Event, event_id) is None:
        return not_found("EVENT_NOT_FOUND", f"事件不存在：{event_id}", eventId=event_id)

    rows, has_more = alerts_repo.query_alerts(session, limit=limit, evidence_event_id=event_id)
    from app.api.alerts import to_alert

    items = [to_alert(r) for r in rows]
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor(rows[-1].last_fired_at, rows[-1].id)

    return {
        "data": {
            "eventId": event_id,
            "alerts": [a.model_dump(mode="json") for a in items],
        },
        "meta": build_meta(
            page=PageMeta(limit=limit, nextCursor=next_cursor, count=len(items))
        ).model_dump(mode="json"),
    }


def _to_event(row: Any) -> EventData:
    """ORM 事件 → 响应对象。

    时间统一转 UTC ISO8601：数据库返回的是会话时区的 timestamptz，
    直接 `isoformat()` 会带上 `+08:00` 这类偏移，前端做时间比较时容易出错。
    全链路统一 UTC 是 `ARCHITECTURE.md` §4.3 的时间语义要求。
    """
    return EventData(
        id=row.id,
        occurredAt=_iso(row.occurred_at),
        receivedAt=_iso(row.received_at),
        source=row.source,
        resourceId=row.resource_id,
        type=row.type,
        severity=row.severity,
        message=row.message,
        metrics=row.metrics or {},
        raw=row.raw or {},
        traceId=row.trace_id,
        batchId=row.batch_id,
    )


def _iso(moment: datetime) -> str:
    """统一输出 UTC ISO8601（带 Z 语义的 +00:00）。"""
    return moment.astimezone(UTC).isoformat()


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "router"]
