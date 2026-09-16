# ZSvirt Observability — 测试计划

| 字段 | 值 |
|---|---|
| 文档状态 | **DRAFT v0.1 — 骨架 + 最小验收闭环定义，待契约冻结后补全** |
| 归属 | 全项目真源（产品文档） |
| 负责人 | 成员 B（B 侧测试）；全项目测试由三方共同认领 |
| 最后更新 | 2026-09-16 |

> 赛题要求「可复现」与「三类故障场景」。本文档把这两项要求落成可执行的测试条目。
> `【待确认】` = 需团队决策；`【待补充】` = 待契约冻结后填写。

---

## 1. 测试分层

| 层 | 范围 | 负责 | 执行时机 |
|---|---|---|---|
| L1 单元测试 | 诊断规则、评分、ID 解析、标准化映射 | B | 每次提交 |
| L2 集成测试 | 接入 → 标准化 → 落库 → 查询 全链路（本地数据库） | B | 每次提交 |
| L3 契约测试 | 按 `API_CONTRACT.md` 校验请求/响应结构与错误码 | B（对 A、对 C 双侧） | 每次提交 |
| L4 场景测试 | 三类故障端到端 | 三方联调 | 里程碑 |
| L5 联调测试 | A 探针 → B → C 页面 | 三方 | 里程碑 |
| L6 复现验收 | 从干净环境按 `DEPLOYMENT.md` 跑通 | 三方 | 交付前 |

---

## 2. 最小可验收闭环（MVP 判据）

> **这是"首轮开发完成"的定义，缺一不可。**

1. **接入**：A 探针按 §3.2 契约上报一批数据，B 返回 `accepted` 计数；脏数据被拒并给出原因。
2. **落库**：资源与事件可在数据库中查到，`vmId` 正确挂到 ZSvirt 同步来的 VM 节点下。
3. **拓扑**：`GET /api/v1/topology` 能返回 `host → gpu → vgpu → vm → container → ai_service` 的连通链。
4. **事件查询**：`GET /api/v1/events` 能按资源与时间窗过滤出上述事件。
5. **告警**：至少一条规则触发，产生 `Alert`，且 `evidenceEventIds` 非空。
6. **诊断**：`GET /api/v1/diagnosis/{id}` 返回 `rootCause` 非 `UNKNOWN`、`confidence` 可由 `confidenceBreakdown` 复算、`evidence` 非空、`affectedResources` 跨 ≥2 层。
7. **健康**：`GET /api/health` 在 ZSvirt 不可达时返回 `degraded` + `staleness`，而非 `ok`。
8. **可复现**：同一份夹具输入 + 同一 `ruleSetVersion` → 输出逐字节一致（诊断确定性）。

---

## 3. 三类故障场景用例

> 场景定义见 `backend/DIAGNOSIS_DESIGN.md` §5。以下为测试条目。

### 场景一：GPU 显存耗尽

| 项 | 内容 |
|---|---|
| 前置 | ZSvirt 集群可用；VM 内已有容器化 AI 负载；探针在线 |
| 注入 | 【待确认】由成员 A 提供注入脚本 |
| 期望告警 | GPU 显存相关告警进入 `firing` |
| 期望诊断 | `rootCause = GPU_MEMORY_EXHAUSTED`；`affectedResources` 含 gpu/vgpu/vm/container/ai_service |
| 期望证据 | ≥1 条指标证据 + ≥1 条事件证据 |
| 期望建议 | 非空，且与根因相关 |
| 恢复 | 停止注入后告警转 `resolved`，诊断可重新运行 |
| 反例测试 | GPU 显存正常时**不得**输出该根因 |

### 场景二：VM 磁盘 I/O 饱和

| 项 | 内容 |
|---|---|
| 注入 | 【待确认】`fio` / `dd` 干扰脚本 |
| 期望诊断 | `rootCause = VM_DISK_IO_SATURATED` |
| 反例测试 | I/O 指标正常而延迟高 → 应指向 `MODEL_COMPUTE_BOUND`，不得误报 I/O |

### 场景三：Agent / 容器网络异常

| 项 | 内容 |
|---|---|
| 注入 | 【待确认】网络限制 / DNS 故障脚本 |
| 期望诊断 | `rootCause = NETWORK_UNREACHABLE`（**前提：B 拥有网络层证据**） |
| 诚实性测试 | 若 B 无网络层资源数据 → 必须返回 `UNKNOWN` + 已有证据，**不得猜测** |

---

## 4. 负向与边界测试（防止"看起来很聪明"的错答）

| # | 用例 | 期望 |
|---|---|---|
| N1 | 完全无事件 | `rootCause = UNKNOWN`，`confidence = 0`，`evidence = []` |
| N2 | 事件引用的 `vmId` 不存在 | 挂 `unresolved` 占位并计入健康指标，不丢数据、不报 500 |
| N3 | 两个候选根因得分接近 | 双方降权，且 `confidenceBreakdown` 反映冲突扣分 |
| N4 | 症状时间早于"根因"时间 | 该项作为反证扣分 |
| N5 | ZSvirt API 超时 | `/api/health` 报 `degraded`；拓扑接口返回上次快照 + `staleness`，不返回 500 |
| N6 | 上报体字段缺失 | `422 SCHEMA_VALIDATION_FAILED` + 精确定位缺失字段 |
| N7 | 重复上报同一 `batchId` | 幂等：不产生重复事件 |
| N8 | `limit` 超过上限 | `400 INVALID_ARGUMENT`，不静默截断 |
| N9 | 时间窗 `from > to` | `400 INVALID_ARGUMENT` |
| N10 | 诊断无匹配规则 | `UNKNOWN` + 证据，`ruleSetVersion` 仍写入 |

---

## 5. 契约测试

| 方向 | 内容 |
|---|---|
| A → B | 用 A 的**真实样例载荷**做 fixture；Schema 变更时该用例必须失败 |
| B → C | 用 `API_CONTRACT.md` 的示例响应做 golden file；字段变更必须同步更新 |
| 错误码 | 逐条覆盖 `API_CONTRACT.md` §2.3 的错误码表 |

> **原则**：契约测试失败 = 契约被破坏，**不允许通过修改测试绕过**，必须先改契约文档并通知受影响成员。

---

## 6. 可复现性验收（赛题硬要求）

| 项 | 要求 |
|---|---|
| 干净环境启动 | 从全新 Ubuntu 按 `DEPLOYMENT.md` 跑通，无个人机器私有步骤 |
| 故障注入脚本 | 每个场景一条命令可复现 |
| 数据夹具 | 每个场景一份可回放的固定数据 |
| 演示时长 | 3–5 分钟完整演示，含故障注入 → 告警 → 诊断 → 定位 |
| 恢复演示 | 每个场景都有恢复步骤与验证 |

---

## 7. 待确认 / 待补充

1. 三方各自负责的测试用例认领（A 侧探针测试、C 侧页面测试）。
2. 注入脚本与夹具的提供者（`DIAGNOSIS_DESIGN.md` S4）。
3. CI 是否引入、用什么（【待确认】，属于依赖决策）。
4. 覆盖率目标【待确认】。
5. 演示是实时注入还是数据回放（`DIAGNOSIS_DESIGN.md` S5）。

---

## 8. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 骨架：测试分层、MVP 验收闭环、三场景用例、负向测试、复现要求 | DRAFT |
