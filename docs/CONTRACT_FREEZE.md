# Crosslayer — 契约冻结提案（第一轮）

| 字段 | 值 |
|---|---|
| 文档状态 | **待三方确认** —— 未冻结项不得开始实现 |
| 归属 | 全项目真源（产品文档） |
| 提出人 | 成员 B |
| 日期 | 2026-09-17 |
| 确认方 | 成员 A（`agents/`）、成员 C（`frontend/`） |
| 关联文档 | [`DECISIONS.md`](DECISIONS.md)（决策登记表）、[`API_CONTRACT.md`](API_CONTRACT.md)、[`DATA_MODEL.md`](DATA_MODEL.md) |

---

## 0. 怎么用这份文档

**目的**：把"必须三方共同拍板"的事项一次收齐，避免来回讨论。

**规则**：
1. **默认采用推荐值。** 每项都给了可直接使用的具体值。**若你同意，回复"F-0X 同意"即可**；
   若不同意，请给出你的值 + 一条理由（理由很重要，避免以后反复）。
2. **未冻结的项不得开始实现**。特别是 F-01（事件类型）—— 它是跨层关联的锚点，
   一旦代码里写死再改名就是破坏性变更。
3. 冻结后，本文件内容**回写**到各专题文档（`DATA_MODEL.md` / `API_CONTRACT.md`），
   并在 `DECISIONS.md` 中把状态改为 ✅ FROZEN。
4. 冻结后如需变更，走 [`CONTRIBUTING.md`](../CONTRIBUTING.md) §5 的契约变更流程。

**优先级**：F-01 > F-02/F-03 > F-04 > F-05 > F-06/F-07
（F-01 阻塞所有人；F-06/F-07 只阻塞各自模块，风险最低）

---

## F-01 ⭐ 事件类型完整枚举（最关键）

**为什么必须先冻结**：跨层关联靠事件 `type` 把宿主机、GPU、VM、容器、Agent 的异常串成一条链。
类型名写进代码后改名 = 破坏性变更，且会让已有数据和规则集失效。

**命名规范**：`{domain}.{subject}.{qualifier}`，全小写、点分。

### 推荐值

#### 场景一 · GPU / 资源瓶颈

| `type` | 含义 | 产生方 | 默认 severity | 备注 |
|---|---|---|---|---|
| `gpu.memory.exhausted` | GPU / vGPU 显存耗尽 | ZSvirt / 探针 | `critical` | 场景一根因 |
| `gpu.utilization.high` | GPU 利用率持续高位 | ZSvirt (ZWatch) | `warning` | 需带阈值与持续时间 |
| `vgpu.quota.exceeded` | vGPU 配额被突破 | ZSvirt | `critical` | |
| `vm.disk.io_saturated` | VM 磁盘 I/O 饱和 | 探针 | `warning` | 供 §5.4 可选场景 |

#### 场景二 · 容器 / 进程异常

| `type` | 含义 | 产生方 | 默认 severity | 备注 |
|---|---|---|---|---|
| `container.oom_killed` | 容器被内核 OOM Killer 终止 | 探针 | `critical` | **场景二根因**；赛题原文点名"容器 OOM" |
| `container.restart` | 容器发生重启 | 探针 | `warning` | 需与重启计数区分：本事件为"发生了一次"，计数在资源属性 |
| `process.crash` | 进程异常退出（非零退出码 / 信号） | 探针 | `error` | |
| `process.io_wait.high` | 进程 I/O 等待时间持续偏高 | 探针 | `warning` | |

#### 场景三 · 应用 / Agent 任务异常

| `type` | 含义 | 产生方 | 默认 severity | 备注 |
|---|---|---|---|---|
| `inference.timeout` | 推理请求超时 | 探针 | `error` | |
| `inference.error` | 推理请求返回错误 | 探针 | `error` | |
| `agent.task.failed` | Agent 任务失败 | 探针 | `error` | 场景三症状 |
| `agent.network.timeout` | Agent 网络调用超时 | 探针 | `error` | 场景三根因候选 |
| `container.network.unreachable` | 容器网络不可达 | 探针 | `error` | 场景三根因候选 |

