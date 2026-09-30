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

from app.alerts.repository import query_alerts
from app.diagnosis import Diagnosis, DiagnosisContext, RuleSet, TimeWindow, diagnose
from app.diagnosis.rules import build_default_rule_set
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

#: 单次诊断最多纳入的告警数。与事件同理设上限。
MAX_CONTEXT_ALERTS = 500

#: 资源图遍历深度上限。链路最长是 HOST→GPU→vGPU→VM→CONTAINER→AI_SERVICE→AGENT，
#: 8 层留有余量。
MAX_GRAPH_DEPTH = 8


def build_context(
    session: Session,
    *,
    anchor_resource_id: str,
    window: TimeWindow,
    rule_set: RuleSet | None = None,
    resource_scope: set[str] | None = None,
) -> DiagnosisContext:
    """从数据库装配诊断上下文。

    资源图只取锚点周围（`max_depth` 限制），事件按时间窗过滤 ——
    两者都刻意设上限，避免"一次诊断扫全库"。

    `resource_scope` 进一步把事件限制在一组资源上。由告警触发时传入
    **该告警所在资源的可达闭包**（自身 + 祖先 + 后代）：

    - 只传"告警采纳的事件 id"太窄 —— 容器 OOM 告警只引用 `container.oom_killed`，
      那样就看不到 AI 服务上的 `inference.error`，跨层关联被关掉了；
    - 完全不限制又太宽 —— 同一批里若有多条不同故障的告警，每条诊断都会吃到
      全部事件，产出内容相同的多条结论，把诊断列表变成噪声。
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

    if resource_scope is not None:
        # 再与可达范围取交集：`resource_scope` 是调用方给的"这次只看这些资源"，
        # 而 `reachable` 是锚点的可达闭包，两者相交才是既相关又聚焦的事件集。
        events = [e for e in events if e.resource_id in resource_scope]

    evidence = tuple(item for e in events for item in _event_evidence(e))

    # ---- 告警也是证据，而且质量更高 ----
    #
    # 告警已经过聚合与分级："R-CTR-OOM-010 触发了 3 次"比三条零散的
    # `container.oom_killed` 事件更能说明容器确实被 OOM 杀了。
    # `EvidenceKind.ALERT` 从一开始就在类型系统里，但此前从未被填充 ——
    # 也就是说任何以告警为证据的规则都**永远不可能命中**。
    alert_evidence = tuple(
        Evidence(
            kind=EvidenceKind.ALERT,
            # 规则按**告警规则 id** 匹配（见 rules.py 的"匹配名"说明）
            name=a.rule_id,
            at=a.last_fired_at,
            resource_id=a.resource_id,
            value=str(a.count),
            count=a.count,
            # 告警的证据是它采纳的那些事件，诊断要能顺着追下去
            event_ids=tuple(a.evidence_event_ids or ()),
        )
        for a in query_alerts(
            session,
            limit=MAX_CONTEXT_ALERTS,
            fired_from=window.start,
            fired_to=window.end,
            resource_ids=sorted(reachable),
        )[0]
    )

    return DiagnosisContext(
        anchor_resource_id=anchor_resource_id,
        window=window,
        resources=resources,
        edges=diagnosis_edges,
        evidence=evidence + alert_evidence,
        rule_set=rule_set or build_default_rule_set(),
    )


def _event_evidence(event: Any) -> list[Evidence]:
    """把一个事件展开成若干条证据。

    **两条以上**：事件类型本身一条，每个数值型指标各一条。

    | 证据 | `name` | 命中什么规则 |
    |---|---|---|
    | 事件 | 事件 `type`（如 `container.oom_killed`） | 事件型规则（"发生了什么"） |
    | 指标 | 指标名（如 `container_memory_usage_ratio`） | 指标型规则（"量到了多少"） |

    为什么必须这样拆：引擎的 `_matches` 用 `name` 匹配，而早期实现让所有证据的
    `name` 都是事件类型、`value` 只取 `metrics["value"]` —— 于是**任何按指标名
    匹配的规则都永远不可能命中**，包括 GPU 显存阈值与容器内存比例。
    这类规则当时看起来"已覆盖"，实际是死数据。

    两者共用同一组 `eventIds`，所以任一条都能追回原始事件。
    """
    items: list[Evidence] = [
        Evidence(
            kind=EvidenceKind.EVENT,
            name=event.type,
            at=event.occurred_at,
            resource_id=event.resource_id,
            value=_evidence_value(event),
            count=_evidence_count(event),
            event_ids=(event.id,),
        )
    ]

    for key, raw in (event.metrics or {}).items():
        number = _as_number(raw)
        if number is None:
            # 非数值型指标（dict / 列表 / 纯标签）不是"测量结果"，
            # 不能硬转成数字参与比较
            continue
        items.append(
            Evidence(
                kind=EvidenceKind.METRIC,
                name=str(key),
                at=event.occurred_at,
                resource_id=event.resource_id,
                value=str(number),
                event_ids=(event.id,),
            )
        )
    return items


def _as_number(raw: Any) -> float | None:
    """把指标值转成数字；转不了就返回 `None`（**不猜、不默认为 0**）。

    容忍探针可能上报的 `"98%"` 这类带百分号的字符串。返回 `None` 而不是 0：
    0 是一个合法测量值，用它表示"没能解析"会让规则把缺失当成"读数为零"。
    """
    if isinstance(raw, bool):
        # bool 是 int 的子类，但 `True` 不是一次测量
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.rstrip("%").strip())
        except ValueError:
            return None
    return None


def _evidence_value(event: Any) -> str | None:
    """事件型证据的展示值：优先 `metrics.value`，否则不用（返回 `None`）。

    不在缺失时退回 `message`：`message` 是给人看的描述文本，把它塞进
    `value` 会让比较型规则对着一段中文做 `float()` 并静默不匹配 ——
    看起来像"规则没生效"，实际是数据里根本没有可比较的量。
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
    resource_scope: set[str] | None = None,
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
        resource_scope=resource_scope,
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
        duration_ms=duration_ms,
    )
    session.add(row)
    session.flush()

    return row, duration_ms


