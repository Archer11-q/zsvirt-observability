# Crosslayer — 数据模型

| 字段 | 值 |
|---|---|
| 文档状态 | **DRAFT v0.1 — 未经团队评审，不得作为实现依据** |
| 归属 | 全项目真源（产品文档） |
| 负责人 | 成员 B |
| 最后更新 | 2026-09-16 |
| 评审人 | 成员 A、成员 C（待评审） |

> 约定：`【待确认】` 表示需团队决策；`【提案】` 表示成员 B 的推荐方案；`TBD` 表示字段语义或取值尚未确定。
> **本文件中的字段在未确认前一律标记为待定，不得在代码中当作既定事实实现。**

---

## 1. 设计目标

1. **可追溯**：从宿主机一路下钻到 AI Agent，任意两层之间可双向导航。
2. **稳定 ID**：任何对象在生命周期内 ID 不变，且能反查到来源系统。
3. **不编造**：来源系统没给的信息，一律 `null` + 显式标记，不用默认值猜测。
4. **可解释**：每个诊断结论都能指回具体证据记录。

---

## 2. 资源对象与层级

```
Host (宿主机)
 └── GPU (物理 GPU)
      └── vGPU (虚拟 GPU / MIG 实例)        【待确认】是否为独立实体
           └── VM (ZSvirt 虚拟机)  ← 赛题边界
                ├── Container (容器)
                │    └── Process (进程)
                └── Process (VM 内直接运行的进程)
                     └── AIService (AI 服务)
                          └── Agent (智能体) / Task (任务)
```

> **待确认**：`vGPU` 是否作为独立资源实体，还是仅作为 GPU 的一个属性（如 `gpu.partitionProfile`）。ZSvirt 对 vGPU 的建模方式决定此选择（R1）。

### 2.1 资源类型枚举

| kind | 说明 | ID 归属（权威来源） |
|---|---|---|
| `host` | 物理宿主机 | ZSvirt |
| `gpu` | 物理 GPU | ZSvirt |
| `vgpu` | 虚拟 GPU / 分区 | ZSvirt【待确认】 |
| `vm` | ZSvirt 虚拟机（**边界对象**） | ZSvirt |
| `container` | VM 内容器 | A 探针 |
| `process` | 进程 | A 探针 |
| `ai_service` | AI 服务（如推理服务） | A 探针 |
| `agent` | 智能体 | A 探针 |
| `task` | 一次任务 / 请求批次 | A 探针【待确认】 |

---

## 3. 资源 ID 语义（【提案】，关键契约）

### 3.1 格式

```
{kind}:{source}:{sourceId}
```

| 段 | 含义 | 示例 |
|---|---|---|
| `kind` | 资源类型枚举（§2.1） | `vm` |
| `source` | 权威来源系统：`zsvirt` \| `probe` \| `manual` | `zsvirt` |
| `sourceId` | 来源系统内的原生标识 | ZSvirt VM UUID |

示例：

```
vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44
container:probe:9d1c...:web-0        # 探针侧原生 ID 由 A 定义【待确认】
ai_service:probe:9d1c...:vllm
```

### 3.2 为什么这样设计

- **可追溯**：一眼看出这个资源是谁定义的，避免"这个 ID 是谁生成的"这类联调扯皮。
- **不冲突**：ZSvirt 与探针各管一段，不存在两个系统同时claiming 同一个 ID。
- **可降级**：`source=manual` 允许演示期手工登记资源，不阻塞联调。
- **代价**：ID 较长；需在 API 层保证不把它当主键暴露给 C 的展示逻辑（C 只应把它当不透明字符串）。

### 3.3 约束

- ID **不透明**：消费方（C）不得解析 ID 结构，只能整体比对与传递。
- ID **不可变**：资源重建（例如 VM 重建）视为新资源，不复用旧 ID。
- ID **不承载语义**：不得从 ID 推断状态、时间或健康度。

> **【待确认】** 是否采纳本方案；探针侧 `sourceId` 的生成规则需成员 A 确认。

---

## 4. 资源对象字段

### 4.1 公共字段（所有资源）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 见 §3，不透明稳定 ID |
| `kind` | enum | ✅ | 资源类型 |
| `name` | string | ⬜ | 展示名，可能重复，**不得用作标识** |
| `parentId` | string \| null | ⬜ | 上级资源 ID，构成层级 |
| `status` | enum | ✅ | **业务状态**：`running` \| `stopped` \| `error` \| `unknown` |
| `observability` | enum | ✅ | **B 的观测状态**：`active` \| `stale` \| `gone` |
| `firstSeenAt` | timestamp | ✅ | B 首次观测到该资源的时间 |
| `lastSeenAt` | timestamp | ✅ | B 最近一次观测到该资源的时间 |
| `staleness` | string \| null | ⬜ | 距上次观测的时长，如 `"10m"` |
| `attributes` | object | ⬜ | 类型特有属性（见下） |
| `labels` | object | ⬜ | 键值标签 |

