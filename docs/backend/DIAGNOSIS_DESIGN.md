# ZSvirt Observability — 诊断引擎设计

| 字段 | 值 |
|---|---|
| 文档状态 | **DRAFT v0.1 — 未经团队评审，不得作为实现依据** |
| 归属 | 成员 B 模块设计（产品文档） |
| 负责人 | 成员 B |
| 最后更新 | 2026-09-16 |
| 前置依赖 | `DATA_MODEL.md`（对象与字段）、`API_CONTRACT.md`（输入契约） |

> **设计立场**：本阶段只做**规则 + 证据链**，不引入任何未经确认的复杂算法（机器学习、图算法库、时序异常检测模型）。
> `confidence` 是**规则加权评分**，必须可由 `confidenceBreakdown` 复算；**严禁伪装成统计学概率**。

---

## 1. 为什么不做"黑盒诊断"

赛题评分强调**可解释**：诊断结果必须指出证据。因此本设计的硬性约束是：

1. 没有证据 → 不允许有根因结论（返回 `UNKNOWN`）。
2. 有结论 → 必须能列出支撑它的每一条证据记录。
3. 有置信分 → 必须能由规则贡献逐项相加复算。
4. 有结论 → 必须记录 `ruleSetVersion`，保证结论可复现。

---

## 2. 引擎架构

诊断逻辑**不得散落在 Controller / Handler 中**。它必须是独立、纯函数式、可单测的模块。

```
                  ┌────────────────────────────────────────────┐
                  │            Diagnosis Engine                 │
                  │                                             │
  DiagnosisContext│  ┌──────────┐   ┌──────────┐   ┌──────────┐ │
  ───────────────▶│  │ Correlate│──▶│  Match   │──▶│  Score   │ │
   anchor resource│  │  跨层归并 │   │ 规则匹配  │   │ 加权评分  │ │
   time window    │  └──────────┘   └──────────┘   └──────────┘ │
   evidence set   │        │              │              │      │
   resource graph │        ▼              ▼              ▼      │
                  │   candidate      matched        confidence  │
                  │   root causes    rules          breakdown   │
                  └────────────────────┬────────────────────────┘
                                       ▼
                                  Diagnosis
                       (rootCause + confidence + evidence
                        + affectedResources + recommendation)
```

### 2.1 三条边界约束

| 约束 | 原因 |
|---|---|
| 引擎**不访问数据库、不发 HTTP** | 接收装配好的 `DiagnosisContext` 纯数据对象，保证可脱离基础设施单测 |
| 规则是**声明式数据**（YAML / 表），不是代码分支 | 避免"为每个场景写一次性硬编码"，新场景只加数据不加代码路径 |
| 资源图**只读传入** | 引擎不修改拓扑，避免诊断过程产生副作用 |

### 2.2 核心数据结构

```python
@dataclass(frozen=True)
class DiagnosisContext:
    anchor_resource_id: str          # 受影响资源
    window: TimeWindow               # 关联时间窗（含容差）
    resources: dict[str, Resource]   # 锚点及其上下游资源（已装配）
    edges: list[Edge]                # 资源关系
    events: list[Event]              # 窗口内事件
    alerts: list[Alert]              # 窗口内告警
    rule_set: RuleSet                # 规则集（含 version）

@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    contribution: float              # 对 confidence 的贡献
    observed: str                    # 人类可读的观测事实
    evidence_refs: list[str]         # 指向支撑它的 event/alert id
```

---

## 3. 五步执行流程

| 步骤 | 输入 | 输出 | 说明 |
|---|---|---|---|
| 1. **触发** | alertId 或 (window, anchorResourceId) | `DiagnosisContext` 骨架 | 由告警触发，或人工/定时触发 |
| 2. **汇聚** | 锚点 + 资源图 | 上下游资源集合 | 默认向上（父）2 层、向下（子）3 层【待确认】 |
| 3. **关联** | 资源集合 + 时间窗 | 证据集合 | 按 `occurredAt` 落入窗口，按资源归属归并 |
| 4. **匹配** | 证据集合 + 规则集 | `RuleHit[]` | 逐条规则求值，全部留痕（命中与未命中都要可查） |
| 5. **评分与产出** | `RuleHit[]` | `Diagnosis` | 加权求和 → 裁剪到 `[0,1]`；生成影响范围与建议 |

### 3.1 关联容差（【待确认】）

跨层事件的时间戳来自不同时钟（ZSvirt 宿主机、VM 内核、容器、探针），存在漂移。

| 参数 | 候选值 | 说明 |
|---|---|---|
| 关联容差窗口 | `±30s` | 【提案】 |
| 上溯窗口 | 锚点事件前 `5min` | 根因必然发生在症状之前 |
| 下溯窗口 | 锚点事件后 `2min` | 捕捉连锁效应 |

