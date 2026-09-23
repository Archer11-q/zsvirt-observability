"""自动诊断联动：告警产生 → 一次诊断 → 回写 `alert.diagnosisId`（任务 3）。

设计依据：C 的 Q13 要求"自动 + 手动都要"。手动路径已在
`POST /api/v1/diagnoses` 里；本模块补上自动路径。

## 为什么不在同一个请求里同步诊断

诊断本身是纯规则计算、通常 < 1s，**单独看完全可以在上报请求里同步做完**。
但一次上报可能瞬间产生十几条告警（同宿主多台 VM 同时受影响），
每条都跑一次"装配资源图 + 扫时间窗事件"，会把探针的写入延迟放大到秒级 ——
而探针的批量上限是 1 秒 / 1MB，写入变慢会直接反压到采集。

因此自动诊断**在事务提交之后**执行（`run_auto_diagnosis` 由调用方在上报落库
成功后调用），并且：

- **有上限**（`max_diagnoses`）：宁可漏掉一部分自动诊断，也不让一次上报卡住；
  未诊断的告警在列表里 `diagnosisId` 为 `null`，用户点"一键诊断"即可补上。
- **只处理新建的告警**：累加到既有告警上的证据不重复触发诊断（见下"幂等"）。
- **失败必须被吞掉并如实报告**：诊断是增值能力，它的失败不能影响上报结果。

## 幂等

按 `alertId` 去重，且**已有 `diagnosisId` 的告警不再诊断**：

  一条持续两小时的告警，每 15 秒收到一批新证据 ⇒ 480 次上报。
  若每次都诊断，会写入 480 条内容几乎相同的结论，把诊断列表变成噪声，
  而"这条告警的根因是什么"的答案并不会因此更准确。

## 事故聚合（一次事故一条结论）

逐条告警诊断是**错误的单位**：一次容器 OOM 会产生多条告警（OOM、重启），
逐条诊断会写出多条内容几乎相同的结论。

因此先按「**同一资源 + 时间相近**」把告警聚成事故，每个事故诊断一次，
事故内所有告警都回写到同一条结论上。

判据是「**一方资源是另一方的祖先或相等** + 触发时间相近」。这个不对称正是关键：

| 情形 | 判断 | 理由 |
|---|---|---|
| GPU 显存耗尽 与 容器 OOM | **不合并** | GPU 是容器的祖先 → 不是同一事故的同一面；两者根因不同 |
| 容器 OOM 与它引起的 AI 服务推理错误 | **合并** | 容器是 AI 服务的祖先 → 运维感受到的是**一个问题** |
| 同一容器的 OOM 与重启循环 | **合并** | 资源相等 |
| 同一资源上相隔很久的两次告警 | **不合并** | `CLUSTER_WINDOW` 限制 |

**两种更简单的写法都是错的**（都试过）：

- 按**资源相等**聚合 → 容器的两条告警与 AI 服务的一条被拆开，一次事故产出两条结论；
- 按**可达闭包相交**聚合 → 单条链路上任何两条告警都相交，三类不同故障（七条告警）
  被并成一次事故、只剩一条结论，另外两个根因直接消失。

诊断本身仍在该资源的**可达闭包**上运行，跨层关联不受影响 —— 变的只是聚合判据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.diagnosis import service as diagnosis_service
from app.enums import AlertState, Severity
from app.models import Alert

#: 单次联动最多执行的诊断数。防止一次大批量上报触发无上限的图遍历。
DEFAULT_MAX_DIAGNOSES = 10

#: 事故聚合的时间窗：同一资源上触发时间相差超过它的两条告警视为两次事故。
#: 没有这个窗，"同一资源"会把一个资源的全部告警历史并成一次事故。
CLUSTER_WINDOW = timedelta(minutes=30)

#: 只对达到该级别的活动告警自动诊断。
#:
#: `info` / `warning` 级告警**不自动诊断**：它们多数是趋势提示（利用率偏高、
#: 重启一次），自动跑根因分析只会产生一堆 `UNKNOWN` —— 而 `UNKNOWN` 结论太多
#: 会让运维学会忽略整个诊断列表。让用户按需点"一键诊断"更诚实。
#:
#: 把它作为**参数**而不是写死的常量，是为了让"演示两条路径"不需要另写一份
#: 诊断逻辑：演示脚本传 `min_severity=Severity.INFO.value` 即可覆盖全部告警。
DEFAULT_MIN_SEVERITY = Severity.ERROR.value


@dataclass
class AutoDiagnosisResult:
    """自动联动摘要。**必须报告跳过了什么、为什么**，否则"为什么这条告警没有诊断"
    查不出来。"""

    attempted: list[str] = field(default_factory=list)
    linked: dict[str, str] = field(default_factory=dict)
    skipped_already_diagnosed: list[str] = field(default_factory=list)
    skipped_below_severity: list[str] = field(default_factory=list)
    skipped_not_firing: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": len(self.attempted),
            "linked": dict(self.linked),
            "skippedAlreadyDiagnosed": len(self.skipped_already_diagnosed),
            "skippedBelowSeverity": len(self.skipped_below_severity),
            "skippedNotFiring": len(self.skipped_not_firing),
            "failed": dict(self.failed),
            "truncated": self.truncated,
        }


def run_auto_diagnosis(
    session: Session,
    alert_ids: list[str],
    *,
    max_diagnoses: int = DEFAULT_MAX_DIAGNOSES,
    min_severity: str = DEFAULT_MIN_SEVERITY,
    now: datetime | None = None,
) -> AutoDiagnosisResult:
    """对给定告警执行自动诊断并回写 `diagnosisId`。

    **调用方负责在上报事务提交之后调用**，并负责提交本函数产生的改动。
    在事务内调用会让"诊断失败"有机会回滚掉已经成功的上报 —— 那是最糟的取舍：
    数据采集的主流程不能因为增值分析出问题而失败。
    """
    from app.enums import SEVERITY_RANK

    moment = now or datetime.now(UTC)
    result = AutoDiagnosisResult()
    if not alert_ids:
        return result

    rows = {
        a.id: a for a in session.execute(select(Alert).where(Alert.id.in_(alert_ids))).scalars()
    }

    threshold = SEVERITY_RANK.get(min_severity, 0)

    # ---- 第一遍：筛出"值得自动诊断"的告警 ----
    candidates: list[Alert] = []
    for alert_id in alert_ids:
        alert = rows.get(alert_id)
        if alert is None:
            # 告警不存在：不算失败，只是没什么可诊断的
            continue
        if alert.diagnosis_id:
            # 幂等：已经有结论了，不重复诊断
            result.skipped_already_diagnosed.append(alert_id)
            continue
        if alert.state not in (AlertState.FIRING.value, AlertState.ACKED.value):
            # 已恢复 / 已静默的告警不自动诊断：静默是"别打扰我"，
            # 已恢复则没什么可查的
            result.skipped_not_firing.append(alert_id)
            continue
        if SEVERITY_RANK.get(alert.severity, 0) < threshold:
            result.skipped_below_severity.append(alert_id)
            continue
        candidates.append(alert)

    if not candidates:
        return result

    # ---- 第二遍：聚成事故，每个事故诊断一次 ----
    #
    # 排序只影响 `max_diagnoses` 截断时的取舍：严重优先，同级则**最早触发**的优先
    # （首个症状信息量更大，且不会因为一条告警反复触发而漂移）。
    candidates.sort(
        key=lambda a: (SEVERITY_RANK.get(a.severity, 0), -a.first_fired_at.timestamp()),
        reverse=True,
    )

    # 事故内所有告警都落在同一条"资源祖先链"上，因此取每条事故的**根资源**
    # （链上最高的那个）作为聚合键。
    relations = _build_relations(session, [a.resource_id for a in candidates])

    clusters: list[tuple[str, list[Alert]]] = []
    for alert in candidates:
        for index, (root, members) in enumerate(clusters):
            # 一方是另一方的祖先或相等，且时间窗重叠 → 同一次事故
            if _related(root, alert.resource_id, relations) and any(
                _times_overlap(m, alert) for m in members
            ):
                clusters[index] = (
                    _higher(root, alert.resource_id, relations),
                    [*members, alert],
                )
                break
        else:
            clusters.append((alert.resource_id, [alert]))

    # **事故锚点取最靠上的资源**，而不是成员里"最严重/最近"那条告警的资源。
    #
    # 这不是排列偏好，而是正确性要求：诊断会忽略**锚点祖先**上的证据
    # （锚点自己已有告警时，祖先上的另一起故障不该压过它）。若锚点落在事故
    # 中间，事故里位于锚点**上方**的成员证据就会被误过滤 —— 实测表现为
    # 网络场景因为锚点选了 agent、容器成了它的祖先，于是得出 AGENT_TASK_FAILURE。
    #
    # 锚点取最高点后，事故内每个成员都落在锚点的向下范围内，
    # "忽略祖先证据"就绝不会滤掉事故自己观测到的东西。
    clusters = [
        (root, sorted(members, key=lambda a: a.first_fired_at)) for root, members in clusters
    ]

    for root, members in clusters:
        if len(result.attempted) >= max_diagnoses:
            result.truncated = True
            continue
        # 两个范围必须分清：
        #   证据范围 = 事故**根资源**的可达闭包（跨层关联靠它，向上也要看）
        #   兄弟范围 = 事故根资源的**后代及自身**上的已有结论
        # 把后者也写成整条链的闭包，会让不同故障共享同一条结论（踩过两次）。
        scope = diagnosis_service.resource_scope_for(session, root)
        siblings = _diagnosed_alerts_for(session, root)
        lead = next(m for m in members if m.resource_id == root)
        result.attempted.append(lead.id)
        try:
            linked_id = _diagnose_cluster(
                session,
                members,
                scope,
                anchor_resource_id=root,
                now=moment,
                # 把"该事故范围内**已经**有结论的告警"一并交进去，让复用判断看到它们。
                #
                # 只检查 `members` 会漏掉真实情形：按门限放宽的那一轮里，
                # 已经诊断过的严重告警不是候选，于是未诊断的告警被聚成一个
                # **只含自己**的事故，然后为同一次事故写出第二条结论。
                siblings=siblings,
            )
        except Exception as exc:  # noqa: BLE001 - 诊断失败绝不能影响上报
            # 回滚到本次尝试之前的干净状态：部分装配可能已经污染了 session。
            session.rollback()
            for member in members:
                result.failed[member.id] = f"{type(exc).__name__}: {exc}"
            continue
        if linked_id is not None:
            # 事故内**所有**告警都关联到同一条结论 —— 不留没有答案的告警
            for member in members:
                result.linked[member.id] = linked_id

    return result


def _build_relations(session: Session, resource_ids: list[str]) -> dict[tuple[str, str], bool]:
    """`(祖先, 后代) -> True` 的关系表，只覆盖给定资源。

    一次算好、按参数传给聚合逻辑，**不用模块级缓存** —— 模块级可变状态在 Web
    服务里是并发缺陷的温床（两个并发上报会互相踩），也会在测试之间泄漏。
    候选资源数量很小，每次重算的代价可以忽略。
    """
    relations: dict[tuple[str, str], bool] = {}
    unique = list(dict.fromkeys(resource_ids))
    for resource_id in unique:
        # 用服务层的图查询，不在这里再写一遍遍历
        closure = diagnosis_service.resource_scope_for(session, resource_id)
        relations[(resource_id, resource_id)] = True
        for other in unique:
            if other != resource_id and other in closure:
                relations[(other, resource_id)] = True
    return relations


def _related(a_id: str, b_id: str, relations: dict[tuple[str, str], bool]) -> bool:
    """两资源是否互为祖先或相等（方向无关）。"""
    return relations.get((a_id, b_id), False) or relations.get((b_id, a_id), False)


def _higher(a_id: str, b_id: str, relations: dict[tuple[str, str], bool]) -> str:
    """返回两者中**更靠上**（祖先方向）的那个资源 id。"""
    return a_id if relations.get((a_id, b_id), False) else b_id


def _times_overlap(a: Alert, b: Alert) -> bool:
    """两条告警的触发时间是否落在同一个聚合窗口内。

    按"区间相交"判断而不是"起点相近"：一条告警可能持续很久（`first_fired_at`
    很早、`last_fired_at` 是刚刚），用起点距离会把同一事故的后续告警判成新事故。
    """
    a_start, a_end = a.first_fired_at, a.last_fired_at
    b_start, b_end = b.first_fired_at, b.last_fired_at
    latest_start = max(a_start, b_start)
    earliest_end = min(a_end, b_end)
    if latest_start <= earliest_end:
        return True  # 区间本身相交
    # 不相交时看间隔是否在窗口内
    gap = latest_start - earliest_end
    return gap <= CLUSTER_WINDOW


def _diagnosed_alerts_for(session: Session, root_resource_id: str) -> list[Alert]:
    """事故**根资源及其后代**上已有诊断结论的活动告警。

    用途见 `_diagnose_cluster` 的 `siblings` 参数：判断"这次事故是否已经有结论"。

    **不能按整条可达闭包查。** 闭包还含**祖先**，于是一次 GPU 故障产生的结论
    会在容器事故的"兄弟"里出现并被复用，三类不同故障最后只剩一条结论 ——
    这个错踩过，而且单测覆盖不到，是端到端验证抓出来的。

    | 用途 | 范围 |
    |---|---|
    | 判断"是不是同一次事故" | 根资源**自身 + 后代** + 时间窗 |
    | 判断"哪些证据可以解释它" | 根资源的**完整可达闭包**（含祖先） |
    """
    # 向下闭包（自身 + 后代）：**不过滤成整条链**，否则祖先方向上的故障结论
    # 会被当成"同一次事故已有答案"而复用（踩过两次）。
    scope = diagnosis_service.descendant_scope_for(session, root_resource_id)
    rows = session.execute(
        select(Alert).where(
            Alert.resource_id.in_(sorted(scope)),
            Alert.diagnosis_id.is_not(None),
            Alert.state.in_([AlertState.FIRING.value, AlertState.ACKED.value]),
        )
    ).scalars()
    return list(rows)


def _diagnose_cluster(
    session: Session,
    members: list[Alert],
    union_scope: set[str],
    *,
    anchor_resource_id: str,
    now: datetime,
    siblings: list[Alert] | None = None,
) -> str | None:
    """诊断一次事故（一组告警）并回写到**每一条**告警上。

    锚点由调用方给定，是事故里**最靠上**的资源（见 `run_auto_diagnosis` 的说明）。
    不在这里"挑最严重的那条告警"，是因为锚点一旦落在事故中间，事故上方成员的
    证据会被"忽略祖先证据"误过滤。
    时间窗取全部成员的并集 —— 一次事故里的告警可能跨越几分钟。
    返回 `diagnosisId`，锚点资源已不存在时返回 `None`。

    **若事故里已有成员关联了结论，则复用那条结论**，不再写第二条。
    这条守卫与"为什么先前被诊断"无关 —— 可能是上一个批次、手动触发、
    或另一档门限。一次事故两条结论只会制造噪声，而第一条已经带了证据。
    """
    family = list(members) + list(siblings or ())
    existing = next((m.diagnosis_id for m in family if m.diagnosis_id), None)
    if existing is not None:
        for member in members:
            member.diagnosis_id = existing
        session.flush()
        return existing

    lead = next(m for m in members if m.resource_id == anchor_resource_id)
    resolved = diagnosis_service.resolve_trigger(
        session,
        alert_id=lead.id,
        anchor_resource_id=None,
        window=None,
        now=now,
    )
    if isinstance(resolved, str):
        # 锚点资源不存在（告警引用的资源被清理过）→ 不诊断，但也不报错
        return None

    _resolved_anchor, _resolved_window, _trigger = resolved

    # 时间窗是**这次事故自己的跨度**加标准余量，而不是"最早告警再往前推 30 分钟"。
    #
    # 宽窗会把更早的、无关事故的事件也拉进来（实测：容器事故吃到了上一次
    # GPU 注入的事件）。`first_fired_at` 本身已经含有告警形成时间，
    # 因此不需要再额外向前扩展。
    window_from = min(m.first_fired_at for m in members)
    window_to = max(m.last_fired_at for m in members) + diagnosis_service.DEFAULT_WINDOW_AFTER
    window = diagnosis_service.TimeWindow(window_from, window_to)

    row, _duration_ms = diagnosis_service.run_diagnosis(
        session,
        anchor_resource_id=anchor_resource_id,
        window=window,
        trigger={
            "alertId": lead.id,
            # 事故聚合的痕迹必须留在结论里，否则"为什么这条结论关联了三条告警"
            # 事后无从回答
            "alertIds": [m.id for m in members],
            "clusterSize": len(members),
        },
        now=now,
        # 只看这次事故涉及资源的**并集闭包**：
        # 太窄（只取 lead 采纳的事件）会关掉跨层关联；
        # 不限定又会把同一时间窗里无关故障的证据混进来。
        resource_scope=union_scope,
    )
    for member in members:
        member.diagnosis_id = row.id
    session.flush()
    return row.id


__all__ = [
    "DEFAULT_MAX_DIAGNOSES",
    "DEFAULT_MIN_SEVERITY",
    "AutoDiagnosisResult",
    "run_auto_diagnosis",
]