def _assert_confidence_reproducible(result: Diagnosis) -> None:
    """自检：`confidence` 必须等于 breakdown 贡献之和（允许浮点误差）。

    这是 D-036「可复算」的落地。若引擎将来改动破坏了可复算性，
    这里会立刻失败，而不是让用户拿到一个无法解释的数字。

    **无条件校验。** 这里此前是"引擎改写过分数的就跳过" —— 于是降权与裁剪
    这两种最需要解释的情况恰恰没有校验，线上出现过「明细只有 +0.55，总分却是
    0.35」的观感缺陷。现在引擎把每一次调整都写成 `__…__` 明细行
    （见 `app/diagnosis/engine.py` 的 `_adjustment`），所以等式在任何情况下都
    必须成立；不成立即为引擎缺陷。
    """
    total = sum(h.contribution for h in result.confidence_breakdown)
    if abs(total - result.confidence) > 1e-6:
        raise AssertionError(
            f"confidence 不可复算：breakdown 之和={total}，confidence={result.confidence}。"
            "这是引擎缺陷，拒绝落库。"
        )


def _directed_walk(session: Session, resource_id: str) -> tuple[set[str], dict[str, str]]:
    """返回 `(自身 + 全部后代, 资源 id → 父 id)`。

    **只沿有向关系走。** `load_graph` 是无向闭包：从根向上走到 VM 之后会再向下
    走进 VM 下的**其他**分支，于是两个兄弟容器会互相出现在对方的结果里。
    这个坑踩了两次（`resource_scope_for` 与 `descendant_scope_for` 各一次），
    因此把遍历收在这一处，两个调用方共用 —— 分开写就一定会有一条漏改。
    """
    nodes, edges = load_graph(session, root_id=resource_id, max_depth=MAX_GRAPH_DEPTH)
    parent_map = {r.id: r.parent_id for r in nodes if r.parent_id}

    child_index: dict[str, list[str]] = {}
    for edge in edges:
        child_index.setdefault(edge.parent_id, []).append(edge.child_id)

    scope = {resource_id}
    frontier = [resource_id]
    depth = 0
    while frontier and depth < MAX_GRAPH_DEPTH:
        nxt: list[str] = []
        for node in frontier:
            for child in child_index.get(node, ()):
                if child in scope:
                    continue
                scope.add(child)
                nxt.append(child)
        frontier = nxt
        depth += 1

    return scope, parent_map