#### 平台 / 接入侧（B 自身产生，`source = derived`）

| `type` | 含义 | 产生方 | 默认 severity | 备注 |
|---|---|---|---|---|
| `ingest.clock_drift.high` | 探针时钟漂移超阈值 | B（`ingest`） | `warning` | 阈值 5s，见 D-060 |
| `ingest.resource.unresolved` | 事件引用了未知资源，挂到占位节点 | B（`ingest`） | `info` | 用于观测链路完整性 |
| `zsvirt.sync.failed` | ZSvirt 平台同步失败 | B（`zsvirt-adapter`） | `warning` | 数据陈旧度的来源 |
| `gpu.provider.degraded` | GPU 指标渠道降级为模拟 / 不可用 | B | `warning` | **诚实性要求**：模拟模式必须可见 |

### 事件设计约定（一并冻结）

| 约定 | 值 |
|---|---|
| `type` 是否可扩展 | 可新增，**但改名/删除属 `BREAKING`** |
| 阈值与持续时间写在哪 | **不写进 `type` 名**。`type` 只表达"是什么事"，阈值/持续时间放 `metrics` 或规则集 |
| 同义词处理 | 禁止同义不同名（如 `container.oom` 与 `container.oom_killed` 并存） |
| 字典端点 | 上述枚举必须出现在 `GET /api/v1/dict` 的 `eventType` 中，**含中文可读文案** |
| 可读文案 | 由 B 提供（见下表，可直接采用） |

### 可读文案（供 `GET /api/v1/dict` 返回）

| `type` | 中文文案 |
|---|---|
| `gpu.memory.exhausted` | GPU 显存耗尽 |
| `gpu.utilization.high` | GPU 利用率过高 |
| `vgpu.quota.exceeded` | vGPU 配额超出 |
| `vm.disk.io_saturated` | 虚拟机磁盘 I/O 饱和 |
| `container.oom_killed` | 容器内存溢出被终止 |
| `container.restart` | 容器重启 |
| `process.crash` | 进程异常退出 |
| `process.io_wait.high` | 进程 I/O 等待过高 |
| `inference.timeout` | 推理请求超时 |
| `inference.error` | 推理请求错误 |
| `agent.task.failed` | 智能体任务失败 |
| `agent.network.timeout` | 智能体网络超时 |
| `container.network.unreachable` | 容器网络不可达 |
| `ingest.clock_drift.high` | 采集时钟漂移过大 |
| `ingest.resource.unresolved` | 存在未识别资源 |
| `zsvirt.sync.failed` | 平台资源同步失败 |
| `gpu.provider.degraded` | GPU 数据渠道降级 |

---

## F-02 `rootCause` 码表

**规则**：`UPPER_SNAKE_CASE`。新增可，改名/删除属 `BREAKING`。
**无匹配时一律返回 `UNKNOWN`，不得编造。**

| `rootCause` | 含义 | 触发场景 | 归属 |
|---|---|---|---|
| `GPU_MEMORY_EXHAUSTED` | GPU/vGPU 显存被**本 VM 自身**用尽 | 场景一（自身超配） | 场景一 |
| `GPU_NEIGHBOR_CONTENTION` | GPU 显存被**同宿主其他 VM**占用（邻居干扰） | 场景一（归因区分） | 场景一 ⭐ |
| `GPU_UTILIZATION_SATURATED` | GPU 算力饱和（显存未必耗尽） | 场景一 | 场景一 |
| `CONTAINER_MEMORY_LIMIT` | 容器内存触及自身 limit 被杀 | 场景二 | 场景二 |
| `CONTAINER_RESTART_LOOP` | 容器陷入重启循环 | 场景二 | 场景二 |
| `VM_MEMORY_EXHAUSTED` | VM 层内存耗尽（根因上移） | 场景二反例 | 场景二 |
| `VM_DISK_IO_SATURATED` | VM 磁盘 I/O 饱和 | §5.4 可选场景 | 扩展 |
| `NETWORK_UNREACHABLE` | 网络不可达（容器网络 / VM 网络 / 依赖服务） | 场景三 | 场景三 |
| `AGENT_TASK_FAILURE` | Agent 任务失败（无更具体根因） | 场景三 | 场景三 |
| `MODEL_COMPUTE_BOUND` | 推理慢源于模型自身计算量 | 反例场景 | 扩展 |
| `UNKNOWN` | 证据不足或规则无匹配 | 兜底 | — |