> **⚠️ `status` 与 `observability` 是两个独立字段，禁止混用**（修正成员 C 在 `frontend/FRONTEND_DESIGN.md` §2 Q14 提出的字段歧义）。
>
> | 字段 | 谁产生 | 语义 | 消费方 |
> |---|---|---|---|
> | `status` | **来源系统**（ZSvirt 或探针） | 资源"本身"的业务状态 | 前端状态标签与颜色 |
> | `observability` | **B** | B 对"自己观测能力"的判断 | 排障、`includeStale` 过滤 |
>
> 举例：一个 VM 被 ZSvirt 报告为 `status = running`，但探针断线 20 分钟 →
> `status = running`, `observability = stale`。**两者同时为真是正常的**，这正是不该合并的原因。
>
> `status = unknown` 是一等公民：来源系统不可达时必须显式降级，**不得伪造 `running`**。

### 4.2 类型特有属性（`attributes`）

> **来源标注规则**（依据 ZSvirt 源码 `ZSvirt/zsvirt` 实测 + 成员决策 2026-09-17）：
>
> | 标记 | 含义 |
> |---|---|
> | `[ZSvirt-资产]` | 来自 ZSvirt 的 GPU/资源**资产** API（`QueryGpuDevice` / `QueryMdevDevice` / `QueryVmInstance`）—— **只有静态资产字段** |
> | `[ZWatch]` | 来自 ZSvirt `zwatch` 监控模块的**指标** API（**premium 模块，需确认测试环境是否启用**） |
> | `[探针]` | 来自成员 A 的 VM 内探针（访客视角） |
> | `[模拟]` | 来自 B 的模拟/降级 Provider（赛题明文要求支持） |

| kind | 关键属性 | 来源 | 状态 |
|---|---|---|---|
| `host` | `cpuCores`, `memTotalBytes`, `clusterId` | `[ZSvirt-资产]` | 【待确认】以实际返回为准 |
| `gpu` | `serialNumber`, `memTotalBytes`（显存容量）, `power`, `isDriverLoaded`, `pciAddress`, `model` | `[ZSvirt-资产]` | ⚠️ **源码已证实：`QueryGpuDevice` 只有这些静态字段，没有利用率/实时占用/温度** |
| `gpu` | `utilizationPct`, `memUsedBytes`, `temperatureC` | `[ZWatch]` | ⚠️ **依赖 premium `zwatch`；若测试环境不启用则不可得** |
| `gpu` | `memUsedBytes`（访客可见的 vGPU 占用）, `processes[]` | `[探针]` | 仅当 vGPU 透传且 VM 内可用 `nvidia-smi` 时可得 |
| `vgpu` | `profile`, `memQuotaBytes`, `ownerVmId`, `mdevType` | `[ZSvirt-资产]` | `QueryMdevDevice` / `QueryVmInstanceMdevDeviceSpecRef` |
| `vm` | `uuid`, `vcpu`, `memBytes`, `hostId`, `state`, `ipAddresses[]` | `[ZSvirt-资产]` | 【待确认】 |
| `container` | `image`, `runtime`, `restartCount`, `cpuLimit`, `memLimitBytes`, `oomKilledCount` | `[探针]` | 【待确认】 |
| `process` | `pid`, `ppid`, `cmdline`, `rssBytes`, `cpuPct` | `[探针]` | 【待确认】 |
| `ai_service` | `framework`, `modelName`, `endpoint`, `concurrency`, `qps` | `[探针]` | 【待确认】 |
| `agent` | `agentType`, `sessionId`, `taskRef` | `[探针]` | 【待确认】 |

**除 GPU 外，其余 `attributes` 仍为 TBD**：须以 ZSvirt API 实际返回与 A 探针实际采集能力为准，成员 B 不预设。

#### 4.2.1 GPU 数据的可插拔 Provider 设计（【已确认】，2026-09-17）

**决策**：GPU/vGPU 数据采用**三层分工 + 可插拔 Provider**。

| 层 | 数据 | 来源 | 角色 |
|---|---|---|---|
| **L1 资产层** | GPU 型号、显存容量、序列号、vGPU 切分、VM↔vGPU 绑定 | ZSvirt `QueryGpuDevice` / `QueryMdevDevice` | **权威**。回答"谁的卡、切给谁" |
| **L2 性能层** | GPU 利用率、显存占用、温度、功耗 | ZWatch metric API | 若测试环境提供 |
| **L3 访客层** | VM 内可见的 vGPU 显存 total/used、占用进程 | A 探针在 VM 内执行 `nvidia-smi` | **交叉验证** |
| **降级层** | 模拟 GPU 指标 | B 的 `SimulatedProvider` | **赛题明文要求**，解决开发机无 GPU |

