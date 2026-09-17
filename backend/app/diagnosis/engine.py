"""诊断引擎的五步流程实现。

    1. 触发   → 构造 DiagnosisContext
    2. 汇聚   → 取上下游资源
    3. 关联   → 证据落入时间窗并归并
    4. 匹配   → 逐条规则求值，全部留痕
    5. 评分   → 加权求和，裁剪到 [0,1]，产出 Diagnosis

规则（docs/backend/DIAGNOSIS_DESIGN.md §4）：
    confidence = clamp(Σ contribution, 0, 1)
    最高分 < 0.30      → 强制 UNKNOWN
    无证据             → 强制 UNKNOWN
    模拟证据需标记     → 诚实性要求
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.diagnosis.types import (
    Diagnosis,
    DiagnosisContext,
    Evidence,
    Recommendation,
    Rule,
    RuleHit,
)

UNKNOWN_ROOT_CAUSE = "UNKNOWN"
MIN_CONFIDENCE = 0.30

# 根因 → 处置建议的映射（码表见 docs/CONTRACT_FREEZE.md F-03）
RECOMMENDATIONS: dict[str, tuple[Recommendation, ...]] = {
    "GPU_MEMORY_EXHAUSTED": (
        Recommendation("REDUCE_CONCURRENCY", "降低推理并发或 batch size"),
        Recommendation("CHECK_GPU_ALLOCATION", "检查该虚拟机的 vGPU 分配与配额"),
    ),
    "GPU_NEIGHBOR_CONTENTION": (
        Recommendation("CHECK_NEIGHBOR_WORKLOADS", "检查同一宿主机上其他虚拟机的 GPU 占用"),
        Recommendation("CHECK_GPU_ALLOCATION", "检查该虚拟机的 vGPU 分配与配额"),
    ),
    "GPU_UTILIZATION_SATURATED": (
        Recommendation("REDUCE_CONCURRENCY", "降低推理并发或 batch size"),
    ),
    "CONTAINER_MEMORY_LIMIT": (
        Recommendation("INCREASE_CONTAINER_MEMORY", "提高容器内存限额或优化模型内存占用"),
    ),
    "CONTAINER_RESTART_LOOP": (
        Recommendation("CHECK_CONTAINER_RESTART", "检查容器退出码与重启日志"),
    ),
    "VM_MEMORY_EXHAUSTED": (Recommendation("INCREASE_VM_MEMORY", "为虚拟机扩容内存"),),
    "VM_DISK_IO_SATURATED": (
        Recommendation("LIMIT_IO_NOISY_PROCESS", "限制同机 I/O 干扰进程"),
        Recommendation("SEPARATE_LOG_DISK", "将日志写入独立磁盘"),
    ),
    "NETWORK_UNREACHABLE": (
        Recommendation("CHECK_NETWORK_POLICY", "检查容器/虚拟机网络策略与安全组"),
        Recommendation("CHECK_DNS", "检查 DNS 解析是否正常"),
        Recommendation("CHECK_DEPENDENCY_HEALTH", "检查被调用服务是否可用"),
    ),
    "AGENT_TASK_FAILURE": (
        Recommendation("REVIEW_AGENT_RETRY_POLICY", "检查智能体重试与超时配置"),
        Recommendation("CHECK_DEPENDENCY_HEALTH", "检查被调用服务是否可用"),
    ),
    "MODEL_COMPUTE_BOUND": (Recommendation("PROFILE_MODEL", "对模型做性能分析，评估算力需求"),),
    UNKNOWN_ROOT_CAUSE: (
        Recommendation("COLLECT_MORE_EVIDENCE", "证据不足，建议扩大时间窗或补采集"),
    ),
}


def _matches(rule: Rule, ev: Evidence, resource_kind: str | None) -> bool:
    """单条规则对单条证据求值。"""
    if rule.evidence_kind is not ev.kind:
        return False
    if rule.match_name and not ev.name.startswith(rule.match_name):
        return False
    if rule.resource_kinds and (resource_kind or "") not in rule.resource_kinds:
        return False
    if rule.requires_source is not None and ev.source is not rule.requires_source:
        return False
    if rule.min_count is not None and (ev.count or 0) < rule.min_count:
        return False
    if rule.comparator is not None and rule.threshold is not None:
        try:
            actual = float(str(ev.value).rstrip("%"))
        except (TypeError, ValueError):
            return False
        if rule.comparator == ">=" and not actual >= rule.threshold:
            return False
        if rule.comparator == ">" and not actual > rule.threshold:
            return False
        if rule.comparator == "<=" and not actual <= rule.threshold:
            return False
        if rule.comparator == "<" and not actual < rule.threshold:
            return False
    return True


def _score(hits: list[RuleHit]) -> tuple[float, list[str]]:
    """对同一 root_cause 的命中求和并裁剪。返回 (confidence, 备注)。"""
    notes: list[str] = []
    total = sum(h.contribution for h in hits)
    if total < 0.0:
        total = 0.0
    if total > 1.0:
        notes.append("confidence 裁剪到 1.0")
        total = 1.0
    return round(total, 4), notes


def diagnose(ctx: DiagnosisContext, now: datetime | None = None) -> Diagnosis:
    """执行一次诊断。纯函数：同一输入 + 同一 rule_set_version → 同一输出。"""
    created_at = now or datetime.now(UTC)
    notes: list[str] = []

    # ---- 步骤 2：汇聚（锚点及其上下游）----
    reachable = {ctx.anchor_resource_id}
    reachable.update(ctx.ancestors(ctx.anchor_resource_id))
    reachable.update(ctx.descendants(ctx.anchor_resource_id))

    # ---- 步骤 3：关联（证据落入时间窗 + 资源范围）----
    window = ctx.window.with_tolerance(0)
    relevant: list[Evidence] = [
        ev for ev in ctx.evidence if ev.resource_id in reachable and window.contains(ev.at)
    ]

    if any(ev.is_simulated for ev in relevant):
        notes.append("本次诊断包含模拟数据来源的证据，已在 evidence.source 中标注")

    if not relevant:
        notes.append("时间窗内无证据，按红线返回 UNKNOWN")
        return Diagnosis(
            id="",
            created_at=created_at,
            trigger={"anchorResourceId": ctx.anchor_resource_id},
            root_cause=UNKNOWN_ROOT_CAUSE,
            confidence=0.0,
            confidence_breakdown=(),
            evidence=(),
            recommendation=RECOMMENDATIONS[UNKNOWN_ROOT_CAUSE],
            rule_set_version=ctx.rule_set.version,
            notes=tuple(notes),
        )

    # ---- 步骤 4：匹配（全部留痕）----
    by_cause: dict[str, list[RuleHit]] = {}
    for rule in ctx.rule_set.rules:
        for ev in relevant:
            res = ctx.resources.get(ev.resource_id)
            if _matches(rule, ev, res.kind if res else None):
                observed = rule.observed_template.format(
                    value=ev.value if ev.value is not None else ev.count
                )
                hit = RuleHit(
                    rule_id=rule.id,
                    root_cause=rule.root_cause,
                    contribution=rule.contribution,
                    observed=observed,
                    evidence_refs=ev.event_ids,
                )
                by_cause.setdefault(rule.root_cause, []).append(hit)
    if not by_cause:
        notes.append("规则集无匹配，按红线返回 UNKNOWN（不编造根因）")
        return Diagnosis(
            id="",
            created_at=created_at,
            trigger={"anchorResourceId": ctx.anchor_resource_id},
            root_cause=UNKNOWN_ROOT_CAUSE,
            confidence=0.0,
            confidence_breakdown=(),
            evidence=tuple(relevant),
            recommendation=RECOMMENDATIONS[UNKNOWN_ROOT_CAUSE],
            rule_set_version=ctx.rule_set.version,
            notes=tuple(notes),
        )

    # ---- 步骤 5：评分 ----
    scored = {cause: _score(hits) for cause, hits in by_cause.items()}
    ranked = sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True)
    top_cause, (top_conf, top_notes) = ranked[0]
    notes.extend(top_notes)

    # 冲突降权：另一候选得分接近时双方都降权
    if len(ranked) > 1:
        second_cause, (second_conf, _) = ranked[1]
        if abs(top_conf - second_conf) < 0.15:
            penalty = 0.20
            top_conf = round(max(0.0, top_conf - penalty), 4)
            notes.append(
                f"候选冲突：{top_cause} 与 {second_cause} 得分接近"
                f"（{top_conf + penalty} vs {second_conf}），双方降权 {penalty}"
            )

    if top_conf < MIN_CONFIDENCE:
        notes.append(f"最高分 {top_conf} < {MIN_CONFIDENCE}，强制返回 UNKNOWN")
        return Diagnosis(
            id="",
            created_at=created_at,
            trigger={"anchorResourceId": ctx.anchor_resource_id},
            root_cause=UNKNOWN_ROOT_CAUSE,
            confidence=top_conf,
            confidence_breakdown=tuple(by_cause[top_cause]),
            evidence=tuple(relevant),
            recommendation=RECOMMENDATIONS[UNKNOWN_ROOT_CAUSE],
            rule_set_version=ctx.rule_set.version,
            notes=tuple(notes),
        )

    # ---- 影响范围 ----
    # 复用 app.graph.algorithms.impact_scope —— 与资源图（L3）同一实现，
    # 避免"引擎算出的影响范围"与"拓扑接口给出的影响范围"不一致。
    #
    # 规则（docs/backend/DIAGNOSIS_DESIGN.md §6）：
    #   1. affected 必须包含**有证据支持**的资源；
    #   2. 若产出该结论的规则标记了 propagate，则额外包含证据链上缺环的中间层
    #      —— 它们确实参与了这条链，而不是"结构上碰巧在下游"；
    #   3. 其余结构相关但无证据的资源进 potentially_affected，与前者分开。
    #   4. 证据的**上游**资源（如证据为 GPU 时的宿主机）不算受影响 ——
    #      它是提供证据的层，不是被影响的对象。
    evidence_resources = {ev.resource_id for ev in relevant}
    propagating_causes = {r.root_cause for r in ctx.rule_set.rules if r.propagate}
    scope = ctx.impact_scope(evidence_resources, propagate=top_cause in propagating_causes)

    return Diagnosis(
        id="",
        created_at=created_at,
        trigger={"anchorResourceId": ctx.anchor_resource_id},
        root_cause=top_cause,
        confidence=top_conf,
        confidence_breakdown=tuple(by_cause[top_cause]),
        affected_resources=scope.affected,
        potentially_affected=scope.potentially_affected,
        evidence=tuple(relevant),
        recommendation=RECOMMENDATIONS.get(top_cause, ()),
        rule_set_version=ctx.rule_set.version,
        notes=tuple(notes),
    )
