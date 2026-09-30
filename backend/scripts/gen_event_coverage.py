"""见 docs/EVENT_COVERAGE_MATRIX.md —— 从代码生成事件覆盖矩阵。

**不要手工编辑那张表**：它由本脚本生成，手改会在下次生成时被覆盖，
而"手工维护导致漂移"正是这张表要消除的东西。

用法（在 backend/ 下）：
    PYTHONPATH=. .venv/bin/python scripts/gen_event_coverage.py
"""

from __future__ import annotations

from app.alerts.rules import build_default_rule_set
from app.diagnosis.rules import build_default_rule_set as build_diag_rules
from app.enums import EVENT_TYPE_LABELS

# 告警规则：`AlertRule.condition.event_type` 直接给出事件类型
alert_by_type: dict[str, list[str]] = {}
for rule in build_default_rule_set().rules:
    event_type = getattr(rule.condition, "event_type", None)
    if event_type:
        alert_by_type.setdefault(event_type, []).append(rule.id)

# 诊断规则**不是**用 `event_types` 声明的 —— 它用
# `evidence_kind`（event / metric / alert）+ `match_name`（事件类型名或指标名）。
# 早先按不存在的 `event_types` 字段取，整列空白 —— 那会谎称"没有任何事件类型参与诊断"，
# 比没有这张表更糟。
diag_by_type: dict[str, list[str]] = {}
for rule in build_diag_rules().rules:
    if str(getattr(rule, "evidence_kind", "")).lower() != "event":
        continue
    match_name = getattr(rule, "match_name", None)
    if match_name in EVENT_TYPE_LABELS:
        diag_by_type.setdefault(match_name, []).append(rule.id)

for event_type in sorted(EVENT_TYPE_LABELS):
    print(
        "|".join(
            [
                event_type,
                EVENT_TYPE_LABELS[event_type],
                ",".join(sorted(alert_by_type.get(event_type, ()))) or "—",
                ",".join(sorted(diag_by_type.get(event_type, ()))) or "—",
            ]
        )
    )