**为什么必须同时做 L1 + L2/L3（GPU 归因的关键）**：

> 只采一个来源时，诊断无法区分下面两种情况 —— 而这正是赛题「创新性」维度点名的 **GPU 归因**：
>
> | 主机 GPU 显存 | 本 VM 的 vGPU 占用 | 根因推断 | 建议 |
> |---|---|---|---|
> | 98% | 20% | **邻居干扰**（同宿主其他 VM 抢占） | 检查同宿主其他 VM 的 vGPU 配额 |
> | 98% | 95% | **自身超配** | 降低并发 / 限制 batch size |
>
> 单一来源会让这两种情况看起来完全一样，导致错误归因。

**实现约束**：`zsvirt-adapter` 内定义 `GpuMetricsProvider` 接口，提供 `ZWatchProvider` / `GuestSmiProvider` / `SimulatedProvider` 三个实现，由配置切换。

**诚实性红线**：
1. `GET /api/health` 的 `gpuProvider.mode` 必须暴露当前渠道（`zsvirt-zwatch` / `guest-smi` / `simulated`）。
2. **模拟数据必须可识别**，不得伪装成真实采集数据进入诊断证据链而不加标记。
3. 若 L2 不可得，GPU 利用率类证据在 `Diagnosis.evidence` 中必须标注来源为模拟，或该规则不参与评分。

---

## 5. 事件与告警对象

### 5.1 `Event` — 不可变观测记录

事件是**只追加、不修改**的事实记录，是诊断的证据来源。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 事件唯一 ID（建议 `evt_` + ULID，单调时间有序） |
| `occurredAt` | timestamp | ✅ | 事件实际发生时间（产生方给出） |
| `receivedAt` | timestamp | ✅ | B 接收时间 |
| `source` | enum | ✅ | `zsvirt` \| `probe` \| `derived` |
| `resourceId` | string | ✅ | 关联资源 ID（可能是占位 `unresolved`） |
| `type` | string | ✅ | 事件类型，如 `gpu.memory.high`、`inference.timeout` |
| `severity` | enum | ✅ | `info` \| `warning` \| `error` \| `critical` |
| `message` | string | ⬜ | 人类可读描述 |
| `metrics` | object | ⬜ | 随事件携带的数值观测 |
| `raw` | object | ⬜ | 原始上报载荷（保留可追溯性） |
| `traceId` | string | ⬜ | 若来自分布式追踪 |

> **`type` 命名规范【提案】**：`{domain}.{subject}.{qualifier}`，全小写点分，如 `gpu.memory.exhausted`、`vm.disk.io_saturated`、`agent.network.timeout`。**类型名属于契约，改名等于破坏性变更。**

### 5.2 `Alert` — 有状态的判断

告警由事件/指标经规则产生，**有生命周期**。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 告警 ID |
| `ruleId` | string | ✅ | 触发它的规则 ID（保证可解释） |
| `resourceId` | string | ✅ | 告警对象 |
| `severity` | enum | ✅ | 同 `Event.severity` |
| `state` | enum | ✅ | `firing` \| `acked` \| `resolved` \| `silenced` |
| `firstFiredAt` | timestamp | ✅ | 首次触发 |
| `lastFiredAt` | timestamp | ✅ | 最近触发 |
| `resolvedAt` | timestamp \| null | ⬜ | 恢复时间 |
| `count` | integer | ✅ | 聚合计数（去重后的触发次数） |
| `evidenceEventIds` | string[] | ✅ | **指向触发本告警的证据事件** |
| `aggregationKey` | string | ✅ | 聚合/去重键 |
| `diagnosisId` | string \| null | ⬜ | 关联诊断（若有） |

**红线**：`evidenceEventIds` 为必填。没有证据的告警不允许存在。

### 5.3 `Diagnosis` — 根因结论

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 诊断 ID |
| `createdAt` | timestamp | ✅ | 生成时间 |
| `trigger` | object | ✅ | 触发方式：`{ alertId }` 或 `{ window, anchorResourceId }` |
| `rootCause` | string | ✅ | 根因候选码，如 `GPU_MEMORY_EXHAUSTED`；无匹配时 `UNKNOWN` |
| `confidence` | number | ✅ | **规则评分**，`0.0–1.0`，非统计学概率；必须可解释 |
| `confidenceBreakdown` | object[] | ✅ | 各规则的得分贡献，**保证 confidence 可追溯** |
| `affectedResources` | string[] | ✅ | 影响范围（按资源图传播得出） |
| `evidence` | object[] | ✅ | 证据列表，见下 |
| `recommendation` | string[] | ✅ | 处置建议（枚举码 + 可读文案） |
| `ruleSetVersion` | string | ✅ | 产出该结论的规则集版本（结论可复现的前提） |

