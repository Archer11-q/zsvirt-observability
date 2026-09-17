"""诊断规则集：三类赛题场景的声明式规则（填充 `RuleSet` 的内容）。

`app/diagnosis/engine.py` 是求值器，本模块是**数据**。规则改动不改代码，
新场景 = 新增数据（`DIAGNOSIS_DESIGN.md` §7）。

## 与告警规则的分工

| | 告警（`app/alerts/rules.py`） | 诊断（本模块） |
|---|---|---|
| 回答 | "哪个资源的哪类异常值得看一眼" | "根因是什么、影响谁、怎么办" |
| 产物 | `Alert`（有状态，可确认/静默） | `Diagnosis`（一次性结论 + 证据链） |
| 依据 | 单个事件是否出现 | 一组证据能否共同支持某个根因 |

两者刻意分开：告警要**灵敏**（漏报比误报贵），诊断要**保守**（错归因会让运维
去改错配置）。所以同一条故障，告警可能有三条，而诊断只给一个根因。

## 匹配名（`match_name`）用什么

引擎的 `_matches` 用 `ev.name.startswith(match_name)`：

| 证据类型 | `name` 是什么 | 规则里写什么 |
|---|---|---|
| `event` | 事件的 `type` | 事件类型字符串，如 `gpu.memory.exhausted` |
| `alert` | 告警的 `rule_id` | 告警规则 id，如 `R-CTR-OOM-010` |
| `metric` | 指标名 | 指标名，如 `gpu_memory_usage` |

**告警也是证据**，而且质量更高：告警已经过聚合与分级，`R-CTR-OOM-010`
比零散的 `container.oom_killed` 事件更能说明"容器确实被 OOM 杀了"。

## 阈值是规则数据，不是代码

`SELF_MEM_THRESHOLD` 等常量集中在本模块顶部：比赛现场调参改这里，
不需要动引擎，也不会改变 `rule_set_version` 之外的任何行为。

## GPU 归因的区分（本项目的核心能力）

赛题强调 GPU/vGPU 关联，而"显存满"有两种完全不同的处置方式：

| 情形 | 证据 | 根因 | 建议 |
|---|---|---|---|
| 本机超配 | 本 vGPU 显存占用到 95% | `GPU_MEMORY_EXHAUSTED` | 降低并发 / 检查配额 |
| 邻居争用 | 宿主 GPU 有压力，但**本 vGPU 只占 20%** | `GPU_NEIGHBOR_CONTENTION` | 检查同宿主其他负载 |

第二条靠**反证**表达：`R-CONTRA-SELF-901` 声明"本机占用很低"**反对**
`GPU_MEMORY_EXHAUSTED`。这要求引擎支持 `Rule.contradicts`，
否则这条规则无法写成数据。
"""

from __future__ import annotations

from app.diagnosis.types import EvidenceKind, Rule, RuleSet

#: 规则集版本。**改动任何规则内容都必须同时升版本** —— 已落库的诊断记录了旧版本号，
#: 两者必须能对得上，否则"这条结论当时按什么规则得出"无法回答（D-036）。
DEFAULT_DIAGNOSIS_RULE_SET_VERSION = "rs-diag-1.0.0"

#: 判定"本机 vGPU 显存耗尽"的阈值（百分比）
SELF_MEM_THRESHOLD = 90.0
#: 判定"宿主 GPU 有压力"的阈值（百分比）
HOST_MEM_THRESHOLD = 90.0
#: 判定"本机占用不高"的上限 —— 低于此值说明压力来自邻居（百分比）
LOW_SELF_USAGE = 70.0

#: `observed` 文案的默认后缀。**只用 ASCII 与已冻结的词**，避免与任何编码问题纠缠。
_PCT = "%"