**⭐ 关于 `GPU_MEMORY_EXHAUSTED` 与 `GPU_NEIGHBOR_CONTENTION` 拆成两个码的理由**：
这是本项目「GPU 归因」能力的直接体现，也是赛题「创新性」维度的得分点。
若合并为一个码，前端与运维无法区分"该找邻居还是该降自己的并发"，诊断建议会失去针对性。
判定依据见 `DATA_MODEL.md` §4.2.1（主机侧显存 vs 本 VM vGPU 占用）。

### 中文文案（供字典端点）

| `rootCause` | 中文文案 |
|---|---|
| `GPU_MEMORY_EXHAUSTED` | GPU 显存耗尽（本机超配） |
| `GPU_NEIGHBOR_CONTENTION` | GPU 争用（同宿主其他负载占用） |
| `GPU_UTILIZATION_SATURATED` | GPU 算力饱和 |
| `CONTAINER_MEMORY_LIMIT` | 容器内存限额触顶 |
| `CONTAINER_RESTART_LOOP` | 容器重启循环 |
| `VM_MEMORY_EXHAUSTED` | 虚拟机内存耗尽 |
| `VM_DISK_IO_SATURATED` | 虚拟机磁盘 I/O 饱和 |
| `NETWORK_UNREACHABLE` | 网络不可达 |
| `AGENT_TASK_FAILURE` | 智能体任务失败 |
| `MODEL_COMPUTE_BOUND` | 模型计算受限 |
| `UNKNOWN` | 未识别 |

---

## F-03 `recommendation` 码表

**规则**：`UPPER_SNAKE_CASE`。可读文案由 B 提供，前端不硬编码中文映射。

| `recommendation` | 含义 | 文案 | 关联根因 |
|---|---|---|---|
| `REDUCE_CONCURRENCY` | 降低推理并发 | 降低推理并发或 batch size | `GPU_MEMORY_EXHAUSTED`、`GPU_UTILIZATION_SATURATED` |
| `CHECK_GPU_ALLOCATION` | 检查 vGPU 分配 | 检查该虚拟机的 vGPU 分配与配额 | `GPU_MEMORY_EXHAUSTED`、`GPU_NEIGHBOR_CONTENTION` |
| `CHECK_NEIGHBOR_WORKLOADS` | 检查同宿主其他负载 | 检查同一宿主机上其他虚拟机的 GPU 占用 | `GPU_NEIGHBOR_CONTENTION` |
| `INCREASE_CONTAINER_MEMORY` | 提高容器内存限额 | 提高容器内存限额或优化模型内存占用 | `CONTAINER_MEMORY_LIMIT` |
| `CHECK_CONTAINER_RESTART` | 检查容器重启原因 | 检查容器退出码与重启日志 | `CONTAINER_RESTART_LOOP`、`process.crash` |
| `INCREASE_VM_MEMORY` | 增加虚拟机内存 | 为虚拟机扩容内存 | `VM_MEMORY_EXHAUSTED` |
| `LIMIT_IO_NOISY_PROCESS` | 限制 I/O 干扰进程 | 限制同机 I/O 干扰进程，或分离日志盘 | `VM_DISK_IO_SATURATED` |
| `SEPARATE_LOG_DISK` | 分离日志盘 | 将日志写入独立磁盘 | `VM_DISK_IO_SATURATED` |
| `CHECK_NETWORK_POLICY` | 检查网络策略 | 检查容器/虚拟机网络策略与安全组 | `NETWORK_UNREACHABLE` |
| `CHECK_DNS` | 检查 DNS | 检查 DNS 解析是否正常 | `NETWORK_UNREACHABLE` |
| `CHECK_DEPENDENCY_HEALTH` | 检查依赖服务健康 | 检查被调用服务是否可用 | `NETWORK_UNREACHABLE`、`AGENT_TASK_FAILURE` |
| `REVIEW_AGENT_RETRY_POLICY` | 检查 Agent 重试策略 | 检查智能体重试与超时配置 | `AGENT_TASK_FAILURE` |
| `PROFILE_MODEL` | 分析模型性能 | 对模型做性能分析，评估算力需求 | `MODEL_COMPUTE_BOUND` |
| `COLLECT_MORE_EVIDENCE` | 补充证据 | 证据不足，建议扩大时间窗或补采集 | `UNKNOWN` |

