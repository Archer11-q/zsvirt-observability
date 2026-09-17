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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.diagnosis import service as diagnosis_service
from app.enums import AlertState, Severity
from app.models import Alert

#: 单次联动最多执行的诊断数。防止一次大批量上报触发无上限的图遍历。
DEFAULT_MAX_DIAGNOSES = 10

#: 只对达到该级别的活动告警自动诊断。
#:
#: `info` / `warning` 级告警**不自动诊断**：它们多数是趋势提示（利用率偏高、
#: 重启一次），自动跑根因分析只会产生一堆 `UNKNOWN` —— 而 `UNKNOWN` 结论太多
#: 会让运维学会忽略整个诊断列表。让用户按需点"一键诊断"更诚实。
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
        a.id: a
        for a in session.execute(select(Alert).where(Alert.id.in_(alert_ids))).scalars()
    }

    threshold = SEVERITY_RANK.get(min_severity, 0)

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
        if len(result.attempted) >= max_diagnoses:
            result.truncated = True
            continue

        result.attempted.append(alert_id)
        try:
            linked_id = _diagnose_one(session, alert, now=moment)
        except Exception as exc:  # noqa: BLE001 - 诊断失败绝不能影响上报
            # 回滚到本次尝试之前的干净状态：部分装配可能已经污染了 session。
            session.rollback()
            result.failed[alert_id] = f"{type(exc).__name__}: {exc}"
            continue
        if linked_id is not None:
            result.linked[alert_id] = linked_id

    return result


def _diagnose_one(session: Session, alert: Alert, *, now: datetime) -> str | None:
    """诊断单条告警并回写。返回 `diagnosisId`，参数不合法时返回 `None`。"""
    resolved = diagnosis_service.resolve_trigger(
        session,
        alert_id=alert.id,
        anchor_resource_id=None,
        window=None,
        now=now,
    )
    if isinstance(resolved, str):
        # 锚点资源不存在（告警引用的资源被清理过）→ 不诊断，但也不报错
        return None

    anchor_resource_id, window, trigger = resolved
    row, _duration_ms = diagnosis_service.run_diagnosis(
        session,
        anchor_resource_id=anchor_resource_id,
        window=window,
        trigger=trigger,
        now=now,
    )
    alert.diagnosis_id = row.id
    session.flush()
    return row.id


__all__ = [
    "DEFAULT_MAX_DIAGNOSES",
    "DEFAULT_MIN_SEVERITY",
    "AutoDiagnosisResult",
    "run_auto_diagnosis",
]