def build_default_rule_set() -> RuleSet:
    """三类赛题场景的默认诊断规则集。

    规则按"结论"分组，同一根因可以由多条规则共同支撑（贡献相加）。
    """
    rules: list[Rule] = [
        # ================================================================
        # 场景一：GPU / 资源瓶颈
        # ================================================================
        Rule(
            id="R-GPU-SELF-001",
            root_cause="GPU_MEMORY_EXHAUSTED",
            contribution=0.55,
            evidence_kind=EvidenceKind.EVENT,
            match_name="gpu.memory.exhausted",
            resource_kinds=("gpu", "vgpu"),
            observed_template="gpu.memory.exhausted on {value}",
            propagate=True,
        ),
        Rule(
            id="R-GPU-SELF-002",
            root_cause="GPU_MEMORY_EXHAUSTED",
            contribution=0.30,
            evidence_kind=EvidenceKind.METRIC,
            match_name="gpu_memory_usage",
            resource_kinds=("gpu", "vgpu"),
            comparator=">=",
            threshold=SELF_MEM_THRESHOLD,
            observed_template="gpu_memory_usage={value}" + _PCT,
            propagate=True,
        ),
        Rule(
            id="R-GPU-SELF-003",
            root_cause="GPU_MEMORY_EXHAUSTED",
            contribution=0.20,
            evidence_kind=EvidenceKind.EVENT,
            match_name="vgpu.quota.exceeded",
            resource_kinds=("vgpu",),
            observed_template="vgpu.quota.exceeded",
            propagate=True,
        ),
        # 邻居争用的**唯一可触发信号**：宿主 GPU 显存压力。
        #
        # 枚举里没有"邻居争用"事件类型（F-01 已冻结，17 项），探针也不上报它 ——
        # 因此这个结论只能由**指标对比**得出：宿主有压力 + 本机 vGPU 占用低。
        # 后半句由下面的反证规则 `R-CONTRA-SELF-901` 承担。
        #
        # 早期版本匹配 `gpu.neighbor.contention`，一个**不存在**的事件类型。
        # 那让"GPU 归因区分"看起来被覆盖了，实际上邻居侧永远不可能触发。
        Rule(
            id="R-GPU-NEIGHBOR-010",
            root_cause="GPU_NEIGHBOR_CONTENTION",
            contribution=0.55,
            evidence_kind=EvidenceKind.METRIC,
            match_name="host_gpu_memory_usage",
            resource_kinds=("gpu", "vgpu"),
            comparator=">=",
            threshold=HOST_MEM_THRESHOLD,
            observed_template="host_gpu_memory_usage={value}" + _PCT + "（宿主压力）",
            propagate=True,
        ),
        Rule(
            id="R-GPU-NEIGHBOR-011",
            root_cause="GPU_NEIGHBOR_CONTENTION",
            contribution=0.30,
            evidence_kind=EvidenceKind.EVENT,
            match_name="gpu.utilization.high",
            resource_kinds=("gpu", "vgpu"),
            observed_template="gpu.utilization.high（与本机显存占用不成比例）",
            propagate=True,
        ),
        # ⭐ 反证：宿主有压力，但本机 vGPU 占用不高 → 压力来自邻居，
        #   因此**反对**"本机超配"这个结论。必须排在 SELF 规则之后，
        #   靠贡献相抵（+0.55+0.30 - 0.40 = 0.45，仍高于 0.30 阈值；
        #   若本机占用也高，这条不命中，SELF 独得 0.85）。
        Rule(
            id="R-CONTRA-SELF-901",
            root_cause="GPU_MEMORY_EXHAUSTED",
            contradicts="GPU_MEMORY_EXHAUSTED",
            contribution=-0.40,
            evidence_kind=EvidenceKind.METRIC,
            match_name="self_vgpu_memory_usage",
            resource_kinds=("gpu", "vgpu"),
            comparator="<",
            threshold=LOW_SELF_USAGE,
            observed_template="self_vgpu_memory_usage={value}" + _PCT + " (本机占用不高)",
        ),
        Rule(
            id="R-GPU-UTIL-020",
            root_cause="GPU_UTILIZATION_SATURATED",
            contribution=0.55,
            evidence_kind=EvidenceKind.EVENT,
            match_name="gpu.utilization.high",
            resource_kinds=("gpu", "vgpu"),
            observed_template="gpu.utilization.high",
            propagate=True,
        ),
        Rule(
            id="R-GPU-UTIL-021",
            root_cause="GPU_UTILIZATION_SATURATED",
            contribution=0.30,
            evidence_kind=EvidenceKind.METRIC,
            match_name="gpu_utilization",
            resource_kinds=("gpu", "vgpu"),
            comparator=">=",
            threshold=SELF_MEM_THRESHOLD,
            observed_template="gpu_utilization={value}" + _PCT,
            propagate=True,
        ),
        Rule(
            id="R-VM-DISK-030",
            root_cause="VM_DISK_IO_SATURATED",
            contribution=0.55,
            evidence_kind=EvidenceKind.EVENT,
            match_name="vm.disk.io_saturated",
            resource_kinds=("vm",),
            observed_template="vm.disk.io_saturated",
            propagate=True,
        ),
        Rule(
            id="R-VM-DISK-031",
            root_cause="VM_DISK_IO_SATURATED",
            contribution=0.25,
            evidence_kind=EvidenceKind.EVENT,
            match_name="process.io_wait.high",
            resource_kinds=("process", "container"),
            observed_template="process.io_wait.high",
            propagate=True,
        ),
        Rule(
            id="R-VM-MEM-040",
            root_cause="VM_MEMORY_EXHAUSTED",
            contribution=0.55,
            evidence_kind=EvidenceKind.EVENT,
            match_name="vm.memory.exhausted",
            resource_kinds=("vm",),
            observed_template="vm.memory.exhausted",
            propagate=True,
        ),
        # ================================================================
        # 场景二：容器 / 进程异常
        # ================================================================
        Rule(
            id="R-CTR-OOM-100",
            root_cause="CONTAINER_MEMORY_LIMIT",
            contribution=0.55,
            evidence_kind=EvidenceKind.ALERT,
            match_name="R-CTR-OOM-010",
            resource_kinds=("container",),
            observed_template="告警 R-CTR-OOM-010（容器被 OOM Killer 终止）x{value}",
            propagate=True,
        ),
        Rule(
            id="R-CTR-OOM-101",
            root_cause="CONTAINER_MEMORY_LIMIT",
            contribution=0.35,
            evidence_kind=EvidenceKind.EVENT,
            match_name="container.oom_killed",
            resource_kinds=("container",),
            observed_template="container.oom_killed x{value}",
            propagate=True,
        ),
        Rule(
            id="R-CTR-MEM-102",
            root_cause="CONTAINER_MEMORY_LIMIT",
            contribution=0.25,
            evidence_kind=EvidenceKind.METRIC,
            match_name="container_memory_usage_ratio",
            resource_kinds=("container",),
            comparator=">=",
            threshold=0.95,
            observed_template="container_memory_usage_ratio={value}",
            propagate=True,
        ),
        Rule(
            id="R-CTR-RESTART-110",
            root_cause="CONTAINER_RESTART_LOOP",
            contribution=0.45,
            evidence_kind=EvidenceKind.EVENT,
            match_name="container.restart",
            resource_kinds=("container",),
            min_count=3,
            observed_template="container.restart x{value}（重启循环）",
            propagate=True,
        ),
        Rule(
            id="R-CTR-RESTART-111",
            root_cause="CONTAINER_RESTART_LOOP",
            contribution=0.25,
            evidence_kind=EvidenceKind.EVENT,
            match_name="process.crash",
            resource_kinds=("process", "container"),
            observed_template="process.crash",
            propagate=True,
        ),
        # 反证：VM 内存也耗尽了 → 根因应上移到 VM，而不是停在容器层
        Rule(
            id="R-CONTRA-CTR-910",
            root_cause="CONTAINER_MEMORY_LIMIT",
            contradicts="CONTAINER_MEMORY_LIMIT",
            contribution=-0.45,
            evidence_kind=EvidenceKind.EVENT,
            match_name="vm.memory.exhausted",
            resource_kinds=("vm",),
            observed_template="vm.memory.exhausted（根因应上移到 VM 层）",
        ),
        # ================================================================
        # 场景三：应用 / Agent 异常
        # ================================================================
        Rule(
            id="R-NET-200",
            root_cause="NETWORK_UNREACHABLE",
            contribution=0.55,
            evidence_kind=EvidenceKind.EVENT,
            match_name="container.network.unreachable",
            resource_kinds=("container", "ai_service"),
            observed_template="container.network.unreachable",
        ),
        Rule(
            id="R-NET-201",
            root_cause="NETWORK_UNREACHABLE",
            contribution=0.30,
            evidence_kind=EvidenceKind.EVENT,
            match_name="agent.network.timeout",
            resource_kinds=("agent",),
            observed_template="agent.network.timeout x{value}",
        ),
        Rule(
            id="R-AGENT-210",
            root_cause="AGENT_TASK_FAILURE",
            contribution=0.55,
            evidence_kind=EvidenceKind.EVENT,
            match_name="agent.task.failed",
            resource_kinds=("agent", "task"),
            observed_template="agent.task.failed x{value}",
        ),
        # ⭐ 反证：网络不可达时，任务失败只是**后果**，根因在网络层。
        #   本规则反对"Agent 自身有问题"这个结论。
        Rule(
            id="R-CONTRA-AGENT-920",
            root_cause="AGENT_TASK_FAILURE",
            contradicts="AGENT_TASK_FAILURE",
            contribution=-0.35,
            evidence_kind=EvidenceKind.EVENT,
            match_name="container.network.unreachable",
            observed_template="container.network.unreachable（任务失败是后果，不是根因）",
        ),
        Rule(
            id="R-AIS-TIMEOUT-220",
            root_cause="MODEL_COMPUTE_BOUND",
            contribution=0.40,
            evidence_kind=EvidenceKind.EVENT,
            match_name="inference.timeout",
            resource_kinds=("ai_service", "container"),
            min_count=5,
            observed_template="inference.timeout x{value}",
            propagate=True,
        ),
        Rule(
            id="R-AIS-ERROR-221",
            root_cause="MODEL_COMPUTE_BOUND",
            contribution=0.25,
            evidence_kind=EvidenceKind.EVENT,
            match_name="inference.error",
            resource_kinds=("ai_service", "container"),
            observed_template="inference.error",
            propagate=True,
        ),
        # 反证：GPU/vGPU 显存确实耗尽时，慢是**结果**，根因在显存而不是模型本身。
        Rule(
            id="R-CONTRA-MODEL-930",
            root_cause="MODEL_COMPUTE_BOUND",
            contradicts="MODEL_COMPUTE_BOUND",
            contribution=-0.30,
            evidence_kind=EvidenceKind.EVENT,
            match_name="gpu.memory.exhausted",
            resource_kinds=("gpu", "vgpu"),
            observed_template="gpu.memory.exhausted（慢是显存耗尽的结果，不是模型本身的问题）",
        ),
    ]
    return RuleSet(version=DEFAULT_DIAGNOSIS_RULE_SET_VERSION, rules=tuple(rules))


