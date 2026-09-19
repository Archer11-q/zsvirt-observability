# 事件类型覆盖确认（成员 A → 全员）

| 字段 | 值 |
|---|---|
| 文档状态 | **已确认（2026-09-19）** —— 回应 `DEVELOPMENT_PLAN.md` §5.4.1 ⬜「事件 `type` 覆盖确认」 |
| 归属 | `agents/docs/`（成员 A） |
| 依据 | `CONTRACT_FREEZE.md` F-01（17 项枚举，✅ FROZEN）、`PROBE_DESIGN.md`、`DEVELOPMENT_PLAN.md` X-04/X-05/X-08 |
| 配套样例 | [`agents/probe/fixtures/`](../probe/fixtures/README.md)（9 项探针侧事件的目标格式载荷） |

## 结论（一句话）

**17 项里，探针侧 13 项：4 项已实现、9 项已给目标格式样例（其中 4 项真实采集待 X-08、1 项待实现、4 项场景依赖见下表）；其余 4 项由 B 侧平台自产。** 无一项被 A 静默放弃；所有"待"都有明确的外部依赖编号。

## 逐项确认表

### 已实现（探针采集器在跑，样例已给）

| `type` | severity | 采集器 | 实现方式（与代码逐字核对） | 样例 |
|---|---|---|---|---|
| `container.oom_killed` | critical | `collector/container.py` | 监听 Docker `events`（`oom` / `die`） | `container_oom_killed.json` |
| `container.restart` | warning | `collector/container.py` | 监听 Docker `events`（`restart`）；`restartCount` 在资源 attributes | `container_oom_killed.json` |
| `process.crash` | error | `collector/process.py` | `/proc` pid 集合与上轮 `_known` 比对，消失即报 | `container_oom_killed.json` |
| `process.io_wait.high` | warning | `collector/process.py` | `/proc/<pid>/stat` state=`D`（不可中断睡眠）瞬时检测 | `gpu_memory_exhausted.json` |

**实现与冻结语义的已知差距（不阻塞，列为增强项）**：

- `process.io_wait.high`：F-01 的语义是"I/O 等待**持续**偏高"（应带阈值与持续时间，
  F-01 约定"阈值与持续时间放 `metrics`"）。当前实现是 state=`D` 瞬时检测——
  没有"持续"窗口、`metrics` 为空，且进程持续 D 态时每轮（5s）重复报。
  增强方向：按 `/proc/<pid>/stat` 的 CPU 时间差分计算等待比例，超阈值才报并带
  `metrics`（阈值 + 窗口）。样例 `gpu_memory_exhausted.json` 中的
  `metrics`（`io_wait_pct`/`threshold_pct`/`window_sec`）即目标格式。

### 待 X-08（AI 工作负载日志规范，目标格式样例已给）

依赖 B 向命题方确认 **X-08**：日志路径 + 格式。落地前，`agents/probe/fixtures/agent_network_failure.json`
已给出这 4 项的**目标格式**（字段、severity 遵循 F-01），B 的契约测试与场景三诊断规则可先行开发。

| `type` | severity | 规划的采集方式 |
|---|---|---|
| `inference.timeout` | error | 解析推理服务日志/指标中的慢请求（阈值写 metrics，不写 type 名） |
| `inference.error` | error | 解析推理服务日志中的 5xx / 错误响应 |
| `agent.network.timeout` | error | Agent 框架日志中的外部调用超时 |
| `agent.task.failed` | error | Agent 任务状态日志（重试耗尽） |

### 待实现（不依赖 X-08，A 的下一步）

| `type` | severity | 实现方案 | 备注 |
|---|---|---|---|
| `container.network.unreachable` | error | 探针周期性 TCP 连通性探测（容器内服务端口），失败差分产生事件 | 样例已给目标格式（`agent_network_failure.json`）；探测目标列表从 ai_service 端点派生 |

### 场景依赖（非默认交付范围）

| `type` | severity | 状态 | 依据 |
|---|---|---|---|
| `vm.disk.io_saturated` | warning | **可选场景**（D-071 降级）：可经 `/proc/diskstats` util 差分实现，赛题非必需 | `DIAGNOSIS_DESIGN.md` §5.4 |
| `gpu.memory.exhausted` | critical | **主责在 B**（D-028 GPU 三层分工：zsvirt-adapter / ZWatch / 探针 / 模拟）。探针的 `collector/gpu.py` 已预留：若 X-04 确认 VM 内直通 GPU 且可用 `nvidia-smi`，A 可启用本地采集作为补充渠道 | X-04 |
| `gpu.utilization.high` | warning | 归 B（ZWatch 渠道，X-05 是否启用待命题方） | X-05 / X-07 |
| `vgpu.quota.exceeded` | critical | 归 B（ZSvirt 平台侧配额数据，探针在 VM 内无法观测） | D-028 |

### B 侧平台自产（4 项，`source = derived`，与 A 无关）

`ingest.clock_drift.high` / `ingest.resource.unresolved` / `zsvirt.sync.failed` / `gpu.provider.degraded`

## 与 FREEZE_ACK 三条专业意见的对应

| 意见 | 本文档的回应 |
|---|---|
| ① inference/agent 事件依赖日志规范 | 上表「待 X-08」4 项 + 目标格式样例已交付，B 不必等规范才动测试 |
| ② vmId 来源待定 | 探针 `config.validate()` 已强制 `vm:zsvirt:<uuid>` 形态（fail-fast，防 B 整批 422）；注入方式仍待 X-09 |
| ③ Docker socket 读权限 | 决定 `container.oom_killed` / `container.restart` 的**真实可采性**；无权限时探针自动降级为仅进程采集，事件缺失是环境问题而非代码缺陷 |

## 给 B 的集成测试建议

1. `normal.json` 直接 POST → 期望 `accepted.resources=7, events=0`、无 `rejected`。
2. `container_oom_killed.json` → 期望 3 事件入库并触发告警规则（规则集 `rs-alert-1.0.0` 的容器 OOM 规则）、自动诊断产出 `CONTAINER_MEMORY_LIMIT`。
3. `gpu_memory_exhausted.json` 与 GPU 模拟渠道（任务 5 的 `SimulatedProvider`）**按 `sentAt` 时间线叠加** → 验证跨层关联（D-037 ±30s 窗口）。
4. `agent_network_failure.json` → 验证场景三规则链（`agent.network.timeout` 为根因候选）与 `resourceRef` 到 `agent`/`task` 资源的图边。
5. 同一批次**重复提交** → 验证 Q2 幂等（`duplicate: true` 且结果一致）。
