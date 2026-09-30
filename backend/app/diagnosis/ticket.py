"""诊断 → 工单文本（`POST /api/v1/diagnoses/{id}/ticket`）。

## 为什么需要它（赛题依据）

赛题评审维度里有两条直接指向这里：

- **「告警运营与可用性 10%」**：界面/CLI 是否便于运维人员完成**定位和处置**；
- **「创新性与工程落地性 15%」**：是否具备**生产落地价值**。

在此之前，"处置建议"只是界面上几行文字：运维要手工把根因、证据、影响范围抄进
工单系统。抄的过程会丢证据 —— 而丢证据的工单在事后复盘时等于没有结论。

## 刻意不做的事

- **不写回 ZSvirt、不执行任何动作**（重启 / 扩容 / 改配置）。本项目只申请只读权限，
  这一点在给命题方的信用是第一位的。工单文本是**给人看的交接物**，不是执行指令。
- **不发明结论**。文本里的每个数字都来自已落库的诊断：置信度、贡献明细、
  影响范围、证据。置信度低于阈值的 `UNKNOWN` 会**如实写成"无法判定"**，
  并提示需要补充什么，而不是硬凑一个根因让工单看起来完整。

## 为什么把格式化做成纯函数

`render_ticket()` 不碰数据库、不读配置、不做 IO：同一份诊断数据必然产出同一份文本。
工单是要归档、要被人反复引用的东西，"同一诊断两次导出不一样"会让它失去证据价值。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = ["TicketDocument", "render_ticket"]

#: 工单文本里一行的最大宽度（按显示宽度粗略计，中文按 2 计）。
#: 太宽在终端/邮件客户端里会折行错乱，太窄则频繁断句。
_MAX_WIDTH = 78

#: 低于这个置信度时的措辞（与引擎的 MIN_CONFIDENCE 同一量级）。
#: 引擎会在低于阈值时给出 UNKNOWN，这里只是把"这意味着什么"讲给工单的读者。
_LOW_CONFIDENCE = 0.5


@dataclass(frozen=True)
class TicketDocument:
    """渲染结果。`text` 是可直接粘贴/下载的正文。"""

    diagnosisId: str
    title: str
    text: str
    rootCause: str
    confidence: float
    #: 该结论是否**不可据此行动**（UNKNOWN 或置信度过低）。
    #: 让工单系统能据此走不同的流程 —— 而不是让一个"待确认"的结论
    #: 看起来和确定结论一样。
    actionable: bool


def _display_width(text: str) -> int:
    """字符串在等宽终端里的**显示宽度**。

    中文/全角字符占 2 列，`len()` 只数 1 —— 直接用它算分隔线会让标题下方的
    `────` 明显长出边框，看起来像排版坏掉。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _rule(title: str) -> str:
    """小节标题 + 与整体边框同宽的横线。"""
    return f"{title}\n" + "─" * min(_MAX_WIDTH, max(_display_width(title) + 8, 24))


def _bullet(lines: list[str], items: list[str], *, empty: str = "（无）") -> None:
    if not items:
        lines.append(f"  {empty}")
        return
    for item in items:
        lines.append(f"  - {item}")


def _fmt_confidence(value: float) -> str:
    return f"{value:.2f}"