**原则：根因时间必须早于或等于症状时间。** 反向关联（症状早于"根因"）应作为**反证**降低该项得分，而不是忽略。这是抵抗错误归因的关键机制。

---

## 4. 置信分模型（【提案】）

```
confidence = clamp( Σ (rule.contribution × rule.weight) , 0.0, 1.0 )
```

| 规则 | 贡献 | 说明 |
|---|---|---|
| 直接指标证据 | `+0.55` | 如 `gpu_memory_usage = 98%`，指标本身即强证据 |
| 同层症状事件 | `+0.25` | 同一资源上的错误事件 |
| 跨层症状事件 | `+0.15` | 子层资源（如容器）出现症状 |
| 时序一致性 | `+0.05` | 根因时间早于症状时间 |
| **反证扣分** | `−0.30` | 存在与结论矛盾的证据（如 GPU 内存充足） |
| **冲突扣分** | `−0.20` | 另一个候选根因得分接近（`|Δ| < 0.15`）时双方都降权 |

**规则**：
- 得分最高的候选成为 `rootCause`；若最高分 `< 0.30`，强制返回 `UNKNOWN`。
- `confidenceBreakdown` 必须逐项列出 `ruleId / contribution / observed`。
- 所有系数是**规则集数据**，可调、可版本化，不写死在代码里。

---

## 5. 三类故障场景（赛题必需）

> **重要**：本节的场景选择与具体事件类型名**必须与成员 A、C 三方确认**，因为事件 `type` 枚举是跨层关联的锚点。以下为成员 B 的**提案剧本**。

### 场景一：GPU 显存耗尽导致推理服务失败

**这是最贴合赛题"GPU/vGPU 关联"要求的场景。**

| 项 | 内容 |
|---|---|
| **注入方式** | 在 VM 内启动多个并发推理请求 / 加载超大模型，使 vGPU 显存打满【待 A 确认可行性】 |
| **症状层** | `ai_service`：请求超时、5xx；`container`：OOMKilled、重启计数上升 |
| **中间层** | `vm`：CPU/内存压力上升 |
| **根因层** | `vgpu` / `gpu`：显存使用率 →98%，显存不足告警 |
| **关键事件类型** | `gpu.memory.exhausted`、`inference.timeout`、`container.oom_killed` |
| **关联链** | `gpu`（根因）→ `vgpu` → `vm` → `container` → `ai_service`（症状） |
| **诊断输出** | `rootCause = GPU_MEMORY_EXHAUSTED`；影响范围含 GPU/vGPU/VM/容器/AI 服务；建议：降低并发、检查 vGPU 分配、限制 batch size |
| **反证检查** | 若 GPU 显存充足而容器仍 OOM，则应指向 `CONTAINER_MEMORY_LIMIT`，不得硬套 GPU 结论 |

### 场景二：VM 磁盘 I/O 饱和导致 AI 服务响应劣化

| 项 | 内容 |
|---|---|
| **注入方式** | VM 内施加大规模磁盘写（如 `fio` / `dd`），与模型加载 / 日志写入争抢 I/O |
| **症状层** | `ai_service`：尾延迟（p99）升高；`process`：I/O wait 升高 |
| **中间层** | `container`：读写延迟升高 |
| **根因层** | `vm`：磁盘 IOPS / 吞吐接近上限、`await` 时间升高 |
| **关键事件类型** | `vm.disk.io_saturated`、`ai_service.latency.degraded` |
| **关联链** | `vm`（根因）→ `container` → `ai_service`（症状） |
| **诊断输出** | `rootCause = VM_DISK_IO_SATURATED`；建议：限制 I/O 干扰进程、分离日志盘、调整模型加载策略 |
| **区分难点** | 需与"模型本身计算慢"区分：若 I/O 指标正常而延迟高，应指向 `MODEL_COMPUTE_BOUND` |

### 场景三：Agent / 容器网络异常导致任务失败

| 项 | 内容 |
|---|---|
| **注入方式** | 容器网络限制 / 丢包 / DNS 故障 / 目标服务不可达 |
| **症状层** | `agent`：任务失败、重试激增；`ai_service`：上游调用失败 |
| **中间层** | `container`：连接错误、重传 |
| **根因层** | 容器网络 / VM 网络 / 依赖服务不可达 |
| **关键事件类型** | `agent.network.timeout`、`container.network.unreachable` |
| **关联链** | 网络资源（根因）→ `container` → `agent`（症状） |
| **诊断输出** | `rootCause = NETWORK_UNREACHABLE`；建议：检查网络策略、DNS、依赖服务健康 |
| **边界的诚实性** | 若 B 没有网络层资源数据，必须返回 `UNKNOWN` + 已有证据，**不得猜测网络原因** |