def _ancestors_from(parent_map: dict[str, str], resource_id: str) -> list[str]:
    """沿父链向上收集祖先（含环保护）。"""
    out: list[str] = []
    seen = {resource_id}
    current = resource_id
    depth = 0
    while depth < MAX_GRAPH_DEPTH:
        parent = parent_map.get(current)
        if parent is None or parent in seen:
            break
        seen.add(parent)
        out.append(parent)
        current = parent
        depth += 1
    return out


def resource_scope_for(session: Session, resource_id: str) -> set[str]:
    """某资源的**有向可达闭包**：自身 + 全部祖先 + 全部后代。

    用于"这次诊断只看与这条告警相关的资源"。根因常在**祖先**方向（GPU 在容器之上），
    影响在**后代**方向（服务在容器之下），只看单点会让跨层关联失效。

    **不能用 `load_graph(root_id=...)` 代替**：那是**无向**闭包 —— 它从根向上走到
    VM 之后会再向下走进 VM 下的**其他**分支，于是两个兄弟容器会互相出现在对方的
    范围里（实测：容器 A 的范围含容器 B，反之亦然）。后果是两起独立事故被互相
    污染，一次诊断能引用另一起的证据。

    正确做法是沿**有向**关系走：自身 + 父链（祖先）+ 子索引（后代）。

    > 这个缺陷只有在存在**第二个兄弟节点**时才会显现。此前所有测试都只有一条链，
    > 而单链上"无向连通分量"与"有向可达闭包"恰好是同一个集合。
    """
    scope, parent_map = _directed_walk(session, resource_id)
    scope.update(_ancestors_from(parent_map, resource_id))
    return scope


def descendant_scope_for(session: Session, resource_id: str) -> set[str]:
    """某资源**自身 + 全部后代**（不含祖先）。

    与 `resource_scope_for` 的区别是方向：那个是完整闭包（含祖先），用于"哪些证据
    可以解释它"；这个是向下闭包，用于判断"这次事故是否已经有结论"。

    两者混用会造成不同故障共享同一条结论 —— 见 `diagnosis/auto._diagnosed_alerts_for`。
    """
    scope, _parent_map = _directed_walk(session, resource_id)
    return scope


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


def root_cause_counts(session: Session, *, created_from: datetime | None = None) -> dict[str, int]:
    """按根因码统计诊断数量（前端"根因 Top N"）。"""
    stmt = select(DiagnosisRow.root_cause, func.count()).group_by(DiagnosisRow.root_cause)
    if created_from is not None:
        stmt = stmt.where(DiagnosisRow.created_at >= created_from)
    rows = session.execute(stmt).all()
    return {str(rc): int(n) for rc, n in rows}


#: 共享基础设施的资源类型。这些类型上出的问题影响**坐在它们上面的全部工作负载**，
#: 而不是某一个。取自 `DATA_MODEL.md` §2.2 的链路
#: （宿主机 → GPU/vGPU → VM → 容器/进程 → AI 服务）。
#:
#: 新增类型时**默认不在此列**（即视为工作负载自有）—— 宁可把"共享"判成"自有"，
#: 也不要把真实的自身故障悄悄降级成"共享基础设施问题"。
SHARED_INFRA_KINDS: frozenset[str] = frozenset({"host", "gpu", "vgpu", "vm"})


def _shared_infra_ids(resource_ids: set[str]) -> set[str]:
    """从一组资源 ID 里挑出属于共享基础设施的那些。"""
    return {
        resource_id
        for resource_id in resource_ids
        if resource_id.split(":", 1)[0] in SHARED_INFRA_KINDS
    }