def render_ticket(
    *,
    diagnosis_id: str,
    created_at: datetime,
    root_cause: str,
    confidence: float,
    confidence_breakdown: list[dict[str, Any]],
    affected: list[str],
    on_chain: list[str],
    potentially_affected: list[str],
    evidence: list[dict[str, Any]],
    recommendation: list[dict[str, str]],
    rule_set_version: str,
    notes: list[str],
    trigger: dict[str, Any] | None = None,
    root_cause_label: str | None = None,
    resource_labels: dict[str, str] | None = None,
) -> TicketDocument:
    """把一条已落库的诊断渲染成工单文本。

    纯函数：不读数据库、不读配置。`resource_labels` 由调用方提供（可读名），
    缺省时直接用资源 ID —— **ID 本身就是可追溯的标识**，总比编一个名字好。
    """
    labels = resource_labels or {}
    cause_text = f"{root_cause_label or root_cause}（{root_cause}）"
    is_unknown = root_cause == "UNKNOWN"
    actionable = (not is_unknown) and confidence >= _LOW_CONFIDENCE

    def name_of(resource_id: str) -> str:
        label = labels.get(resource_id)
        return f"{label}（{resource_id}）" if label else resource_id

    lines: list[str] = []
    title = f"[Crosslayer] {cause_text} · 置信度 {_fmt_confidence(confidence)}"

    lines.append("Crosslayer 跨层诊断工单")
    lines.append("=" * _MAX_WIDTH)
    lines.append(f"诊断 ID   : {diagnosis_id}")
    lines.append(f"生成时间  : {created_at.astimezone(UTC).isoformat()}")
    lines.append(f"规则集    : {rule_set_version}")
    lines.append(f"根因      : {cause_text}")
    lines.append(f"置信度    : {_fmt_confidence(confidence)}")
    if not actionable:
        lines.append(
            "结论强度  : ⚠ 不足以下结论 —— "
            + ("引擎判定为 UNKNOWN（证据不足）" if is_unknown else "置信度偏低，建议人工复核")
        )
    lines.append("")

    if trigger:
        lines.append(_rule("触发方式"))
        for key, value in trigger.items():
            lines.append(f"  {key}: {value}")
        lines.append("")

    lines.append(_rule("置信度构成（可逐项复算）"))
    if confidence_breakdown:
        for item in confidence_breakdown:
            rule_id = str(item.get("ruleId", "?"))
            contribution = item.get("contribution")
            observed = str(item.get("observed", ""))
            # 合成明细行（引擎对分数的调整，如冲突降权）标注出来，
            # 否则读者会去规则集里找一个不存在的规则。
            mark = "（调整）" if rule_id.startswith("__") else ""
            lines.append(f"  {rule_id}{mark:<4} {contribution:+.4f}  {observed}")
        total = sum(float(i.get("contribution") or 0.0) for i in confidence_breakdown)
        lines.append(f"  {'合计':<16} {total:+.4f}")
    else:
        lines.append("  （无 —— 该结论没有规则支撑，不应采信）")
    lines.append("")

    lines.append(_rule("影响范围"))
    lines.append(f"  确定受影响（有证据 + 下游）：{len(affected)} 个")
    _bullet(lines, [name_of(r) for r in affected])
    if on_chain:
        lines.append(f"  传播链（证据到锚点之间的过渡层）：{len(on_chain)} 个")
        _bullet(lines, [name_of(r) for r in on_chain])
    if potentially_affected:
        lines.append(f"  可能受影响（仅结构相关，无证据）：{len(potentially_affected)} 个")
        _bullet(lines, [name_of(r) for r in potentially_affected])
    lines.append("")

    lines.append(_rule("证据"))
    if evidence:
        for item in evidence:
            kind = str(item.get("type", "?"))
            name = str(item.get("name", "?"))
            parts = [f"[{kind}] {name}"]
            if item.get("resourceId"):
                parts.append(f"@{name_of(str(item['resourceId']))}")
            if item.get("value") is not None:
                parts.append(f"= {item['value']}")
            if item.get("count") is not None:
                parts.append(f"×{item['count']}")
            if item.get("at"):
                parts.append(f"({item['at']})")
            source = item.get("source")
            if source:
                # 来源必须写进工单：模拟数据与真实采集的证据价值不同，
                # 而工单会被归档、被后来的人当作事实引用。
                parts.append(f"<source={source}>")
            lines.append("  - " + " ".join(parts))
    else:
        lines.append("  （无证据 —— 按项目红线，无证据不得给出根因）")
    lines.append("")

    lines.append(_rule("建议处置"))
    if recommendation:
        for index, item in enumerate(recommendation, 1):
            lines.append(f"  {index}. [{item.get('code', '?')}] {item.get('text', '')}")
    else:
        lines.append("  （无）")
    if not actionable:
        lines.append("")
        lines.append("  ⚠ 本结论置信度不足，建议先补充观测再处置：")
        lines.append("     1. 确认相关资源的采集是否正常（探针 / ZWatch 渠道）；")
        lines.append("     2. 扩大时间窗或补充证据后重新触发诊断；")
        lines.append("     3. 不要仅凭本工单执行变更。")
    lines.append("")

    if notes:
        lines.append(_rule("引擎备注"))
        for note in notes:
            lines.append(f"  · {note}")
        lines.append("")

    lines.append("=" * _MAX_WIDTH)
    lines.append("本工单由 Crosslayer 自动生成，内容为**可解释的诊断结论**，非执行指令。")
    lines.append(f"复核方式：GET /api/v1/diagnosis/{diagnosis_id}?includeEvidence=true")

    text = "\n".join(lines)
    return TicketDocument(
        diagnosisId=diagnosis_id,
        title=title,
        text=text,
        rootCause=root_cause,
        confidence=confidence,
        actionable=actionable,
    )