def assert_rule_set_is_consistent(rule_set: RuleSet) -> None:
    """自检：规则的引用必须全部指向已冻结的码表，且贡献值在合法区间。

    与 `app.enums.assert_enums_consistent()`、`app.alerts.rules.assert_rules_are_consistent()`
    同一思路：**规则是数据，数据也要有校验**。

    为什么这条自检尤其重要：引擎求值失败的默认表现是"这条规则从不命中"，
    **没有任何报错**。写错一个事件类型名，结果就是诊断永远返回 `UNKNOWN`，
    而日志里干干净净。把校验提前到导入时，是唯一能防住它的办法。
    """
    from app.enums import ResourceKind, RootCause

    known_causes = {m.value for m in RootCause}
    known_kinds = {m.value for m in ResourceKind}

    seen: set[str] = set()
    for rule in rule_set.rules:
        if rule.id in seen:
            raise AssertionError(f"诊断规则 id 重复：{rule.id}")
        seen.add(rule.id)

        if rule.root_cause not in known_causes:
            raise AssertionError(
                f"规则 {rule.id} 引用了未冻结的根因码：{rule.root_cause!r}"
            )
        if rule.contradicts is not None and rule.contradicts not in known_causes:
            raise AssertionError(
                f"规则 {rule.id} 的 contradicts 不是已冻结的根因码：{rule.contradicts!r}"
            )
        if not rule.match_name:
            raise AssertionError(
                f"规则 {rule.id} 的 match_name 为空 —— 它会匹配**所有**同类证据"
            )
        unknown_kinds = set(rule.resource_kinds) - known_kinds
        if unknown_kinds:
            raise AssertionError(f"规则 {rule.id} 引用了未知资源类型：{sorted(unknown_kinds)}")

        if rule.contradicts is not None:
            # 反证必须是**扣分**，否则它什么也反对不了
            if rule.contribution >= 0:
                raise AssertionError(
                    f"规则 {rule.id} 声明了 contradicts，但 contribution={rule.contribution} "
                    "不是负数 —— 反证必须是扣分，否则它不构成反证"
                )
        else:
            if rule.contribution <= 0:
                raise AssertionError(
                    f"规则 {rule.id} 的 contribution={rule.contribution} 不是正数；"
                    "扣分型规则必须显式声明 contradicts"
                )

        if rule.comparator is not None and rule.threshold is None:
            raise AssertionError(f"规则 {rule.id} 给了 comparator 却没有 threshold")
        if rule.comparator is not None and rule.comparator not in (">=", ">", "<=", "<", "=="):
            raise AssertionError(f"规则 {rule.id} 的比较符非法：{rule.comparator!r}")
        if rule.min_count is not None and rule.min_count < 1:
            raise AssertionError(f"规则 {rule.id} 的 min_count={rule.min_count} 小于 1")

    # 三类场景都必须有规则覆盖，否则赛题要求没被满足
    covered = {r.root_cause for r in rule_set.rules}
    required = {
        "GPU / 资源瓶颈": {"GPU_MEMORY_EXHAUSTED", "GPU_NEIGHBOR_CONTENTION"},
        "容器 / 进程异常": {"CONTAINER_MEMORY_LIMIT", "CONTAINER_RESTART_LOOP"},
        "应用 / Agent 异常": {"NETWORK_UNREACHABLE", "AGENT_TASK_FAILURE"},
    }
    for scenario, causes in required.items():
        missing = causes - covered
        if missing:
            raise AssertionError(f"{scenario} 缺少规则覆盖的根因：{sorted(missing)}")

    # GPU 归因的两种情形必须**都能**被表达，否则核心能力是空的
    if not any(r.contradicts == "GPU_MEMORY_EXHAUSTED" for r in rule_set.rules):
        raise AssertionError(
            "缺少反对 GPU_MEMORY_EXHAUSTED 的反证规则 —— "
            "「邻居争用」与「本机超配」将无法区分（本项目的核心能力）"
        )


__all__ = [
    "DEFAULT_DIAGNOSIS_RULE_SET_VERSION",
    "HOST_MEM_THRESHOLD",
    "LOW_SELF_USAGE",
    "SELF_MEM_THRESHOLD",
    "assert_rule_set_is_consistent",
    "build_default_rule_set",
]