**约定**：`recommendation` 是**数组**（一个根因可给多条建议），**排序即优先级**。

---

## F-04 基础枚举确认

以下枚举已在契约中定义，**请确认无异议**（有异议请指出）。

### `severity`

| 值 | 文案 | 用途 |
|---|---|---|
| `info` | 提示 | 状态变化、链路完整性提示 |
| `warning` | 警告 | 指标越阈值、性能劣化 |
| `error` | 错误 | 请求失败、进程异常退出 |
| `critical` | 严重 | 服务不可用、资源耗尽、被 OOM 终止 |

### `alert.state`

| 值 | 文案 | 说明 |
|---|---|---|
| `firing` | 触发中 | |
| `acked` | 已确认 | 运维已认领 |
| `resolved` | 已恢复 | |
| `silenced` | 已静默 | 静默期内不再通知 |

### `resource.kind`

`host` / `gpu` / `vgpu` / `vm` / `container` / `process` / `ai_service` / `agent` / `task`

> `vgpu` 与 `task` 是否纳入首版见 F-04.1。

### `resource.status`（**业务状态**，来源系统给出）

`running` / `stopped` / `error` / `unknown`

### `resource.observability`（**B 的观测状态**，与上者独立）

`active` / `stale` / `gone`

> 这两个字段的分离是采纳成员 C 的质疑后确定的，见 `DATA_MODEL.md` §4.1。

### F-04.1 附带的三个待定项

| 项 | B 的推荐 | 请确认 |
|---|---|---|
| `vgpu` 是否作为独立 kind | **是**（ZSvirt 源码存在 `MdevDevice` 概念） | A / C |
| `task` 是否纳入首版 | **纳入**（Agent → Task 是赛题点名的层级） | A / C |
| 是否输出 `potentiallyAffected`（结构相关但无证据） | **输出**，与 `affectedResources` 分开 | C（前端是否展示） |

---

## F-05 敏感字段清单

**依据**：赛题要求「涉及容器、Agent 或业务日志时，须实现基本的敏感信息保护措施」。
详见 [`SENSITIVE_DATA.md`](SENSITIVE_DATA.md)。

### 建议「直接丢弃、不入库」

| 目标 | 匹配方式 | 责任方 |
|---|---|---|
| 环境变量类字段 | 键名匹配 `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` / `*_PASSWD` / `*_CREDENTIAL` / `AWS_*` / `OPENAI_*` | B（探针侧可先行过滤） |
| 凭据 / 证书 | `authorization` / `cookie` / `set-cookie` / `private_key` / `certificate` | B |
| 含密码的连接串 | 值匹配 `://[^:]+:[^@]+@` | B |
| 已知密钥形态 | 值前缀 `sk-` / `ghp_` / `AKIA` 等 | B |