`evidence` 条目：

```json
{ "type": "metric", "name": "gpu_memory_usage", "value": "98%", "resourceId": "gpu:zsvirt:...", "at": "..." }
{ "type": "event",  "name": "inference_timeout", "count": 12, "eventIds": ["evt_..."] }
```

**红线**：
- `evidence` 不得为空。空证据的诊断只能返回 `rootCause = UNKNOWN`。
- `confidence` 必须能由 `confidenceBreakdown` 复算出来。
- 必须携带 `ruleSetVersion`，否则结论不可复现。

---

## 6. 实体关系

```
Host 1──* GPU 1──* vGPU? 1──* VM 1──* Container 1──* Process
                                  │                      │
                                  └──────* Process ──────┘
                                              │
                                              *── AIService 1──* Agent 1──* Task?
Resource 1──* Event          （resourceId）
Resource 1──* Alert          （resourceId）
Alert    1──0..1 Diagnosis    （diagnosisId）
Alert    *──* Event          （evidenceEventIds）
Diagnosis *──* Event / 指标观测（evidence）
```

关系存储：**邻接表**（`resource_edge(parent_id, child_id, relation_type)`）。
【待确认】是否需要支持多重关系类型（如 `hosts`、`part-of`、`depends-on`），还是单一父子层级即可。

---

## 7. 生命周期与状态语义

> **重要**：本节的 `active` / `stale` / `gone` 属于资源的 **`observability` 字段**（B 的观测状态），
> **不是** `status` 字段。二者关系见 §4.1。

| 对象 | 状态机 |
|---|---|
| Resource `observability` | `active → stale → gone`（`stale` 由 `lastSeenAt` 超时判定，**不立即删除**） |
| Resource `status` | 由来源系统给出：`running` / `stopped` / `error` / `unknown` |
| Alert | `firing → acked → resolved`；旁路 `firing → silenced` |
| Event | 无状态，只追加，永不修改 |
| Diagnosis | 一次性生成，可被新诊断取代；**不修改既有记录** |

**删除策略**：资源不物理删除，只标记 `gone` 并保留历史，否则历史事件会变成孤儿，破坏可追溯性。

---

## 8. 事件类型枚举（草案，待三方冻结）

> 这是跨层关联的锚点。命名规范 `{domain}.{subject}.{qualifier}`，全小写点分。
> **最终枚举需三方共同确认，并同步到 `GET /api/v1/dict` 的 `eventType`。**

| 故障场景（赛题分类） | 事件类型草案 |
|---|---|
| **容器/进程异常** | `container.oom_killed`、`container.restart`、`process.crash`、`process.io_wait.high` |
| **GPU / 资源瓶颈** | `gpu.memory.exhausted`、`gpu.utilization.high`、`vgpu.quota.exceeded`、`vm.disk.io_saturated` |
| **应用/Agent 任务异常** | `inference.timeout`、`inference.error`、`agent.task.failed`、`agent.network.timeout`、`container.network.unreachable` |

---

## 9. 未决问题

| # | 问题 | 状态 |
|---|---|---|
| 1 | `vGPU` 是否为独立实体 | ⚠️ 源码表明确有 `MdevDevice` 概念，倾向**是**；仍需确认测试环境字段 |
| 2 | ZSvirt 对各资源类型实际暴露的字段清单 | ❌ 阻塞（Q15–Q20） |
| 3 | A 探针 `sourceId` 生成规则 | ✅ **已确认**（见 `API_CONTRACT.md` §3.3.1） |
| 4 | `task` 是否纳入首版模型 | ❌ 待定 |
| 5 | 事件 `type` 完整枚举 | ⚠️ 草案见 §8，待三方冻结 |
| 6 | 时间容差窗口取值 | ✅ **已确认 ±30s**（A 无异议） |
| 7 | 是否需要多租户 / 权限维度 | ❌ 待定（赛题提到"租户或项目"关联，可能纳入） |
| 8 | `status` 与 `observability` 字段歧义 | ✅ **已修正**（§4.1，采纳 C 的质疑） |
| 9 | GPU 指标渠道（ZWatch 是否可用） | ❌ 待命题方确认 |

---

## 10. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 首轮草案：资源层级、ID 方案提案、Event/Alert/Diagnosis 字段、生命周期 | DRAFT，待评审 |
| v0.2 | 2026-09-17 | **修正 C 指出的字段歧义**：拆分 `status`（业务）与 `observability`（观测），新增 `staleness`；**修正 GPU 字段来源**（源码证实 `QueryGpuDevice` 无利用率/实时占用），新增 §4.2.1 可插拔 Provider 设计；关闭 `sourceId` 与时间窗口待确认项；新增 §8 事件类型枚举草案（含容器 OOM） | 已确认 |
