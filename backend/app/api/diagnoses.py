"""诊断接口（`docs/API_CONTRACT.md` §4.6、§4.7）。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/diagnoses` | 诊断列表（C 的要求：否则前端无法发现"有哪些诊断"） |
| `GET` | `/api/v1/diagnosis/{id}` | 单条诊断详情 |
| `POST` | `/api/v1/diagnoses` | 手动触发一次诊断（演示的「一键诊断」） |

**模块名用复数 `diagnoses`**：`app.diagnosis` 是**诊断领域包**（引擎 + 服务），
本模块是它的 HTTP 门面，两者同名会让 `from app import diagnosis` 的意图
变得含混。复数名也正好对应本模块的主要端点 `/diagnoses`。

**`POST` 默认同步返回**：诊断是规则匹配 + 定长图遍历，亚秒级完成。
契约里预留的 `202 + pending` 分支**不实现** —— 没有真异步执行器却返回
"已受理"，会让前端拿到一个永远查不到的 `diagnosisId`，比慢一点更糟。
若将来诊断变重，再按契约补上真异步（记入 `DECISIONS.md`）。

**证据不可伪造**：`POST` 的响应只回引擎实际收集到的证据。规则集尚未配置时
（`DEVELOPMENT_PLAN.md` 任务 2），结论是 `UNKNOWN` + 真实证据 ——
这比返回一个编造的根因更有用，也符合 `DATA_MODEL.md` §5.3 的红线。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import build_meta, invalid_argument, not_found
from app.api.schemas.common import CursorError, PageMeta, decode_cursor, encode_cursor
from app.api.schemas.diagnosis import (
    DiagnosisData,
    DiagnosisEvidence,
    DiagnosisListData,
    DiagnosisRecommendation,
    TriggerDiagnosisRequest,
)
from app.api.schemas.events import EventData
from app.db import get_db
from app.diagnosis import service as diagnosis_service
from app.models import Diagnosis as DiagnosisRow
from app.models import Event

router = APIRouter(tags=["diagnosis"])

DbSession = Annotated[Session, Depends(get_db)]

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


@router.get("/api/v1/diagnoses")
def list_diagnoses(
    session: DbSession,
    rootCause: list[str] | None = Query(default=None, description="可重复传参"),
    resourceId: str | None = Query(default=None, description="相关资源（三集任一）"),
    created_from: datetime | None = Query(default=None, alias="from"),
    created_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None, description="向后翻页游标"),
) -> Any:
    """诊断列表，按 `createdAt` 降序。

    之所以需要这个端点（C 的 Q13）：只有详情接口时，前端无法枚举诊断，
    也无从发现"没有关联告警的诊断" —— 而那恰恰是自动诊断产出的主体。
    """
    if created_from and created_to and created_from > created_to:
        return invalid_argument("时间窗非法：from 晚于 to")

    cursor_pair = None
    try:
        if cursor:
            cursor_pair = decode_cursor(cursor)
    except CursorError as exc:
        return invalid_argument(f"游标非法：{exc}")

    rows, has_more = diagnosis_service.query_diagnoses(
        session,
        limit=limit,
        cursor=cursor_pair,
        root_causes=rootCause,
        created_from=created_from,
        created_to=created_to,
        resource_id=resourceId,
    )

    items = [to_diagnosis(r) for r in rows]
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id)

    data = DiagnosisListData(
        items=items,
        rootCauseCounts=diagnosis_service.root_cause_counts(session, created_from=created_from),
    )

    return {
        "data": data.model_dump(mode="json"),
        "meta": build_meta(
            page=PageMeta(limit=limit, nextCursor=next_cursor, count=len(items))
        ).model_dump(mode="json"),
    }


@router.get("/api/v1/diagnosis/{diagnosis_id}")
def get_diagnosis(
    session: DbSession,
    diagnosis_id: str,
    includeEvidence: bool = Query(
        default=False, description="是否内联证据指向的事件原文（默认不内联）"
    ),
) -> Any:
    """单条诊断详情。

    `includeEvidence=true` 时把 `evidence[].eventIds` 指向的事件原文一并返回。
    默认关闭的原因：一次诊断的证据可能指向几十条事件，而列表页逐条打开时
    并不需要原文；只有详情页的"证据展开"才需要。
    """
    row = session.get(DiagnosisRow, diagnosis_id)
    if row is None:
        return not_found(
            "DIAGNOSIS_NOT_FOUND", f"诊断不存在：{diagnosis_id}", diagnosisId=diagnosis_id
        )

    events = _evidence_events(session, row) if includeEvidence else []
    payload = to_diagnosis(row)
    payload.evidenceEvents = events

    return {
        "data": payload.model_dump(mode="json"),
        "meta": build_meta().model_dump(mode="json"),
    }


def _evidence_events(session: Session, row: DiagnosisRow) -> list[EventData]:
    """取出证据指向的事件（**一次查询**，逐条 `session.get` 会变成 N+1）。

    找不到的事件**静默跳过**：事件只追加不删除，理论上不会丢；但若历史诊断
    引用了被人工清理过的事件，这里应当少返回一条，而不是让整个详情接口 500。
    """
    event_ids = sorted({eid for ev in (row.evidence or []) for eid in (ev.get("eventIds") or [])})
    if not event_ids:
        return []

    rows = session.execute(select(Event).where(Event.id.in_(event_ids))).scalars()
    found = {e.id: e for e in rows}
    return [_to_event_data(found[eid]) for eid in event_ids if eid in found]


def _to_event_data(event: Event) -> EventData:
    """ORM 事件 → 响应对象（与 `api/events.py` 同一口径，时间统一 UTC）。"""
    return EventData(
        id=event.id,
        occurredAt=event.occurred_at.astimezone(UTC).isoformat(),
        receivedAt=event.received_at.astimezone(UTC).isoformat(),
        source=event.source,
        resourceId=event.resource_id,
        type=event.type,
        severity=event.severity,
        message=event.message,
        metrics=event.metrics or {},
        raw=event.raw or {},
        traceId=event.trace_id,
        batchId=event.batch_id,
    )


@router.post("/api/v1/diagnoses")
def trigger_diagnosis(session: DbSession, body: TriggerDiagnosisRequest) -> Any:
    """手动触发一次诊断。

    两种触发方式（契约 §4.6）：
      - `{"alertId": "..."}` —— 从告警出发
      - `{"anchorResourceId": "...", "window": {"from":..., "to":...}}` —— 从资源出发

    触发失败**不落库**：参数非法（400）或锚点不存在（404）时没有任何结论
    产生，写入一条空诊断只会在列表里制造噪声。
    """
    resolved = diagnosis_service.resolve_trigger(
        session,
        alert_id=body.alertId,
        anchor_resource_id=body.anchorResourceId,
        window=body.window,
    )
    if isinstance(resolved, str):
        # 消息里含"不存在"的视为 404，其余是参数问题 400 —— 让前端能区分
        # "你传错了"和"这个对象真的没有"。
        if "不存在" in resolved:
            return not_found("TRIGGER_TARGET_NOT_FOUND", resolved)
        return invalid_argument(resolved)

    anchor_resource_id, window, trigger = resolved

    row, duration_ms = diagnosis_service.run_diagnosis(
        session,
        anchor_resource_id=anchor_resource_id,
        window=window,
        trigger=trigger,
    )
    session.commit()

    linked = False
    if body.linkToAlert and body.alertId:
        linked = diagnosis_service.link_to_alert(session, body.alertId, row.id)
        if linked:
            session.commit()

    payload = to_diagnosis(row, duration_ms=duration_ms).model_dump(mode="json")
    payload["linkedToAlert"] = linked

    return {"data": payload, "meta": build_meta().model_dump(mode="json")}


def to_diagnosis(row: DiagnosisRow, *, duration_ms: int | None = None) -> DiagnosisData:
    """ORM 诊断 → 响应对象。

    时间统一 UTC ISO8601（`ARCHITECTURE.md` §4.3）：数据库返回的是会话时区的
    `timestamptz`，直接 `isoformat()` 会带上 `+08:00` 偏移，前端做时间比较
    时会算错。
    """
    return DiagnosisData(
        id=row.id,
        createdAt=row.created_at.astimezone(UTC).isoformat(),
        trigger=row.trigger or {},
        rootCause=row.root_cause,
        confidence=row.confidence,
        confidenceBreakdown=row.confidence_breakdown or [],
        affectedResources=list(row.affected_resources or []),
        potentiallyAffected=list(row.potentially_affected or []),
        onChain=list(row.on_chain or []),
        evidence=[_to_evidence(e) for e in (row.evidence or [])],
        recommendation=[
            DiagnosisRecommendation(code=r["code"], text=r["text"])
            for r in (row.recommendation or [])
        ],
        ruleSetVersion=row.rule_set_version,
        notes=list(row.notes or []),
        durationMs=duration_ms,
    )


def _to_evidence(raw: dict[str, Any]) -> DiagnosisEvidence:
    """证据字典 → 响应对象。缺字段一律给安全默认值。

    用 `.get` 而不是直接下标：证据是**历史落库数据**，字段可能来自早期版本。
    列表接口因为一条旧数据 500 掉，会让整段历史不可见。
    """
    return DiagnosisEvidence(
        type=raw.get("type", "event"),
        name=raw.get("name", ""),
        resourceId=raw.get("resourceId"),
        value=raw.get("value"),
        count=raw.get("count"),
        at=raw.get("at"),
        eventIds=list(raw.get("eventIds") or []),
        source=raw.get("source"),
    )


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "router", "to_diagnosis"]
