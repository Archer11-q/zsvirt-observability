"""告警引擎：事件 → 规则求值 → 产生/累加/恢复告警（`app.alerts`，L4）。

职责边界（`docs/backend/BACKEND_DESIGN.md` §2.6）：

  - `repository.py`：**管理**已存在的告警（查询、状态机、静默、批量）
  - 本模块：**产生**告警。规则求值、聚合去重、证据绑定、自动恢复

设计立场与诊断引擎一致：**规则是数据**（`app.alerts.rules`），本模块是求值器。
换规则集不改代码；改代码不改规则语义。

## 四条不可妥协的红线

1. **`evidenceEventIds` 必填**（`DATA_MODEL.md` §5.2）。没有证据的告警不允许存在 ——
   数据库 CHECK 已在表上强制（`ck_alert_evidence_required`），本模块再保证一次：
   候选告警缺少事件 id 时**直接丢弃**，绝不写入空证据告警。
2. **聚合去重**：同一 `aggregationKey` 重复触发只累加 `count` 并推进
   `last_fired_at`，**不新建告警**。否则一个持续故障会在两分钟内刷出上千条，
   把真正的信号埋掉 —— 这是告警系统最常见的失败方式。
3. **静默生效**：处于 `silenced` 且在静默期内的告警只更新证据，**不迁移状态**。
   静默的意义就是"现在别打扰我"。
4. **自动恢复**：超过 `recovery_after` 没有新证据的告警转为 `resolved` 并写入
   `resolved_at`。不确定态长期挂着会让运维学会忽略告警列表。

## 为什么可以"事件到达即求值"

求值只看**刚写入的这批事件**，不做跨批次状态记忆，因此不需要流式窗口引擎 ——
但也因此**不支持 `durationSec` 这类需要持续时间的条件**（见 `rules.AlertCondition`
的说明）。这是有意划的边界：做半套持续时间条件比不做更危险，因为规则作者会
以为它生效了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from app.alerts.rules import (
    AlertCondition,
    AlertRule,
    RuleSet,
    build_default_rule_set,
)
from app.enums import SEVERITY_RANK, AlertState
from app.models import Alert, Resource

#: 单次求值最多新建的告警数。防御性上限：若规则集被写坏（例如漏了
#: `event_type` 导致匹配一切），也不至于在一批 1000 条事件时创建上千条告警。
MAX_NEW_ALERTS_PER_BATCH = 200


@dataclass
class AlertEvaluation:
    """一次求值的结果摘要。**必须完整报告，不能只报"新建了几条"**。"""

    rule_set_version: str
    rules_evaluated: int
    events_evaluated: int
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped_silenced: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def matched(self) -> int:
        return len(self.created) + len(self.updated) + len(self.skipped_silenced)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ruleSetVersion": self.rule_set_version,
            "rulesEvaluated": self.rules_evaluated,
            "eventsEvaluated": self.events_evaluated,
            "created": len(self.created),
            "updated": len(self.updated),
            "skippedSilenced": len(self.skipped_silenced),
            "truncated": self.truncated,
        }


@dataclass
class ReconcileResult:
    """自动恢复的结果摘要。"""

    resolved: list[str] = field(default_factory=list)
    checked: int = 0

    def as_dict(self) -> dict[str, Any]:
        # `resolved` 保持**id 列表**而不是计数：调用方真正能用的是"哪几条被关了"。
        # 计数可由 `len()` 得到，反过来则不可逆 —— 与 `AlertEvaluation.as_dict`
        # 用 `created`/`updated` 表示计数的命名约定不同是有意的，那两者的字段名
        # 本身就写着"数量"。
        return {"checked": self.checked, "resolved": list(self.resolved)}


# ---------------------------------------------------------------- 条件求值


def _metric_value(metrics: dict[str, Any] | None, key: str) -> float | None:
    """从事件 `metrics` 里取值，容忍 camelCase 与带百分号的字符串。

    **两种键名都试**（`gpu_memory_usage` / `gpuMemoryUsage`）：规则按文档用
    下划线命名，而探针上报的是 camelCase。只支持一种会让规则"写对了却不触发"，
    这是最难查的一类静默失效。
    """
    if not metrics:
        return None
    camel = key
    head, _, tail = key.partition("_")
    if tail:
        camel = head + "".join(part.capitalize() for part in tail.split("_"))
    for candidate in (key, camel):
        raw = metrics.get(candidate)
        if raw is None:
            continue
        try:
            return float(str(raw).rstrip("%"))
        except (TypeError, ValueError):
            continue
    return None


def _condition_matches(
    condition: AlertCondition,
    *,
    event_type: str,
    severity: str,
    resource_kind: str | None,
    metrics: dict[str, Any] | None,
) -> bool:
    """单条条件对单个事件求值。所有给出的字段都必须满足。"""
    if condition.event_type is not None and event_type != condition.event_type:
        return False
    # 资源类型未知时**不匹配**：规则明确限定了类型，就不该在类型未知时放行。
    # （与 `validate_edge` 的"类型未知则放行"取舍相反，原因不同：
    #   那里放行是为了不丢关系，这里放行会产出归错层的告警。）
    if condition.resource_kinds and (
        resource_kind is None or resource_kind not in condition.resource_kinds
    ):
        return False
    if condition.min_severity is not None:
        threshold = SEVERITY_RANK.get(condition.min_severity)
        actual = SEVERITY_RANK.get(severity)
        if threshold is None or actual is None or actual < threshold:
            return False
    if condition.metric is not None:
        value = _metric_value(metrics, condition.metric.key)
        if value is None:
            return False
        if not condition.metric.matches(value):
            return False
    return True


# ---------------------------------------------------------------- 求值主流程


def evaluate_events(
    session: Session,
    events: Sequence[Any],
    *,
    rule_set: RuleSet | None = None,
    now: datetime | None = None,
    resource_kinds: dict[str, str] | None = None,
) -> AlertEvaluation:
    """对一批**新写入**的事件求值，产生/累加告警。

    `events` 必须是本次真正落库的事件。**不要把重复批次的事件传进来** ——
    虽然本函数对同一 `aggregationKey` 只累加计数，看不出差别，但 `count`
    会被重复投递灌水，让"这个故障触发了几次"变成假数据。

    调用方负责提交事务。本函数只 `flush`，不 `commit` —— 让告警与产生它的事件
    处于**同一个事务**：要么一起可见，要么一起不可见。
    """
    active_rules = rule_set or build_default_rule_set()
    moment = now or datetime.now(UTC)

    result = AlertEvaluation(
        rule_set_version=active_rules.version,
        rules_evaluated=len(active_rules.rules),
        events_evaluated=len(events),
    )
    if not events or not active_rules.rules:
        return result

    kinds = resource_kinds if resource_kinds is not None else _load_kinds(session, events)

    # `aggregationKey` → (规则, 证据事件 id 列表)
    candidates: dict[str, tuple[AlertRule, list[Any]]] = {}
    for event in events:
        kind = kinds.get(event.resource_id)
        for rule in active_rules.rules:
            if not _condition_matches(
                rule.condition,
                event_type=event.type,
                severity=event.severity,
                resource_kind=kind,
                metrics=event.metrics,
            ):
                continue
            key = rule.aggregation_key(event.resource_id)
            candidates.setdefault(key, (rule, []))[1].append(event)

    if not candidates:
        return result

    existing = _load_active_alerts(session, list(candidates))

    for key, (rule, matched_events) in candidates.items():
        # 红线：没有证据的告警不允许存在。
        #
        # `Event.id` 有数据库层的非空约束，且写入时即生成 —— 因此"事件存在但没有
        # id"只可能来自绕过 ORM 的调用方。这种情况**抛错**而不是静默丢弃：
        # 静默丢弃会让"为什么这条告警没产生"变成一个查不出来的谜。
        evidence_ids = [e.id for e in matched_events if e.id]
        if not evidence_ids:
            raise ValueError(
                f"规则 {rule.id} 匹配到 {len(matched_events)} 条事件，但它们都没有 id；"
                "告警必须有证据（DATA_MODEL.md §5.2），拒绝创建空证据告警"
            )

        alert = existing.get(key)
        if alert is None:
            if len(result.created) >= MAX_NEW_ALERTS_PER_BATCH:
                result.truncated = True
                continue
            alert = _create_alert(
                session,
                rule=rule,
                rule_set_version=active_rules.version,
                aggregation_key=key,
                resource_id=matched_events[0].resource_id,
                evidence_ids=evidence_ids,
                matched_events=matched_events,
                moment=moment,
            )
            existing[key] = alert
            result.created.append(alert.id)
            continue

        _accumulate(alert, evidence_ids=evidence_ids, matched_events=matched_events, moment=moment)
        if alert.state == AlertState.SILENCED.value:
            result.skipped_silenced.append(alert.id)
        else:
            result.updated.append(alert.id)

    session.flush()
    return result


def _load_kinds(session: Session, events: Sequence[Any]) -> dict[str, str]:
    """一次取回本批事件涉及资源的类型（避免逐条查询）。"""
    resource_ids = sorted({e.resource_id for e in events if e.resource_id})
    if not resource_ids:
        return {}
    rows = session.execute(
        select(Resource.id, Resource.kind).where(Resource.id.in_(resource_ids))
    ).all()
    return {str(rid): str(kind) for rid, kind in rows}


def _load_active_alerts(session: Session, aggregation_keys: list[str]) -> dict[str, Alert]:
    """取当前处于"活动"状态（firing / acked / silenced）的告警。

    已 `resolved` 的**不在这里** —— 条件再次满足时应当新建一条告警（并保留
    历史那条的恢复时间），而不是把旧记录"复活"并抹掉它曾经的恢复时刻。
    这也是"复发"与"从未恢复"在数据上可区分的前提。
    """
    active_states = [
        AlertState.FIRING.value,
        AlertState.ACKED.value,
        AlertState.SILENCED.value,
    ]
    rows = session.execute(
        select(Alert).where(
            Alert.aggregation_key.in_(aggregation_keys),
            Alert.state.in_(active_states),
        )
    ).scalars()
    return {a.aggregation_key: a for a in rows}


def _create_alert(
    session: Session,
    *,
    rule: AlertRule,
    rule_set_version: str,
    aggregation_key: str,
    resource_id: str,
    evidence_ids: list[str],
    matched_events: list[Any],
    moment: datetime,
) -> Alert:
    """新建一条告警。

    `severity` 取**规则严重级别与来源事件最高严重级别中更严重的那个**：
    规则说"这是警告"，但事件本身被上报为 critical，就不该把它降级显示 ——
    降级会让真正紧急的信号看起来不紧急。
    """
    from app.enums import worst_severity

    event_severity = matched_events[0].severity if matched_events else rule.severity
    severity = worst_severity(rule.severity, event_severity)

    unique_evidence = list(dict.fromkeys(evidence_ids))
    # 用**事件的发生时间**而不是接收时间：告警列表按 `lastFiredAt` 排序，
    # 若一批迟到 10 分钟的事件把 `lastFiredAt` 写成"现在"，这条告警会排到
    # 最新的位置，而它旁边的证据时间戳却是 10 分钟前 —— 排序与证据互相矛盾。
    fired_at = _latest_occurred_at(matched_events, fallback=moment)
    alert = Alert(
        id=f"alert_{ULID()}",
        rule_id=rule.id,
        resource_id=resource_id,
        severity=severity,
        state=AlertState.FIRING.value,
        first_fired_at=fired_at,
        last_fired_at=fired_at,
        # `count` 是"触发了几次"，与证据条数一致 —— 一批里命中 3 次就是 3。
        # 硬编码 1 会让 `count` 与紧挨着它的 `evidenceEventIds` 互相矛盾，
        # 而运维正是靠 `count` 判断"这事多频繁"。
        count=max(len(unique_evidence), 1),
        evidence_event_ids=unique_evidence,
        aggregation_key=aggregation_key,
        title=rule.title,
        labels={
            # 规则集版本：规则改动后，"这条告警当时是按什么规则报的"必须可回答
            "ruleSetVersion": rule_set_version,
            "rootCauseHint": rule.root_cause_hint,
            # 产生它的源头事件类型 —— 前端"为什么报"的最小说明
            "sourceEventType": matched_events[0].type if matched_events else None,
        },
    )
    session.add(alert)
    return alert


def _latest_occurred_at(events: Sequence[Any], *, fallback: datetime) -> datetime:
    """本批事件里**最晚**的 `occurredAt`；取不到时退回 `fallback`。

    取最晚而不是最早：`last_fired_at` 回答的是"最近一次触发是什么时候"，
    而一批事件里最后发生的那条才是最近一次。
    """
    moments = [e.occurred_at for e in events if getattr(e, "occurred_at", None) is not None]
    return max(moments) if moments else fallback


def _accumulate(
    alert: Alert,
    *,
    evidence_ids: list[str],
    matched_events: list[Any],
    moment: datetime,
) -> None:
    """把新证据累加到既有告警上。

    - `count` **按新增证据条数**累加，而不是简单 `+1`：
      "容器重启 3 次"和"重启 1 次"是不同的信息量。
    - 证据 id **去重后追加**，保留历史证据（诊断要能看到整条证据链）。
    - `last_fired_at` 推进到本次时间。
    - `acked` 状态**保持不动**：运维已经确认过了，新证据不等于需要重新确认。
      只有 `resolved` 才会因为新证据而变成新告警（见 `_load_active_alerts`）。
    - `firing` 状态若此前写过 `resolved_at`（不该发生，但数据可能被人工改过），
      这里顺手清掉，避免出现"正在触发但显示已恢复"的自相矛盾记录。
    """
    from app.enums import worst_severity

    existing_ids = list(alert.evidence_event_ids or [])
    new_ids = [eid for eid in dict.fromkeys(evidence_ids) if eid not in existing_ids]
    alert.evidence_event_ids = existing_ids + new_ids
    alert.count = int(alert.count or 0) + max(len(new_ids), 1)
    # 推进到本批事件的最新发生时间，且**不回退** —— 迟到批次的 `occurredAt`
    # 可能早于已有记录，让 `last_fired_at` 倒退会让告警在列表里"下沉"。
    newest = _latest_occurred_at(matched_events, fallback=moment)
    alert.last_fired_at = max(alert.last_fired_at, newest)

    if matched_events:
        alert.severity = worst_severity(alert.severity, matched_events[0].severity)

    if alert.state == AlertState.FIRING.value:
        alert.resolved_at = None


# ---------------------------------------------------------------- 自动恢复


def reconcile(
    session: Session,
    *,
    rule_set: RuleSet | None = None,
    now: datetime | None = None,
    recovery_after: timedelta | None = None,
) -> ReconcileResult:
    """把"条件已解除"的告警转为 `resolved`。

    判据：`last_fired_at` 距今超过恢复窗口，且状态为 `firing` / `acked`。

    **`silenced` 不参与恢复**：静默期内本来就不该收到新证据，用"没有新证据"
    推断条件解除会把静默变成"自动关单"，而运维静默的真实意图往往是
    "我在处理，先别叫我"。这条差异必须保留。

    **`acked` 会被恢复**：已确认不等于还在发生。若证据确实停了，它就该关掉。
    """
    active_rules = rule_set or build_default_rule_set()
    moment = now or datetime.now(UTC)
    window = recovery_after if recovery_after is not None else active_rules.recovery_after
    cutoff = moment - window

    rows = list(
        session.execute(
            select(Alert).where(
                Alert.state.in_([AlertState.FIRING.value, AlertState.ACKED.value]),
                Alert.last_fired_at < cutoff,
            )
        ).scalars()
    )

    result = ReconcileResult(checked=len(rows))
    for alert in rows:
        alert.state = AlertState.RESOLVED.value
        alert.resolved_at = moment
        result.resolved.append(alert.id)

    if result.resolved:
        session.flush()
    return result


# ---------------------------------------------------------------- 静默到期


def release_expired_silences(
    session: Session, *, now: datetime | None = None
) -> list[str]:
    """把静默已到期的告警放回 `firing`。

    静默到期**不会自动恢复**（那是 `reconcile` 的职责）：到期只意味着
    "可以再打扰我了"，不意味着问题消失了。两者混在一起会导致
    "静默 1 小时 → 到期瞬间告警全变成已恢复"，把仍在发生的故障关掉。
    """
    from app.alerts.repository import silence_expired

    moment = now or datetime.now(UTC)
    rows = list(
        session.execute(
            select(Alert).where(Alert.state == AlertState.SILENCED.value)
        ).scalars()
    )
    released: list[str] = []
    for alert in rows:
        if silence_expired(alert, now=moment):
            alert.state = AlertState.FIRING.value
            labels = dict(alert.labels or {})
            labels.pop("silencedUntil", None)
            alert.labels = labels
            # **重新基准化 `last_fired_at`**：静默期内没有新证据，是因为我们
            # 主动屏蔽了通知，而不是因为问题消失了。若保留旧的
            # `last_fired_at`，恢复窗口会把这段"没在听"的时间算作"证据已停"，
            # 于是静默一到期的告警会被立刻判为已恢复 ——
            # 那正是"静默偷偷变成自动关单"这个被明令禁止的行为。
            #
            # 释放这一刻它确实重新处于触发态，恢复窗口应该从**这里**重新计时。
            alert.last_fired_at = moment
            released.append(alert.id)
    if released:
        session.flush()
    return released


def reconcile_all(
    session: Session,
    *,
    rule_set: RuleSet | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """依次完成"静默到期释放"与"条件解除恢复"，返回**两个独立的**摘要。

    供后台定时任务或演示脚本调用。放在一个函数里的理由是两者都依赖"当前时间"：
    分开调用容易传入两个不同的 `now`，产生难以复现的边界行为。

    两个阶段刻意**不合并**：

    - 静默到期只表示"可以再打扰我了"，不表示问题消失；
    - 条件恢复是"证据停了这么久，判定已恢复"。

    若把两者合成一个动作，刚从静默中释放出来的告警会立刻因为"静默期间没有新证据"
    而被判为已恢复 —— 那等于把静默偷偷变成自动关单，而运维静默的意图往往是
    "我在处理，先别叫我"。

    返回 `{"released": [...], "recovery": {...}}`。
    """
    moment = now or datetime.now(UTC)
    released = release_expired_silences(session, now=moment)

    # **必须 flush**：`reconcile` 用同一个 session 发 SELECT，而本项目关闭了
    # autoflush，未 flush 的状态改动对这条查询不可见 —— 刚释放的静默告警会被
    # 当成仍然是 `silenced`，于是既不进候选、又（更糟的版本里）被误判为已恢复。
    # 这正是"静默偷偷变成自动关单"的实现级成因。
    if released:
        session.flush()

    recovered = reconcile(session, rule_set=rule_set, now=moment)
    return {"released": released, "recovery": recovered.as_dict()}


__all__ = [
    "MAX_NEW_ALERTS_PER_BATCH",
    "AlertEvaluation",
    "ReconcileResult",
    "evaluate_events",
    "reconcile",
    "reconcile_all",
    "release_expired_silences",
]
