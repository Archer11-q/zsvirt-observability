"""枚举与字典的**单一真源**。

这些值已由三方冻结（`docs/CONTRACT_FREEZE.md` F-01～F-04，A、C 均签字），
**改名即破坏性变更**，须走 `CONTRIBUTING.md` §5 的契约变更流程。

为什么集中在一个文件：

  - `GET /api/v1/dict` 的响应需要码 + 中文文案
  - 告警引擎与诊断引擎需要按码查表（严重级别排序、根因 → 建议映射）
  - 前端不硬编码中文映射，因此文案必须由 B 提供且**只有一份**
  - 散落多处必然漂移，而枚举漂移是"只改代码不改契约"的典型形态

对应关系：

| 本文定义 | 契约位置 |
|---|---|
| `Severity` / `SEVERITY_LABELS` | `CONTRACT_FREEZE.md` F-04 |
| `AlertState` / `ALERT_STATE_LABELS` | F-04 |
| `ResourceKind` / `RESOURCE_KIND_LABELS` | F-04（含 F-04.1：`vgpu`、`task` 均纳入） |
| `ResourceStatus` / `RESOURCE_STATUS_LABELS` | F-04 |
| `Observability` | `DATA_MODEL.md` §4.1（与 `status` 分离，D-032） |
| `EventType` / `EVENT_TYPE_LABELS` | F-01（17 项） |
| `RootCause` / `ROOT_CAUSE_LABELS` | F-02（11 项） |
| `RecommendationCode` / `RECOMMENDATION_LABELS` | F-03（14 项） |
| `EventSource` | `DATA_MODEL.md` §5.1 |
"""

from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------- 严重级别