def latest_diagnoses_per_owner(
    session: Session, owners: dict[str, list[str]]
) -> dict[str, tuple[DiagnosisRow, int]]:
    """按"归属关系"取每个工作负载最近一次诊断。

    `owners` 是 `资源 ID → 拥有它的工作负载 ID 列表` 的映射（由
    `app.api.workloads._resolve_owners` 沿父链算出，**一对多**）。

    **必须先做这一步归并，不能要求诊断恰好点名工作负载**：

      "容器的 GPU 显存耗尽"这条诊断的 affected 里是容器，链路是
      HOST→GPU→vGPU→VM→CTR→AIS，AI 服务从头到尾没被提及 —— 但它恰恰是
      **受影响的那一方**。若只认"诊断提到了工作负载 ID"，链路下层的诊断在
      业务视图里就全部不可见，工作负载总览也就失去了意义。

    **证据是归属的前提。** `_rank` 早先把 `potentially_affected` 也算作"贴近"，
    而那一档按定义就是"结构可达但**无证据**"。后果是可达性被当成了证据：一条证据
    全在 A 工作负载平面上的诊断，会因为共享祖先而对 B、C、D 同样"贴近"，并列时按
    `-created_at` 任意择一 —— 实测 `demo-svc-healthy`（健康场景）被挂上了
    `GPU_MEMORY_EXHAUSTED`，而"自己超配 vs 邻居干扰"这两个本该区分的场景在同一列里
    长得一模一样。

    现在：**没有任何相关证据的诊断不参与归属**（与诊断引擎自身的红线一致 ——
    无证据不得给根因）。可达但无证据 ⇒ 不归属，而不是勉强归属。

    返回 `{owner: (row, rank)}`，`rank` 让调用方区分"这就是你的故障"（0/1）与
    "你所在的共享基础设施有问题"（2）。**这两种不该在界面上长得一样。**

    实现：一次按 `created_at DESC` 的扫描，内存里对每个工作负载只保留最优的一条。
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

    # 注：这里**不再**计算 `own_plane`（自身 + 后代）与 `shared_infra`。
    # 归属所需的两个方向分别由下面的 `owns`（拥有，向下）与 `reachable`
    # （依赖，向上+向下）回答；早先的 own_plane 只有"自身 + 后代"这一个方向，
    # 无法回答"容器上的证据是否属于这个工作负载"（容器是它的祖先）。

    # 注：这里**不需要** `owns`（自身 + 后代）。归属用 `owners`（资源 → 归属它的
    # 工作负载，沿父链向上）来判"谁拥有它" —— 这是唯一能同时正确涵盖"我自己的容器"
    # 与"共享的 vGPU"的方向；而"是不是共享层"由资源类型回答（见 `shared_infra`）。
    # 三个方案各错一次的经过记在 `_rank` 的文档字符串里。

    #: 本次候选资源里属于共享基础设施的那些（宿主 / GPU / vGPU / VM）。
    #: 共享与否是**资源类型**的性质：这些层是多个工作负载共同坐在上面的。
    shared_infra: set[str] = _shared_infra_ids(set(all_resources))

    #: 工作负载**依赖**的资源（自身 + 祖先 + 后代）。用于回答"它是否受影响"。
    #: 两个方向分开，是因为它们回答两个不同的问题 —— 混用会把共享基础设施上的
    #: 故障误报成每个工作负载的自身故障（实测）。
    reachable: dict[str, set[str]] = {
        owner: resource_scope_for(session, owner) for owner in wanted_owners
    }

    rows = list(session.execute(stmt).scalars())

    #: 结论真正依赖的资源。**这才是"是不是自己的故障"的判据**，而不是 `affected`。
    #:
    #: 实测过两次教训：`affected_resources` 是**影响闭包**，会沿链向下把每个工作负载
    #: 自己的容器 / Agent / AI 服务都收进来。拿它判归属，会让同一块卡上的**全部**
    #: 工作负载（含健康场景）都被标成"自身故障"。结论依据的是 `evidence`，归属也应当看它。
    def _evidence_ids(row: DiagnosisRow) -> set[str]:
        return {str(item["resourceId"]) for item in (row.evidence or []) if item.get("resourceId")}

    def _rank(row: DiagnosisRow, owner: str) -> int | None:
        """这条结论对该工作负载的"贴近程度"；**不适用返回 `None`**。

        | rank | 条件 | 界面语义 |
        |---|---|---|
        | `0` | 证据点名了工作负载自身 | 你自己的故障 |
        | `1` | 证据在**归属于它**的资源上（容器 / 进程 / 它自己的 Agent），且该资源不是共享基础设施 | 你自己的故障 |
        | `2` | 证据落在**共享基础设施**（宿主 / GPU / vGPU / VM）上，而它坐在上面 | **"你所在的机器/卡有问题"** |
        | `None` | 无证据、或与本工作负载无关 | 不显示根因 |

        **这条函数被实测纠正过三次**，把结论记在这里免得再走回头路：

        1. 用 `affected_resources` 判归属 —— 那是**影响闭包**，会沿链向下把每个工作
           负载自己的容器/Agent 都收进来，于是同一块卡上的全部服务（含健康场景）
           都被标成"自身故障"。归属要看 `evidence`。
        2. 用 `owners` 判"拥有" —— `owners` 是沿父链**向上**建的，共享的 vGPU 因此
           被归给下面**全部五个**服务，于是每个都拿到 rank 1。那是在量"依赖"。
        3. 用 `_directed_walk(owner)`（自身 + **后代**）判"拥有" —— 但 AI 服务在链路
           **末端**，容器是它的**父**，根本不在"后代"里，于是它对自己的容器 OOM
           只拿到 rank 2，输给了无关的 GPU 诊断。

        最终：**"谁拥有它"用 `owners`（向上）回答**，这是唯一能同时正确涵盖
        "我自己的容器"与"共享的 vGPU"的方向；**"是不是共享层"用资源类型回答** ——
        共享与否是类型的性质，不是归属关系的性质。

        共享基础设施上的证据一律 rank 2：一张 GPU 卡满时它上面的每个工作负载都受影响，
        但**没有一个是"自己配错了"**。
        """
        evidence = _evidence_ids(row)
        if not evidence:
            # 无证据的结论不参与归属（与诊断引擎"无证据不得给根因"同一红线）
            return None

        if owner in evidence:
            return 0

        # 证据落在"归属于这个工作负载"的资源上？共享层不算 —— 共享故障属于所有人，
        # 也就不属于任何一个。
        for resource_id in evidence:
            if resource_id in shared_infra:
                continue
            if owner in owners.get(resource_id, ()):
                return 1

        # 只在共享基础设施上相关（它坐着的机器 / 卡）⇒ 共享级结论。
        #
        # **不要在这里并上 `all_resources`。** 曾经那样写过（想兜住"万一 reachable
        # 没覆盖到"），后果是**任何**诊断都能匹配**任何**工作负载 —— 因为每条诊断的
        # 证据集里都含一个共享层资源，于是网络诊断也匹配到了 GPU 场景的服务：
        #     svc-gpu-self rank=2 NETWORK_UNREACHABLE   ← 实测
        # `reachable`（自身 + 祖先 + 后代）本身就回答了"这条结论可能与它有关吗"，
        # 对同一张卡上的每个工作负载都包含那块 vGPU，因此不需要兜底。
        if evidence & reachable.get(owner, {owner}):
            return 2
        return None

    # 一次性算好每个工作负载的排序键 —— 不用"迭代中逐步替换"的写法：
    # 那种写法要求平面集合、自身节点、并列取舍三件事同时正确，任一处错就会静默
    # 退回"所有卡片都显示最新一条"（实测错了两次）。
    # 排序键是**全序**：`(rank, -created, id)`。
    #
    # 加 id 不是凑数：实测所有演示诊断的 `created_at` **完全相同**（场景时间基准是
    # 同一个时刻），于是 `(rank, -created)` 无法区分它们，Python 会保留**先遇到**的
    # 那条 —— 一条无关的 rank 2 结论因此可能压过工作负载**自己的** rank 1 结论
    # （实测：`demo-svc-oom` 显示成了 GPU 耗尽，而它自己的容器 OOM 结论被丢掉）。
    #
    # `diag_<ULID>` 的 id 是字典序即时间序，因此并列时"更早产生的结论优先"，
    # 确定且可解释，不依赖行顺序。
    best: dict[str, tuple[int, float, str]] = {}
    chosen: dict[str, tuple[DiagnosisRow, int]] = {}
    for row in rows:
        created = row.created_at.timestamp()
        for owner in wanted_owners:
            rank = _rank(row, owner)
            if rank is None:
                continue
            key = (rank, -created, row.id)
            if owner not in best or key < best[owner]:
                best[owner] = key
                chosen[owner] = (row, rank)

    return chosen


def latest_diagnosis_for_resource(session: Session, resource_id: str) -> DiagnosisRow | None:
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
    "MAX_CONTEXT_ALERTS",
    "MAX_CONTEXT_EVENTS",
    "MAX_GRAPH_DEPTH",
    "build_context",
    "latest_diagnoses_per_owner",
    "latest_diagnosis_for_resource",
    "link_to_alert",
    "descendant_scope_for",
    "query_diagnoses",
    "resolve_trigger",
    "resource_scope_for",
    "root_cause_counts",
    "run_diagnosis",
]
