"""告警存储与状态机（`app.alerts`，L4）。

契约：`docs/API_CONTRACT.md` §4.5；对象定义：`docs/DATA_MODEL.md` §5.2。

职责边界（`docs/backend/BACKEND_DESIGN.md` §2.6）：

  - 负责：告警查询、状态机流转、聚合去重、静默
  - **不负责**：规则求值（那是告警**引擎**，后续落地）；
    本模块不产生告警，只管理已产生的告警

**状态机是本模块的核心不变量**。允许的迁移定义在
`app.enums.ALERT_STATE_TRANSITIONS`（与 `docs/CONTRACT_FREEZE.md` F-04 一致）：

```
firing ──ack──▶ acked ──resolve──▶ resolved
   │                │                   │
   │                └───────────────────┘
   ├──silence──▶ silenced ──▶ firing/resolved
   └──resolve──▶ resolved ──复发──▶ firing
```

**重复操作不报错**：运维重复点"确认"不应弹错误。因此已处于目标状态时
返回 `changed=False` 而不是 4xx —— 这与"非法迁移"区别对待（见下）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.enums import ALERT_STATE_TRANSITIONS, SEVERITY_RANK, AlertState
from app.models import Alert


class IllegalTransition(Exception):
    """非法的状态迁移 —— 应返回 409 CONFLICT。"""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"非法状态迁移：{current} → {target}。"
            f"允许的目标：{sorted(ALERT_STATE_TRANSITIONS.get(current, set()))}"
        )


def _allowed_targets(current: str) -> set[str]:
    return set(ALERT_STATE_TRANSITIONS.get(current, set()))


def _apply_filters(
    stmt: Select,
    *,
    states: list[str] | None,
    min_severity: str | None,
    resource_ids: list[str] | None,
    rule_ids: list[str] | None,
    fired_from: datetime | None,
    fired_to: datetime | None,
    evidence_event_id: str | None,
) -> Select:
    """施加过滤条件。"""
    if states:
        stmt = stmt.where(Alert.state.in_(states))
    if min_severity:
        threshold = SEVERITY_RANK.get(min_severity)
        if threshold is None:
            return stmt.where(False)
        allowed = [k for k, v in SEVERITY_RANK.items() if v >= threshold]
        stmt = stmt.where(Alert.severity.in_(allowed))
    if resource_ids:
        stmt = stmt.where(Alert.resource_id.in_(resource_ids))
    if rule_ids:
        stmt = stmt.where(Alert.rule_id.in_(rule_ids))
    if fired_from is not None:
        stmt = stmt.where(Alert.last_fired_at >= fired_from)
    if fired_to is not None:
        stmt = stmt.where(Alert.last_fired_at <= fired_to)
    if evidence_event_id:
        # 数组包含判断：PostgreSQL 的 `@>`（ARRAY.contains）走 GIN 索引最有效。
        # 当前未建 GIN 索引（规模小），但语义上仍是最准确的表达。
        stmt = stmt.where(Alert.evidence_event_ids.contains([evidence_event_id]))
    return stmt


def query_alerts(
    session: Session,
    *,
    limit: int,
    cursor: tuple[datetime, str] | None = None,
    states: list[str] | None = None,
    min_severity: str | None = None,
    resource_ids: list[str] | None = None,
    rule_ids: list[str] | None = None,
    fired_from: datetime | None = None,
    fired_to: datetime | None = None,
    evidence_event_id: str | None = None,
) -> tuple[list[Alert], bool]:
    """查询告警。

    排序按 `(last_fired_at DESC, id DESC)` —— 运维看的是"最近还在报的"，
    而不是"最早出现的"。与事件的 keyset 分页同一套语义，保证稳定。
    """
    fetch_limit = limit + 1
    stmt = select(Alert)
    stmt = _apply_filters(
        stmt,
        states=states,
        min_severity=min_severity,
        resource_ids=resource_ids,
        rule_ids=rule_ids,
        fired_from=fired_from,
        fired_to=fired_to,
        evidence_event_id=evidence_event_id,
    )

    if cursor is not None:
        ts, item_id = cursor
        stmt = stmt.where(
            or_(
                Alert.last_fired_at < ts,
                and_(Alert.last_fired_at == ts, Alert.id < item_id),
            )
        )

    stmt = stmt.order_by(Alert.last_fired_at.desc(), Alert.id.desc())
    rows = list(session.execute(stmt.limit(fetch_limit)).scalars())
    has_more = len(rows) > limit
    return rows[:limit], has_more


def alert_state_counts(session: Session) -> dict[str, int]:
    """按状态计数（全量，不含筛选）。

    前端顶部状态条与筛选器需要，一次请求拿到比多次查询划算。
    缺失的状态补 0，避免前端做 `undefined` 判断。
    """
    rows = session.execute(select(Alert.state, func.count()).group_by(Alert.state)).all()
    counts = {m.value: 0 for m in AlertState}
    for state, count in rows:
        counts[str(state)] = int(count)
    return counts


def active_severity_counts(session: Session) -> dict[str, int]:
    """活动告警（firing / acked）的严重级别分布。

    已恢复/已静默的不算 —— 它们不该继续占用运维的注意力。
    """
    active_states = [AlertState.FIRING.value, AlertState.ACKED.value]
    rows = session.execute(
        select(Alert.severity, func.count())
        .where(Alert.state.in_(active_states))
        .group_by(Alert.severity)
    ).all()
    counts = dict.fromkeys(SEVERITY_RANK, 0)
    for severity, count in rows:
        counts[str(severity)] = int(count)
    return counts


def get_alert(session: Session, alert_id: str) -> Alert | None:
    return session.get(Alert, alert_id)


def transition(
    session: Session,
    alert: Alert,
    *,
    action: str,
    silence_seconds: int | None = None,
    now: datetime | None = None,
) -> tuple[Alert, bool, str]:
    """执行一次状态操作。

    返回 `(告警, 是否发生变化, 说明)`。

    三种结果的区分很重要：

    | 情形 | 行为 |
    |---|---|
    | 已处于目标状态（重复操作） | `changed=False`，**不报错** —— 运维重复点击不应弹错 |
    | 目标状态合法且不同 | 迁移并 `changed=True` |
    | 目标状态不在允许集合 | 抛 `IllegalTransition` → API 层映射为 409 |
    """
    moment = now or datetime.now(UTC)
    current = alert.state

    target_by_action = {
        "ack": AlertState.ACKED.value,
        "resolve": AlertState.RESOLVED.value,
        "silence": AlertState.SILENCED.value,
        "unsilence": AlertState.FIRING.value,
    }
    target = target_by_action.get(action)
    if target is None:
        raise IllegalTransition(current, f"unknown action {action!r}")

    # 幂等：已是目标状态直接返回
    if current == target:
        return alert, False, f"告警已处于 {target}，无需变更"

    if target not in _allowed_targets(current):
        raise IllegalTransition(current, target)

    alert.state = target
    if target == AlertState.RESOLVED.value:
        alert.resolved_at = moment
    elif target == AlertState.FIRING.value:
        # 复发：清掉恢复时间，否则详情页会显示"已恢复但仍在触发"
        alert.resolved_at = None

    if target == AlertState.SILENCED.value:
        seconds = silence_seconds or 3600
        alert.labels = {
            **(alert.labels or {}),
            "silencedUntil": (moment + timedelta(seconds=seconds)).isoformat(),
        }
    elif "silencedUntil" in (alert.labels or {}):
        # 取消静默：清掉标记，避免残留字段误导前端
        labels = dict(alert.labels or {})
        labels.pop("silencedUntil", None)
        alert.labels = labels

    session.flush()
    return alert, True, f"{current} → {target}"


def silence_expired(alert: Alert, *, now: datetime | None = None) -> bool:
    """静默是否已到期。

    到期后**不自动迁移** —— 由调用方决定（通常是下一次规则求值时重新触发）。
    本函数只做判断，不产生副作用，便于单测。
    """
    if alert.state != AlertState.SILENCED.value:
        return False
    raw = (alert.labels or {}).get("silencedUntil")
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    return (now or datetime.now(UTC)) >= until


def bulk_transition(
    session: Session,
    alert_ids: list[str],
    *,
    action: str,
    silence_seconds: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """批量状态操作。

    一次事故往往产生多条告警（同宿主多个 VM），逐条点击不现实。
    **逐条报告结果**：一次 20 条里失败 1 条，运维需要知道是哪条。
    """
    results: list[dict[str, Any]] = []
    changed = unchanged = failed = 0

    for alert_id in alert_ids:
        alert = session.get(Alert, alert_id)
        if alert is None:
            failed += 1
            results.append({"alertId": alert_id, "result": "not_found"})
            continue
        try:
            _, did_change, message = transition(
                session,
                alert,
                action=action,
                silence_seconds=silence_seconds,
                now=now,
            )
        except IllegalTransition as exc:
            failed += 1
            results.append(
                {"alertId": alert_id, "result": "illegal_transition", "detail": str(exc)}
            )
            continue
        if did_change:
            changed += 1
            results.append({"alertId": alert_id, "result": "changed", "detail": message})
        else:
            unchanged += 1
            results.append({"alertId": alert_id, "result": "unchanged", "detail": message})

    return {
        "requested": len(alert_ids),
        "changed": changed,
        "unchanged": unchanged,
        "failed": failed,
        "results": results,
    }


def count_alerts(session: Session, *, states: list[str] | None = None) -> int:
    stmt = select(func.count()).select_from(Alert)
    if states:
        stmt = stmt.where(Alert.state.in_(states))
    return int(session.execute(stmt).scalar() or 0)


__all__ = [
    "IllegalTransition",
    "active_severity_counts",
    "alert_state_counts",
    "bulk_transition",
    "count_alerts",
    "get_alert",
    "query_alerts",
    "silence_expired",
    "transition",
]