### 5.1 场景选择需确认的问题

| # | 问题 | 归属 |
|---|---|---|
| S1 | 三个场景是否就按上述选定？是否需要第四个（如 CPU 争抢）？ | 三方 |
| S2 | 场景一的 GPU 显存压力能否在远程 ZSvirt 集群上安全注入？ | 成员 A / 平台 |
| S3 | 事件 `type` 枚举由谁定义、在哪冻结？ | 三方 |
| S4 | 每个场景由谁负责提供可复现的注入脚本？ | 三方 |
| S5 | 演示是实时注入还是预置数据回放？ | 三方 |

---

## 6. 影响范围计算

从根因资源出发，沿资源图**向下传播**（受影响方向）：

```
根因 gpu
  └─ vgpu  → 受影响
       └─ vm  → 受影响
            ├─ container → 受影响
            │    ├─ process → 受影响
            │    └─ ai_service → 受影响
            └─ container(healthy) → 若有证据表明其无异常，则不标记为受影响
```

**诚实性原则**：`affectedResources` 只包含**有证据支持**的资源（该资源自身存在异常事件/指标），而不是"结构上在下游所以一律算受影响"。否则影响范围会变成无意义的全量拓扑。

【待确认】是否同时输出 `potentiallyAffected`（结构上相关但无证据）与 `affectedResources`（有证据）。成员 B 建议**分开输出**，避免误导 C 的展示。

---

## 7. 规则集设计（声明式）

### 7.1 规则结构（【提案】）

```yaml
version: rs-0.1.0
rules:
  - id: R-GPU-MEM-001
    when:
      resourceKind: [gpu, vgpu]
      metric: gpu_memory_usage
      comparator: ">="
      threshold: 95
      durationSec: 30
    then:
      rootCause: GPU_MEMORY_EXHAUSTED
      contribution: 0.55
      observedTemplate: "gpu_memory_usage={value}%"
    evidenceTypes: [metric]

  - id: R-INFER-TIMEOUT-002
    when:
      resourceKind: [ai_service]
      eventType: inference.timeout
      minCount: 5
    then:
      rootCause: GPU_MEMORY_EXHAUSTED
      contribution: 0.25
    evidenceTypes: [event]

  - id: R-CONTRADICT-GPU-MEM-900
    when:
      resourceKind: [gpu, vgpu]
      metric: gpu_memory_usage
      comparator: "<"
      threshold: 70
    then:
      contradicts: GPU_MEMORY_EXHAUSTED
      contribution: -0.30
```

### 7.2 设计要点

- 同一 `rootCause` 可由**多条规则共同支撑**，贡献相加。
- 规则可以有 `contradicts`（反证）与 `requiresUpstream`（要求父层证据）语义。
- 规则集有独立版本号，与代码版本解耦；`Diagnosis.ruleSetVersion` 记录它。
- **新故障场景 = 新增规则数据，不新增代码分支。**

---

## 8. 测试策略（诊断引擎）

| 层次 | 内容 |
|---|---|
| 单元测试 | 单条规则求值：边界值（95 vs 94.9）、持续时间条件、缺字段容错 |
| 单元测试 | 评分：贡献求和、裁剪到 `[0,1]`、反证扣分、冲突降权、`< 0.30 → UNKNOWN` |
| 单元测试 | `confidenceBreakdown` 复算一致性（断言 `sum(contributions) == confidence`） |
| 集成测试 | 用夹具（fixture）事件流跑完整五步流程，断言根因与证据列表 |
| 场景测试 | 三个故障场景各一条端到端用例，断言 `rootCause` / `affectedResources` / `evidence` 非空 |
| 负向测试 | 无证据 → `UNKNOWN`；矛盾证据 → 不得给出错误的高置信结论 |
| 可复现测试 | 同一输入 + 同一 `ruleSetVersion` → 完全相同的输出（确定性） |

**最小可验收闭环**：给定一份固定的场景事件夹具，诊断引擎输出带证据、可复算置信分、且结果确定的 `Diagnosis`。

---

## 9. 未决问题

1. 三类场景的最终选定与事件类型枚举冻结（S1、S3）。
2. 资源图上下游检索的层数与性能上限。
3. 关联容差窗口取值（§3.1）。
4. 是否输出 `potentiallyAffected`（§6）。
5. 诊断是自动触发（告警联动）还是 C 手动触发（`API_CONTRACT.md` Q13）。
6. 是否需要诊断结果的持久化与历史对比。
7. 规则集存放形式：YAML 文件 / 数据库表 / 代码内常量。

---

## 10. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 首轮草案：引擎架构、五步流程、置信分模型、三类场景、规则集设计、测试策略 | DRAFT，待三方评审 |