### 建议「保留但掩码」

| 字段 | 处理 | 责任方 |
|---|---|---|
| `process.cmdline` | 参数值掩码，保留参数名 | B（`normalize`） |
| `Event.message` | 命中规则时掩码匹配片段 | B |
| `Event.raw` | 整体过脱敏管道；无法安全脱敏的键直接删除 | B |
| 身份标识（用户 ID / 手机号） | 单向哈希，保留可关联性 | B |
| 内网 IP | **B 建议保留**（拓扑关联需要）；请确认 | A / C |

### 需要成员 A 明确的

| 问题 | 为什么问 |
|---|---|
| 探针会采集哪些字段？（尤其 `attributes` 与 `raw` 的内容范围） | B 的脱敏规则要覆盖真实字段，否则可能漏网 |
| 探针侧是否愿意先做一层过滤（不采集明显敏感字段）？ | 双重防护更稳，且与赛题"低侵入"评分相关 |

---

## F-06 前端技术栈确认

成员 C 已提案（`frontend/FRONTEND_DESIGN.md` §1），**B 无异议，请 A 确认**：

| 层 | 选型 |
|---|---|
| 语言 / 框架 | TypeScript + React 18 + Vite |
| 数据请求 | TanStack Query |
| UI 组件 | Ant Design 5 |
| 拓扑 + 图表 | ECharts 5 |
| 状态管理 | Zustand |
| 测试 | Vitest + React Testing Library（+ Playwright 可选） |

**对 B 的约束（B 已知悉并接受）**：C 的轮询节奏为 `/health` 3s、列表 10–15s、诊断 30s，
因此 B 的查询接口必须**幂等 + 游标分页稳定**。

---

## F-07 探针技术选型确认

**这是成员 A 的决策范围，B 不预设、不干预。** 列在此处仅为让三方知道进度。

| 项 | 状态 |
|---|---|
| 语言（C / C++ / Go / Rust / Python…） | 🟡 待 A 明确 |
| 采集手段（/proc 解析 / cgroup / eBPF / 容器运行时 API） | 🟡 待 A 明确 |
| 断网缓冲实现（SQLite / JSONL，A 已在 Q6 提到） | 🟡 待 A 明确 |

**B 侧唯一的请求**：若探针使用 C/C++，请在 `agents/` 内自带构建说明（`CMakeLists.txt` + README），
以便其他成员和评委能复现构建（赛题"可复现性"评分项）。仓库 `.gitignore` 已保留 C/C++ 规则。

---

## 三方签字表

> 确认方式：回复"F-0X 同意"，或给出你的值与理由。全部确认后由成员 B 回写文档并标记 FROZEN。

| 项 | 成员 A | 成员 B | 成员 C |
|---|---|---|---|
| F-01 事件类型枚举 | ☐ | ✅ 提出 | ☐ |
| F-02 `rootCause` 码表 | ☐ | ✅ 提出 | ☐ |
| F-03 `recommendation` 码表 | ☐ | ✅ 提出 | ☐ |
| F-04 基础枚举 + F-04.1 三个待定项 | ☐ | ✅ | ☐ |
| F-05 敏感字段清单 | ☐ | ✅ | ☐ |
| F-06 前端技术栈 | ☐ | ✅ 无异议 | ✅ 提出 |
| F-07 探针技术选型 | ☐ | — （A 的范围） | — |

**备注**：F-06 与 F-07 属各自模块内部决策，风险最低；**建议优先确认 F-01～F-05**，
因为它们阻塞跨模块实现。

---

## 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-17 | 首版：F-01 事件类型枚举（17 项）、F-02 根因码表（11 项）、F-03 建议码表（14 项）、F-04 基础枚举、F-05 敏感字段清单、F-06 前端栈、F-07 探针选型 | 待三方确认 |
