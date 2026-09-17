"""诊断引擎（L4）。

设计见 docs/backend/DIAGNOSIS_DESIGN.md。三条边界约束：

1. 不访问数据库、不发 HTTP —— 只接收装配好的 DiagnosisContext 纯数据对象；
2. 规则是声明式数据，不是代码分支；
3. 资源图只读传入，引擎无副作用。
"""

from app.diagnosis.engine import RECOMMENDATIONS, UNKNOWN_ROOT_CAUSE, diagnose
from app.diagnosis.types import (
    Diagnosis,
    DiagnosisContext,
    Edge,
    Evidence,
    EvidenceKind,
    EvidenceSource,
    Recommendation,
    Resource,
    Rule,
    RuleHit,
    RuleSet,
    Severity,
    TimeWindow,
)

__all__ = [
    "RECOMMENDATIONS",
    "UNKNOWN_ROOT_CAUSE",
    "Diagnosis",
    "DiagnosisContext",
    "Edge",
    "Evidence",
    "EvidenceKind",
    "EvidenceSource",
    "Recommendation",
    "Resource",
    "Rule",
    "RuleHit",
    "RuleSet",
    "Severity",
    "TimeWindow",
    "diagnose",
]