class Severity(StrEnum):
    """事件与告警的严重级别（F-04）。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


SEVERITY_LABELS: dict[str, str] = {
    "info": "提示",
    "warning": "警告",
    "error": "错误",
    "critical": "严重",
}

#: 数值化用于排序与"取更严重者"。数值越大越严重。
SEVERITY_RANK: dict[str, int] = {
    "info": 10,
    "warning": 20,
    "error": 30,
    "critical": 40,
}


def worst_severity(a: str, b: str) -> str:
    """取两者中更严重的。用于告警聚合时提升级别。"""
    return a if SEVERITY_RANK.get(a, 0) >= SEVERITY_RANK.get(b, 0) else b


# ---------------------------------------------------------------- 告警状态


class AlertState(StrEnum):
    """告警生命周期（F-04）。`firing → acked → resolved`，旁路 `silenced`。"""

    FIRING = "firing"
    ACKED = "acked"
    RESOLVED = "resolved"
    SILENCED = "silenced"


ALERT_STATE_LABELS: dict[str, str] = {
    "firing": "触发中",
    "acked": "已确认",
    "resolved": "已恢复",
    "silenced": "已静默",
}

#: 允许的状态迁移。状态机由 `app.alerts` 使用，避免非法跳转。
ALERT_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "firing": frozenset({"acked", "resolved", "silenced"}),
    "acked": frozenset({"resolved", "firing"}),
    "silenced": frozenset({"firing", "resolved"}),
    "resolved": frozenset({"firing"}),  # 允许复发
}


# ---------------------------------------------------------------- 资源


class ResourceKind(StrEnum):
    """资源类型（F-04，含 F-04.1 定论：`vgpu` 与 `task` 均纳入首版）。

    `unresolved` 是**占位资源类型**，不是真实资源类型：当事件引用了尚未
    上报的资源时（成员 A 的 Q1 允许极端竞态出现孤儿引用），B 把事件挂到
    该类型下的占位节点，而不是丢弃数据或报错。占位节点会计数并暴露为
    健康指标（`/api/health` 的 `ingest.unresolvedEvents`）。
    """

    HOST = "host"
    GPU = "gpu"
    VGPU = "vgpu"
    VM = "vm"
    CONTAINER = "container"
    PROCESS = "process"
    AI_SERVICE = "ai_service"
    AGENT = "agent"
    TASK = "task"
    UNRESOLVED = "unresolved"


RESOURCE_KIND_LABELS: dict[str, str] = {
    "host": "宿主机",
    "gpu": "GPU",
    "vgpu": "vGPU",
    "vm": "虚拟机",
    "container": "容器",
    "process": "进程",
    "ai_service": "AI 服务",
    "agent": "智能体",
    "task": "任务",
    "unresolved": "未识别资源",
}

#: 真实资源类型（不含 `unresolved` 占位）。用于图校验与拓扑查询过滤。
REAL_RESOURCE_KINDS: frozenset[str] = frozenset(
    m.value for m in ResourceKind if m is not ResourceKind.UNRESOLVED
)


class ResourceStatus(StrEnum):
    """**业务状态** —— 由来源系统给出，B 不得推测（D-032）。"""

    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    UNKNOWN = "unknown"


RESOURCE_STATUS_LABELS: dict[str, str] = {
    "running": "运行中",
    "stopped": "已停止",
    "error": "异常",
    "unknown": "未知",
}


class Observability(StrEnum):
    """**B 的观测状态** —— 与 `ResourceStatus` 是两个独立字段，禁止混用（D-032）。

    一个 VM 可以被 ZSvirt 报为 `status=running`，同时因探针断线而为
    `observability=stale`。两者同时为真是正常的，这正是不该合并的原因。
    """

    ACTIVE = "active"
    STALE = "stale"
    GONE = "gone"


OBSERVABILITY_LABELS: dict[str, str] = {
    "active": "正常观测",
    "stale": "数据陈旧",
    "gone": "已消失",
}


# ---------------------------------------------------------------- 事件


class EventSource(StrEnum):
    """**事件**来源（`DATA_MODEL.md` §5.1）。

    `DERIVED` 表示由 B 自身产生（如时钟漂移、未识别资源计数）。
    """

    ZSVIRT = "zsvirt"
    PROBE = "probe"
    DERIVED = "derived"


class EvidenceSource(StrEnum):
    """**诊断证据**来源。

    与 `EventSource` 分开定义是刻意的：证据可以来自 ZWatch 指标或模拟数据，
    而事件不能（`DATA_MODEL.md` §5.1 的事件来源只有三种）。
    两者混用会让 `simulated` 有机会混进事件表，破坏诚实性要求（§4.2.1）。

    `SIMULATED` 尤其关键：赛题要求支持降级模式，但**模拟数据必须可识别**，
    不得伪装成真实采集进入证据链。
    """

    ZSVIRT = "zsvirt"
    ZWATCH = "zwatch"
    PROBE = "probe"
    SIMULATED = "simulated"
    DERIVED = "derived"


class EventType(StrEnum):
    """事件类型枚举（**F-01，17 项，已冻结**）。

    命名规范：`{domain}.{subject}.{qualifier}`，全小写点分。

    **阈值与持续时间不写进类型名** —— `type` 只表达"是什么事"，
    阈值放 `metrics` 或规则集，否则会出现 `gpu.memory.high_95pct` 这类失控命名。
    """

    # --- GPU / 资源瓶颈（场景一）---
    GPU_MEMORY_EXHAUSTED = "gpu.memory.exhausted"
    GPU_UTILIZATION_HIGH = "gpu.utilization.high"
    VGPU_QUOTA_EXCEEDED = "vgpu.quota.exceeded"
    VM_DISK_IO_SATURATED = "vm.disk.io_saturated"

    # --- 容器 / 进程异常（场景二）---
    CONTAINER_OOM_KILLED = "container.oom_killed"
    CONTAINER_RESTART = "container.restart"
    PROCESS_CRASH = "process.crash"
    PROCESS_IO_WAIT_HIGH = "process.io_wait.high"

    # --- 应用 / Agent 任务异常（场景三）---
    INFERENCE_TIMEOUT = "inference.timeout"
    INFERENCE_ERROR = "inference.error"
    AGENT_TASK_FAILED = "agent.task.failed"
    AGENT_NETWORK_TIMEOUT = "agent.network.timeout"
    CONTAINER_NETWORK_UNREACHABLE = "container.network.unreachable"

    # --- 平台 / 接入侧（由 B 产生）---
    INGEST_CLOCK_DRIFT_HIGH = "ingest.clock_drift.high"
    INGEST_RESOURCE_UNRESOLVED = "ingest.resource.unresolved"
    ZSVIRT_SYNC_FAILED = "zsvirt.sync.failed"
    GPU_PROVIDER_DEGRADED = "gpu.provider.degraded"


EVENT_TYPE_LABELS: dict[str, str] = {
    "gpu.memory.exhausted": "GPU 显存耗尽",
    "gpu.utilization.high": "GPU 利用率过高",
    "vgpu.quota.exceeded": "vGPU 配额超出",
    "vm.disk.io_saturated": "虚拟机磁盘 I/O 饱和",
    "container.oom_killed": "容器内存溢出被终止",
    "container.restart": "容器重启",
    "process.crash": "进程异常退出",
    "process.io_wait.high": "进程 I/O 等待过高",
    "inference.timeout": "推理请求超时",
    "inference.error": "推理请求错误",
    "agent.task.failed": "智能体任务失败",
    "agent.network.timeout": "智能体网络超时",
    "container.network.unreachable": "容器网络不可达",
    "ingest.clock_drift.high": "采集时钟漂移过大",
    "ingest.resource.unresolved": "存在未识别资源",
    "zsvirt.sync.failed": "平台资源同步失败",
    "gpu.provider.degraded": "GPU 数据渠道降级",
}

#: 事件类型的默认严重级别。用于字典端点兜底；
#: **前端按事件自身携带的 `severity` 渲染**（C 的要求），本表非渲染依据。
EVENT_TYPE_DEFAULT_SEVERITY: dict[str, str] = {
    "gpu.memory.exhausted": "critical",
    "gpu.utilization.high": "warning",
    "vgpu.quota.exceeded": "critical",
    "vm.disk.io_saturated": "warning",
    "container.oom_killed": "critical",
    "container.restart": "warning",
    "process.crash": "error",
    "process.io_wait.high": "warning",
    "inference.timeout": "error",
    "inference.error": "error",
    "agent.task.failed": "error",
    "agent.network.timeout": "error",
    "container.network.unreachable": "error",
    "ingest.clock_drift.high": "warning",
    "ingest.resource.unresolved": "info",
    "zsvirt.sync.failed": "warning",
    "gpu.provider.degraded": "warning",
}


# ---------------------------------------------------------------- 诊断


class RootCause(StrEnum):
    """根因码（**F-02，11 项，已冻结**）。

    `GPU_MEMORY_EXHAUSTED` 与 `GPU_NEIGHBOR_CONTENTION` 的拆分是本项目
    「GPU 归因」能力的直接体现：合并后前端与运维无法区分"该找邻居还是
    该降自己的并发"，诊断建议会失去针对性（见 `DATA_MODEL.md` §4.2.1）。
    """

    GPU_MEMORY_EXHAUSTED = "GPU_MEMORY_EXHAUSTED"
    GPU_NEIGHBOR_CONTENTION = "GPU_NEIGHBOR_CONTENTION"
    GPU_UTILIZATION_SATURATED = "GPU_UTILIZATION_SATURATED"
    CONTAINER_MEMORY_LIMIT = "CONTAINER_MEMORY_LIMIT"
    CONTAINER_RESTART_LOOP = "CONTAINER_RESTART_LOOP"
    VM_MEMORY_EXHAUSTED = "VM_MEMORY_EXHAUSTED"
    VM_DISK_IO_SATURATED = "VM_DISK_IO_SATURATED"
    NETWORK_UNREACHABLE = "NETWORK_UNREACHABLE"
    AGENT_TASK_FAILURE = "AGENT_TASK_FAILURE"
    MODEL_COMPUTE_BOUND = "MODEL_COMPUTE_BOUND"
    UNKNOWN = "UNKNOWN"


ROOT_CAUSE_LABELS: dict[str, str] = {
    "GPU_MEMORY_EXHAUSTED": "GPU 显存耗尽（本机超配）",
    "GPU_NEIGHBOR_CONTENTION": "GPU 争用（同宿主其他负载占用）",
    "GPU_UTILIZATION_SATURATED": "GPU 算力饱和",
    "CONTAINER_MEMORY_LIMIT": "容器内存限额触顶",
    "CONTAINER_RESTART_LOOP": "容器重启循环",
    "VM_MEMORY_EXHAUSTED": "虚拟机内存耗尽",
    "VM_DISK_IO_SATURATED": "虚拟机磁盘 I/O 饱和",
    "NETWORK_UNREACHABLE": "网络不可达",
    "AGENT_TASK_FAILURE": "智能体任务失败",
    "MODEL_COMPUTE_BOUND": "模型计算受限",
    "UNKNOWN": "未识别",
}


class RecommendationCode(StrEnum):
    """处置建议码（**F-03，14 项，已冻结**）。

    `recommendation` 是**数组，排序即优先级**（C 确认）。
    """

    REDUCE_CONCURRENCY = "REDUCE_CONCURRENCY"
    CHECK_GPU_ALLOCATION = "CHECK_GPU_ALLOCATION"
    CHECK_NEIGHBOR_WORKLOADS = "CHECK_NEIGHBOR_WORKLOADS"
    INCREASE_CONTAINER_MEMORY = "INCREASE_CONTAINER_MEMORY"
    CHECK_CONTAINER_RESTART = "CHECK_CONTAINER_RESTART"
    INCREASE_VM_MEMORY = "INCREASE_VM_MEMORY"
    LIMIT_IO_NOISY_PROCESS = "LIMIT_IO_NOISY_PROCESS"
    SEPARATE_LOG_DISK = "SEPARATE_LOG_DISK"
    CHECK_NETWORK_POLICY = "CHECK_NETWORK_POLICY"
    CHECK_DNS = "CHECK_DNS"
    CHECK_DEPENDENCY_HEALTH = "CHECK_DEPENDENCY_HEALTH"
    REVIEW_AGENT_RETRY_POLICY = "REVIEW_AGENT_RETRY_POLICY"
    PROFILE_MODEL = "PROFILE_MODEL"
    COLLECT_MORE_EVIDENCE = "COLLECT_MORE_EVIDENCE"


RECOMMENDATION_LABELS: dict[str, str] = {
    "REDUCE_CONCURRENCY": "降低推理并发或 batch size",
    "CHECK_GPU_ALLOCATION": "检查该虚拟机的 vGPU 分配与配额",
    "CHECK_NEIGHBOR_WORKLOADS": "检查同一宿主机上其他虚拟机的 GPU 占用",
    "INCREASE_CONTAINER_MEMORY": "提高容器内存限额或优化模型内存占用",
    "CHECK_CONTAINER_RESTART": "检查容器退出码与重启日志",
    "INCREASE_VM_MEMORY": "为虚拟机扩容内存",
    "LIMIT_IO_NOISY_PROCESS": "限制同机 I/O 干扰进程",
    "SEPARATE_LOG_DISK": "将日志写入独立磁盘",
    "CHECK_NETWORK_POLICY": "检查容器/虚拟机网络策略与安全组",
    "CHECK_DNS": "检查 DNS 解析是否正常",
    "CHECK_DEPENDENCY_HEALTH": "检查被调用服务是否可用",
    "REVIEW_AGENT_RETRY_POLICY": "检查智能体重试与超时配置",
    "PROFILE_MODEL": "对模型做性能分析，评估算力需求",
    "COLLECT_MORE_EVIDENCE": "证据不足，建议扩大时间窗或补采集",
}


# ---------------------------------------------------------------- 字典聚合


def build_dict_payload() -> dict[str, dict[str, str]]:
    """构造 `GET /api/v1/dict` 的响应体（`API_CONTRACT.md` §4.6.1）。

    **含可读文案** —— 这是 C 的明确要求：前端筛选下拉、图例颜色、状态标签、
    根因/建议文案都依赖映射；硬编码会导致前后端漂移，破坏可复现性。
    """
    return {
        "severity": dict(SEVERITY_LABELS),
        "alertState": dict(ALERT_STATE_LABELS),
        "resourceKind": dict(RESOURCE_KIND_LABELS),
        "resourceStatus": dict(RESOURCE_STATUS_LABELS),
        "observability": dict(OBSERVABILITY_LABELS),
        "eventType": dict(EVENT_TYPE_LABELS),
        "rootCause": dict(ROOT_CAUSE_LABELS),
        "recommendation": dict(RECOMMENDATION_LABELS),
    }


def assert_enums_consistent() -> None:
    """自检：每个枚举值都必须有中文文案，且两份表键集一致。

    由单测调用。任何新增枚举而忘记补文案的情况都会在这里暴露，
    避免字典端点漏项导致前端渲染空白。
    """
    pairs = [
        ("severity", Severity, SEVERITY_LABELS),
        ("alertState", AlertState, ALERT_STATE_LABELS),
        ("resourceKind", ResourceKind, RESOURCE_KIND_LABELS),
        ("resourceStatus", ResourceStatus, RESOURCE_STATUS_LABELS),
        ("observability", Observability, OBSERVABILITY_LABELS),
        ("eventType", EventType, EVENT_TYPE_LABELS),
        ("rootCause", RootCause, ROOT_CAUSE_LABELS),
        ("recommendation", RecommendationCode, RECOMMENDATION_LABELS),
    ]
    problems: list[str] = []
    for name, enum_cls, labels in pairs:
        values = {member.value for member in enum_cls}
        missing = values - set(labels)
        extra = set(labels) - values
        if missing:
            problems.append(f"{name}: 缺文案 {sorted(missing)}")
        if extra:
            problems.append(f"{name}: 多余文案 {sorted(extra)}")

    # 事件默认严重级别必须覆盖全部事件类型
    missing_sev = {m.value for m in EventType} - set(EVENT_TYPE_DEFAULT_SEVERITY)
    if missing_sev:
        problems.append(f"eventType: 缺默认 severity {sorted(missing_sev)}")

    # 状态迁移表必须覆盖全部告警状态
    missing_trans = {m.value for m in AlertState} - set(ALERT_STATE_TRANSITIONS)
    if missing_trans:
        problems.append(f"alertState: 缺状态迁移定义 {sorted(missing_trans)}")

    if problems:
        raise AssertionError("枚举与字典不一致：\n  " + "\n  ".join(problems))
