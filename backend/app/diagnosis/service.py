"""诊断服务：装配上下文 → 跑引擎 → 落库。

设计要点（`docs/backend/DIAGNOSIS_DESIGN.md`）：

  - 引擎本身是**纯函数**（不访问数据库、不发 HTTP）；本模块负责从库里
    装配 `DiagnosisContext` 并把结论落回 `diagnosis` 表
  - 因此"引擎逻辑"与"数据装配"分离：引擎的可测试性不因接入数据库而受损

**规则集现状**：本模块接受一个 `RuleSet`；当前默认传入**空规则集**，
因此所有诊断都会返回 `UNKNOWN` + 已收集到的证据。这是**正确行为**，
不是缺陷 —— 红线是"不编造根因"（`DATA_MODEL.md` §5.3）。规则内容属
`docs/DEVELOPMENT_PLAN.md` 任务 2（告警引擎），届时只需换入真实规则集，
本模块与 API 层无需改动。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ulid import ULID

from app.diagnosis import Diagnosis, DiagnosisContext, RuleSet, TimeWindow, diagnose
from app.diagnosis.types import Edge as DiagnosisEdge
from app.diagnosis.types import Evidence, EvidenceKind, Resource
from app.events.repository import query_events
from app.graph.repository import load_graph
from app.models import Alert
from app.models import Diagnosis as DiagnosisRow
from app.models import Resource as ResourceRow

#: 诊断窗口的默认跨度。够覆盖"故障发生 → 症状显现"的常见间隔。
DEFAULT_WINDOW_BEFORE = timedelta(minutes=10)
DEFAULT_WINDOW_AFTER = timedelta(minutes=2)

#: 单次诊断最多纳入的事件数。防止超大时间窗把整个事件表拉进内存 ——
#: 诊断是交互式操作，宁可截断并留 note，也不要让一次点击变成全表扫描。
MAX_CONTEXT_EVENTS = 5000

#: 资源图遍历深度上限。链路最长是 HOST→GPU→vGPU→VM→CONTAINER→AI_SERVICE→AGENT，
#: 8 层留有余量。
MAX_GRAPH_DEPTH = 8


def build_context(
    session: Session,
    *,
    anchor_resource_id: str,
    window: TimeWindow,
    rule_set: RuleSet | None = None,
) -> DiagnosisContext:
    """从数据库装配诊断上下文。

    资源图只取锚点周围（`max_depth` 限制），事件按时间窗过滤 ——
    两者都刻意设上限，避免"一次诊断扫全库"。
    """
    nodes, edges = load_graph(session, root_id=anchor_resource_id, max_depth=MAX_GRAPH_DEPTH)

    resources = {
        r.id: Resource(
            id=r.id,
            kind=r.kind,
            status=r.status,
            observability=r.observability,
            parent_id=r.parent_id,
        )
        for r in nodes
    }

    diagnosis_edges = tuple(
        DiagnosisEdge(parent_id=e.parent_id, child_id=e.child_id, relation=e.relation)
        for e in edges
    )

    # 事件按**资源范围**过滤，而不是把全窗事件都装进来：引擎的步骤 3 只保留
    # 锚点可达资源上的证据，提前在 SQL 里剪掉可以减少内存与匹配开销。
    reachable = {anchor_resource_id}
    for node in nodes:
        reachable.add(node.id)

    events, _ = query_events(
        session,
        limit=MAX_CONTEXT_EVENTS,
        occurred_from=window.start,
        occurred_to=window.end,
        resource_ids=sorted(reachable),
    )

    evidence = tuple(
        Evidence(
            kind=EvidenceKind.EVENT,
            name=e.type,
            at=e.occurred_at,
            resource_id=e.resource_id,
            value=_evidence_value(e),
            count=_evidence_count(e),
            event_ids=(e.id,),
        )
        for e in events
    )

    return DiagnosisContext(
        anchor_resource_id=anchor_resource_id,
        window=window,
        resources=resources,
        edges=diagnosis_edges,
        evidence=evidence,
        rule_set=rule_set or RuleSet(version="rs-unconfigured"),
    )


def _evidence_value(event: Any) -> str | None:
    """从事件里取出**可比较**的观测值。

    只有 `metrics.value` 无法表达"断言式"事件（如 `gpu.memory.exhausted`
    没有数值），因此退回到 `message` 的短描述；两者都没有就是 `None`，
    规则里的比较型条件会自动判为不匹配（而不是崩在 `float(None)`）。
    """
    metrics = event.metrics or {}
    if "value" in metrics:
        return str(metrics["value"])
    return None


def _evidence_count(event: Any) -> int | None:
    """事件的聚合计数。没有 `count` 字段时视为 1（单条事件即一次发生）。"""
    metrics = event.metrics or {}
    raw = metrics.get("count")
    if raw is None:
        return 1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def run_diagnosis(
    session: Session,
    *,
    anchor_resource_id: str,
    window: TimeWindow,
    trigger: dict[str, Any],
    rule_set: RuleSet | None = None,
    now: datetime | None = None,
) -> tuple[DiagnosisRow, int]:
    """执行一次诊断并落库。返回 `(诊断行, 耗时毫秒)`。

    耗时**不写进数据库**（它是运行期指标不是结论的一部分），由 API 层
    放进响应；这样同一条结论回放时不带上一次的耗时，符合"结论可复现"。
    """
    started = datetime.now(UTC)
    ctx = build_context(
        session,
        anchor_resource_id=anchor_resource_id,
        window=window,
        rule_set=rule_set,
    )

    result: Diagnosis = diagnose(ctx, now=now)
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

    _assert_confidence_reproducible(result)

    row = DiagnosisRow(
        id=f"diag_{ULID()}",
        created_at=result.created_at,
        trigger=trigger,
        root_cause=result.root_cause,
        confidence=result.confidence,
        confidence_breakdown=[
            {
                "ruleId": h.rule_id,
                "contribution": h.contribution,
                "observed": h.observed,
            }
            for h in result.confidence_breakdown
        ],
        affected_resources=list(result.affected_resources),
        potentially_affected=list(result.potentially_affected),
        on_chain=list(result.on_chain),
        evidence=[
            {
                "type": ev.kind.value,
                "name": ev.name,
                "resourceId": ev.resource_id,
                "value": ev.value,
                "count": ev.count,
                "at": ev.at.astimezone(UTC).isoformat(),
                "eventIds": list(ev.event_ids),
                "source": ev.source.value,
            }
            for ev in result.evidence
        ],
        recommendation=[{"code": r.code, "text": r.text} for r in result.recommendation],
        rule_set_version=result.rule_set_version,
        notes=list(result.notes),
    )
    session.add(row)
    session.flush()

    return row, duration_ms


def _assert_confidence_reproducible(result: Diagnosis) -> None:
    """自检：`confidence` 必须等于 breakdown 贡献之和（允许浮点误差）。

    这是 D-036「可复算」的落地。若引擎将来改动破坏了可复算性，
    这里会立刻失败，而不是让用户拿到一个无法解释的数字。

    注意：**降权/裁剪会让两者不再相等**（引擎在冲突降权与裁剪到 1.0 时
    会改写 confidence），因此只在 breakdown 本身就是最终得分时校验 ——
    判据是"引擎没有在 notes 里报告任何改写"。
    """
    rewritten = any(
        ("裁剪" in note) or ("降权" in note) or ("强制返回 UNKNOWN" in note)
        for note in result.notes
    )
    if rewritten:
        return
    total = sum(h.contribution for h in result.confidence_breakdown)
    if abs(total - result.confidence) > 1e-6:
        raise AssertionError(
            f"confidence 不可复算：breakdown 之和={total}，confidence={result.confidence}。"
            "这是引擎缺陷，拒绝落库。"
        )


def resolve_trigger(
    session: Session,
    *,
    alert_id: str | None,
    anchor_resource_id: str | None,
    window: dict[str, str] | None,
    now: datetime | None = None,
) -> tuple[str, TimeWindow, dict[str, Any]] | str:
    """解析触发参数，返回 `(锚点资源 ID, 时间窗, trigger 描述)` 或**错误说明字符串**。

    返回字符串表示参数不合法 —— 比抛异常更便于 API 层直接映射为 400
    并带上具体原因。
    """
    moment = now or datetime.now(UTC)

    if alert_id:
        alert = session.get(Alert, alert_id)
        if alert is None:
            return f"告警不存在：{alert_id}"
        # 告警触发：锚点取告警所在资源，窗口覆盖"首次触发之前 → 现在"。
        # 起点取 first_fired_at 而不是 last_fired_at：根因必须早于症状，
        # 只看最近一次触发会把根因事件漏在窗外。
        window_from = alert.first_fired_at - DEFAULT_WINDOW_BEFORE
        return (
            alert.resource_id,
            TimeWindow(window_from, moment + DEFAULT_WINDOW_AFTER),
            {"alertId": alert_id},
        )

    if not anchor_resource_id:
        return "必须提供 alertId，或同时提供 anchorResourceId 与 window"

    resource = session.get(ResourceRow, anchor_resource_id)
    if resource is None:
        return f"资源不存在：{anchor_resource_id}"

    if window:
        raw_from = window.get("from")
        raw_to = window.get("to")
        if not raw_from or not raw_to:
            return "window 必须同时包含 from 与 to"
        try:
            parsed_from = datetime.fromisoformat(raw_from)
            parsed_to = datetime.fromisoformat(raw_to)
        except (TypeError, ValueError) as exc:
            return f"window 时间格式非法（需 ISO8601）：{exc}"
        if parsed_from >= parsed_to:
            return "window 非法：from 必须早于 to"
        return (
            anchor_resource_id,
            TimeWindow(parsed_from, parsed_to),
            {
                "anchorResourceId": anchor_resource_id,
                "window": {"from": raw_from, "to": raw_to},
            },
        )

    # 未给窗口：用默认窗口（锚点最近活跃时间之前的一段时间）
    window_from = (resource.last_seen_at or moment) - DEFAULT_WINDOW_BEFORE
    window_to = moment + DEFAULT_WINDOW_AFTER
    return (
        anchor_resource_id,
        TimeWindow(window_from, window_to),
        {
            "anchorResourceId": anchor_resource_id,
            "window": {"from": window_from.isoformat(), "to": window_to.isoformat()},
        },
    )


def query_diagnoses(
    session: Session,
    *,
    limit: int,
    cursor: tuple[datetime, str] | None = None,
    root_causes: list[str] | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    resource_id: str | None = None,
) -> tuple[list[DiagnosisRow], bool]:
    """诊断列表查询（keyset 分页，语义与事件/告警一致：稳定、不漏不重）。

    `resource_id` 过滤用 PostgreSQL 的数组包含，这样"某资源参与过哪些诊断"
    不需要全表扫描。
    """
    from sqlalchemy import and_, or_

    fetch_limit = limit + 1
    stmt = select(DiagnosisRow)

    if root_causes:
        stmt = stmt.where(DiagnosisRow.root_cause.in_(root_causes))
    if created_from is not None:
        stmt = stmt.where(DiagnosisRow.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(DiagnosisRow.created_at <= created_to)
    if resource_id:
        # 该资源出现在三集里任一组都算相关 —— 只查 affected 会漏掉
        # "被判定为潜在受影响"的资源的诊断历史。
        stmt = stmt.where(
            DiagnosisRow.affected_resources.contains([resource_id])
            | DiagnosisRow.potentially_affected.contains([resource_id])
            | DiagnosisRow.on_chain.contains([resource_id])
        )
    if cursor is not None:
        ts, item_id = cursor
        stmt = stmt.where(
            or_(
                DiagnosisRow.created_at < ts,
                and_(DiagnosisRow.created_at == ts, DiagnosisRow.id < item_id),
            )
        )

    stmt = stmt.order_by(DiagnosisRow.created_at.desc(), DiagnosisRow.id.desc())
    rows = list(session.execute(stmt.limit(fetch_limit)).scalars())
    has_more = len(rows) > limit
    return rows[:limit], has_more


def root_cause_counts(
    session: Session, *, created_from: datetime | None = None
) -> dict[str, int]:
    """按根因码统计诊断数量（前端"根因 Top N"）。"""
    stmt = select(DiagnosisRow.root_cause, func.count()).group_by(DiagnosisRow.root_cause)
    if created_from is not None:
        stmt = stmt.where(DiagnosisRow.created_at >= created_from)
    rows = session.execute(stmt).all()
    return {str(rc): int(n) for rc, n in rows}


def latest_diagnoses_per_owner(
    session: Session, owners: dict[str, list[str]]
) -> dict[str, DiagnosisRow]:
    """按"归属关系"取每个工作负载最近一次诊断。

    `owners` 是 `资源 ID → 拥有它的工作负载 ID 列表` 的映射（由
    `app.api.workloads._resolve_owners` 沿父链算出，**一对多**）。

    **必须先做这一步归并，不能要求诊断恰好点名工作负载**：

      "容器的 GPU 显存耗尽"这条诊断的 affected 里是容器，链路是
      HOST→GPU→vGPU→VM→CTR→AIS，AI 服务从头到尾没被提及 —— 但它恰恰是
      **受影响的那一方**。若只认"诊断提到了工作负载 ID"，链路下层的诊断在
      业务视图里就全部不可见，工作负载总览也就失去了意义。

    实现：一次按 `created_at DESC` 的扫描，内存里对每个工作负载只保留第一条。
    诊断表规模（演示与比赛量级）下完全够用，也不需要窗口函数。
    """
    if not owners:
        return {}

    all_resources = sorted(owners)
    stmt = (
        select(DiagnosisRow)
        .where(
            DiagnosisRow.affected_resources.overlap(all_resources)
            | DiagnosisRow.potentially_affected.overlap(all_resources)
            | DiagnosisRow.on_chain.overlap(all_resources)
        )
        .order_by(DiagnosisRow.created_at.desc())
    )

    wanted_owners = {owner for resource_owners in owners.values() for owner in resource_owners}
    found: dict[str, DiagnosisRow] = {}
    for row in session.execute(stmt).scalars():
        row_resources: set[str] = set(row.affected_resources or ())
        row_resources.update(row.potentially_affected or ())
        row_resources.update(row.on_chain or ())
        for resource_id in row_resources:
            for owner in owners.get(resource_id, ()):
                # `setdefault` 配合 desc 排序 = "每个工作负载只留最新的一条"
                found.setdefault(owner, row)
        if len(found) == len(wanted_owners):
            break
    return found


def latest_diagnosis_for_resource(
    session: Session, resource_id: str
) -> DiagnosisRow | None:
    """取某资源最近一次**点名它**的诊断。

    与 `latest_diagnoses_per_owner` 的区别：这个不沿链路归并，只找真正提到该
    资源的诊断。用于"这个资源的诊断历史"这类单点查询。
    """
    stmt = (
        select(DiagnosisRow)
        .where(
            DiagnosisRow.affected_resources.contains([resource_id])
            | DiagnosisRow.potentially_affected.contains([resource_id])
            | DiagnosisRow.on_chain.contains([resource_id])
        )
        .order_by(DiagnosisRow.created_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def link_to_alert(session: Session, alert_id: str, diagnosis_id: str) -> bool:
    """把诊断回写到告警（C 的 Q13 联动）。

    返回是否成功。告警不存在时返回 False 而不是抛错 ——
    **诊断本身已成功落库，不该因为它而回滚**。
    """
    alert = session.get(Alert, alert_id)
    if alert is None:
        return False
    alert.diagnosis_id = diagnosis_id
    session.flush()
    return True


__all__ = [
    "DEFAULT_WINDOW_AFTER",
    "DEFAULT_WINDOW_BEFORE",
    "MAX_CONTEXT_EVENTS",
    "MAX_GRAPH_DEPTH",
    "build_context",
    "latest_diagnoses_per_owner",
    "latest_diagnosis_for_resource",
    "link_to_alert",
    "query_diagnoses",
    "resolve_trigger",
    "root_cause_counts",
    "run_diagnosis",
]
