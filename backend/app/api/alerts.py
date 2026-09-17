"""告警查询与运营端点。

契约：`docs/API_CONTRACT.md` §4.5；成员 C 的 Q11（轮询 + 游标分页稳定）。

端点：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/alerts` | 列表（含状态与严重级别计数） |
| GET | `/api/v1/alerts/{id}` | 详情 |
| GET | `/api/v1/alerts/{id}/evidence` | **展开证据事件** —— 可解释性的入口 |
| POST | `/api/v1/alerts/{id}/actions` | 单条状态操作 |
| POST | `/api/v1/alerts/actions` | 批量状态操作 |

**为什么单独提供 `/evidence`**：`DATA_MODEL.md` §5.2 的红线是"没有证据的
告警不允许存在"。前端要能一键看到触发告警的那几条事件，否则"可解释"
只是文档里的承诺。C 也可以用它做告警详情页的"证据"区块。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.alerts import repository as alerts_repo
from app.api.common import build_meta, error_response, invalid_argument, not_found
from app.api.schemas.alerts import (
    AlertActionData,
    AlertActionRequest,
    AlertData,
    AlertListData,
    BulkAlertActionData,
)
from app.api.schemas.common import CursorError, PageMeta, decode_cursor, encode_cursor
from app.db import get_db
from app.enums import SEVERITY_RANK, AlertState
from app.events import repository as events_repo

router = APIRouter(tags=["alerts"])

DbSession = Annotated[Session, Depends(get_db)]

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

VALID_ACTIONS = {"ack", "resolve", "silence", "unsilence"}


@router.get("/api/v1/alerts")
def list_alerts(
    session: DbSession,
    state: list[str] | None = Query(default=None, description="可重复传参"),
    minSeverity: str | None = Query(default=None),
    resourceId: list[str] | None = Query(default=None),
    ruleId: list[str] | None = Query(default=None),
    fired_from: datetime | None = Query(default=None, alias="from"),
    fired_to: datetime | None = Query(default=None, alias="to"),
    evidenceEventId: str | None = Query(default=None, description="按证据事件反查告警"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
) -> Any:
    """查询告警，并附带状态/严重级别计数。"""
    if state:
        invalid = [s for s in state if s not in {m.value for m in AlertState}]
        if invalid:
            return invalid_argument(
                f"state 非法：{invalid}。允许值：{sorted(m.value for m in AlertState)}"
            )
    if minSeverity is not None and minSeverity not in SEVERITY_RANK:
        return invalid_argument(
            f"minSeverity 非法：{minSeverity!r}。允许值：{sorted(SEVERITY_RANK)}"
        )
    if fired_from and fired_to and fired_from > fired_to:
        return invalid_argument("时间窗非法：from 晚于 to")

    cursor_pair = None
    try:
        if cursor:
            cursor_pair = decode_cursor(cursor)
    except CursorError as exc:
        return invalid_argument(f"游标非法：{exc}")

    rows, has_more = alerts_repo.query_alerts(
        session,
        limit=limit,
        cursor=cursor_pair,
        states=state,
        min_severity=minSeverity,
        resource_ids=resourceId,
        rule_ids=ruleId,
        fired_from=fired_from,
        fired_to=fired_to,
        evidence_event_id=evidenceEventId,
    )

    items = [to_alert(r) for r in rows]
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor(rows[-1].last_fired_at, rows[-1].id)

    data = AlertListData(
        items=items,
        # 计数是**全量**的（不受本次筛选影响）—— 它们是顶部状态条的数据源，
        # 若跟着筛选变化，前端就永远看不到"总共还有多少条在报"。
        stateCounts=alerts_repo.alert_state_counts(session),
        activeSeverityCounts=alerts_repo.active_severity_counts(session),
    )

    return {
        "data": data.model_dump(mode="json"),
        "meta": build_meta(
            page=PageMeta(limit=limit, nextCursor=next_cursor, count=len(items))
        ).model_dump(mode="json"),
    }


@router.get("/api/v1/alerts/{alert_id}")
def get_alert(session: DbSession, alert_id: str) -> Any:
    """告警详情。"""
    row = alerts_repo.get_alert(session, alert_id)
    if row is None:
        return not_found("ALERT_NOT_FOUND", f"告警不存在：{alert_id}", alertId=alert_id)
    return {
        "data": to_alert(row).model_dump(mode="json"),
        "meta": build_meta().model_dump(mode="json"),
    }


@router.get("/api/v1/alerts/{alert_id}/evidence")
def get_alert_evidence(session: DbSession, alert_id: str) -> Any:
    """展开告警的证据事件。

    `evidenceEventIds` 里可能引用了已被清理的事件（事件表有保留策略时），
    因此**必须报告缺失的 id**，而不是静默少返回几条 ——
    证据缺一条，"可解释"就打了折扣，运维需要知道。
    """
    row = alerts_repo.get_alert(session, alert_id)
    if row is None:
        return not_found("ALERT_NOT_FOUND", f"告警不存在：{alert_id}", alertId=alert_id)

    from app.api.events import _to_event

    events = events_repo.get_events_by_ids(session, row.evidence_event_ids)
    found_ids = {e.id for e in events}
    missing = [eid for eid in row.evidence_event_ids if eid not in found_ids]

    return {
        "data": {
            "alertId": alert_id,
            "evidenceCount": len(row.evidence_event_ids),
            "events": [_to_event(e).model_dump(mode="json") for e in events],
            "missingEventIds": missing,
        },
        "meta": build_meta().model_dump(mode="json"),
    }


@router.post("/api/v1/alerts/{alert_id}/actions")
def act_on_alert(
    session: DbSession,
    alert_id: str,
    request: AlertActionRequest,
) -> Any:
    """单条告警状态操作（ack / resolve / silence / unsilence）。"""
    if request.action not in VALID_ACTIONS:
        return invalid_argument(f"action 非法：{request.action!r}。允许值：{sorted(VALID_ACTIONS)}")

    row = alerts_repo.get_alert(session, alert_id)
    if row is None:
        return not_found("ALERT_NOT_FOUND", f"告警不存在：{alert_id}", alertId=alert_id)

    try:
        updated, changed, message = alerts_repo.transition(
            session,
            row,
            action=request.action,
            silence_seconds=request.silenceSeconds,
        )
    except alerts_repo.IllegalTransition as exc:
        session.rollback()
        # 非法迁移是**冲突**而不是参数错误：请求本身合法，是当前状态不允许
        return error_response(
            409,
            "CONFLICT",
            str(exc),
            details={"currentState": exc.current, "targetState": exc.target},
        )

    session.commit()
    return {
        "data": AlertActionData(
            alert=to_alert(updated), changed=changed, message=message
        ).model_dump(mode="json"),
        "meta": build_meta().model_dump(mode="json"),
    }


@router.post("/api/v1/alerts/actions")
def bulk_act_on_alerts(
    session: DbSession,
    request: AlertActionRequest,
    ids: list[str] | None = Query(default=None, description="告警 id，可重复传参"),
) -> Any:
    """批量状态操作。

    一次事故往往同时产生多条告警（例如同宿主多个 VM 受影响），
    逐条点击不现实。**逐条报告结果**：运维需要知道哪条失败、为什么。
    """
    if request.action not in VALID_ACTIONS:
        return invalid_argument(f"action 非法：{request.action!r}。允许值：{sorted(VALID_ACTIONS)}")
    # 缺失与空数组都走 400 而不是让 FastAPI 先返回 422：
    # 两者对调用方是同一类错误（没给要操作的告警），错误信息也更直白。
    if not ids:
        return invalid_argument("ids 不能为空：批量操作必须指定至少一个告警 id")

    summary = alerts_repo.bulk_transition(
        session,
        ids,
        action=request.action,
        silence_seconds=request.silenceSeconds,
    )
    session.commit()

    return {
        "data": BulkAlertActionData.model_validate(summary).model_dump(mode="json"),
        "meta": build_meta().model_dump(mode="json"),
    }


def to_alert(row: Any) -> AlertData:
    """ORM 告警 → 响应对象（时间统一 UTC）。"""
    return AlertData(
        id=row.id,
        ruleId=row.rule_id,
        resourceId=row.resource_id,
        severity=row.severity,
        state=row.state,
        firstFiredAt=row.first_fired_at.astimezone(UTC).isoformat(),
        lastFiredAt=row.last_fired_at.astimezone(UTC).isoformat(),
        resolvedAt=row.resolved_at.astimezone(UTC).isoformat() if row.resolved_at else None,
        count=row.count,
        aggregationKey=row.aggregation_key,
        evidenceEventIds=list(row.evidence_event_ids or []),
        diagnosisId=row.diagnosis_id,
        title=row.title,
        labels=row.labels or {},
    )


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "VALID_ACTIONS", "router", "to_alert"]
