# 成员 A · VM 内探针技术方案（PROBE_DESIGN）

| 字段 | 值 |
|---|---|
| 状态 | **DRAFT v0.1** —— 成员 A 技术选型决策，待 B/C 知悉 |
| 归属 | 成员 A（agents/） |
| 决策人 | 成员 A |
| 对应契约 | `docs/API_CONTRACT.md` §3、`docs/DATA_MODEL.md` §3/§4/§5、`docs/CONTRACT_FREEZE.md` F-07 |
| 日期 | 2026-09-17 |

---

## 1. 技术选型决策（F-07）

| 项 | 决策 | 理由 |
|---|---|---|
| 语言 | **Python 3.12** | 与后端基线一致（`TECH-BASELINE.md` §2.1）；标准库足以覆盖探针全部需求 |
| 运行时依赖 | **零第三方依赖（纯标准库）** | 部署 = 复制脚本即可跑，无需 `pip install`；最大化「可复现」评分，契合 `ADR-0003` 最小依赖 |
| 采集手段 | **只读 /proc 解析 + Docker API + `nvidia-smi`（可选）** | 低侵入，不影响被观测的 AI 负载；**不用 eBPF**（本机 `unprivileged_bpf_disabled=2` 不可行） |
| 断网缓冲 | **SQLite（标准库 `sqlite3`）+ FIFO** | 原子写、崩溃安全；零额外依赖 |
| 上报 | **HTTP 批量上报**（标准库 `urllib.request`） | 契约已冻结（D-054） |
| 打包 | 纯脚本运行；可选 PyInstaller 单文件 | 默认零依赖脚本，PyInstaller 仅作可选交付形态 |

> **一句话**：探针 = 一个 Python 3.12 脚本 + 一个配置文件 + 一个 SQLite 文件，零 `pip` 依赖，丢进 VM 就能采集上报。

---

## 2. 模块架构

```
agents/
├── probe/
│   ├── __main__.py          # 入口：装配 + 主循环 + 信号处理
│   ├── config.py            # 配置加载（环境变量 > config.json > 默认值）
│   ├── model.py             # Resource / Event 数据类（对应契约字段）
│   ├── idgen.py             # batchId（ULID）+ sourceId 生成规则
│   ├── sensitive.py         # 前置脱敏过滤
│   ├── buffer.py            # SQLite 断网缓冲（FIFO）
│   ├── reporter.py          # 批量上报（幂等 + 部分接受处理）
│   └── collector/
│       ├── __init__.py      # 采集器集合 + 默认启用清单
│       ├── base.py          # 采集器基类
│       ├── procutil.py      # /proc 读取共享工具（cgroup 解析 / stat / cmdline）
│       ├── process.py       # 进程采集（crash + io_wait 窗口化 + parent 挂容器）
│       ├── container.py     # 容器采集（Docker socket 清单 + 事件）
│       ├── ai_service.py    # AI 服务识别（parent 挂容器）
│       ├── network.py       # 网络可达性探测（container.network.unreachable）
│       └── gpu.py           # GPU 采集（预留，默认不启用）
├── config.example.json      # 配置样例（JSON，不含任何真实凭据）
└── README.md                # 构建 / 运行 / 配置说明
```

**数据流**：采集器 → `model` 组装 → `sensitive` 过滤 → `buffer` 入队 → `reporter` 批量上报；上报失败回写 `buffer` 等待重试。

**并发模型**：主线程调度 + 每个采集器一个线程 + 独立上报线程，通过线程安全的 `queue.Queue` 解耦；`buffer`（SQLite）以单一写连接 + 锁串行化，避免多线程竞争。

---

## 3. 采集设计（只读、低侵入）

### 3.1 采集器清单

| 采集器 | 数据源 | 产出 | 频率 |
|---|---|---|---|
| `process` | `/proc/<pid>/stat` `status` `cmdline` `cgroup` | `process` 资源（parent 挂容器）+ `process.crash` `process.io_wait.high` | 5s 轮询 |
| `container` | Docker `/var/run/docker.sock` API | `container` 资源 + `container.oom_killed` `container.restart` | 清单 10s + 事件流实时 |
| `ai_service` | 进程 cmdline + 端口探测 | `ai_service` 资源（parent 挂容器）+ `inference.*` | 10s 轮询 |
| `network` | 容器内 AI 服务端口 TCP connect | `container.network.unreachable`（up→down 转换） | 15s 轮询 |
| `gpu`（可选） | `nvidia-smi` CSV | VM 内 GPU 指标（若直通） | 10s 轮询 |

> **GPU/vGPU 指标主责在 B 的 `zsvirt-adapter`**（DECISIONS D-028「三层分工 + 可插拔 Provider」）；探针仅在 VM 有 GPU 直通时补充采集，非主渠道。

### 3.2 关键采集手段（全部只读）

| 目标 | 手段 | 说明 |
|---|---|---|
| 进程清单 | 遍历 `/proc/<pid>/`，读 `stat`（pid/ppid/starttime/state）、`status`（VmRSS）、`cmdline` | `process.sourceId = starttime + pid`（对齐 D-031，避免 pid 复用冲突） |
| 容器清单 | Docker socket `GET /containers/json` | `container.sourceId = 容器 ID 前 12 位` |
| 容器事件 | Docker socket `GET /events`（filter `oom`/`die`/`restart`） | `container.oom_killed`（critical）、`container.restart`（warning） |
| 进程崩溃 | 对比相邻两次进程清单，进程消失且退出码非零 / 被信号终止 | `process.crash`（error） |
| I/O 等待 | `/proc/<pid>/stat` 的 `delayacct_blkio_ticks`（字段 42）差分占比 + 连续轮数 | `process.io_wait.high`（warning，带 `io_wait_pct`/`threshold_pct`/`window_sec`）；无 delayacct 内核退化 state=`D` |
| AI 服务识别 | 进程 cmdline 命中已知框架（vllm / triton / ollama / fastapi 等）+ 监听端口 | `ai_service.sourceId = 服务名 + 端口` |
| 推理事件 | 解析 AI 服务的访问/错误日志（配置日志路径）或健康端点探测 | `inference.timeout` / `inference.error` |
| Agent 事件 | 解析 Agent 框架日志 / 状态文件 | `agent.task.failed` / `agent.network.timeout` |
| 网络探测 | 容器内 AI 服务端口 TCP connect（可达 → 不可达转换） | `container.network.unreachable`（error，只报一次） |

> **进程/服务的资源归属**：容器内进程与 AI 服务经 `/proc/<pid>/cgroup` 解析出容器 ID 前 12 位，
> 作为 `parentSourceId` 挂到容器资源下 —— 为 B 的资源图提供 container→process / container→ai_service 边。

> **进程采集范围**：优先采集**容器内进程**（通过 `/proc/<pid>/cgroup` 归属判断）+ **直接运行于 VM 上的 AI 服务进程**（cmdline 命中框架特征），而非全量进程——否则 `process.crash` 会淹没在系统进程噪音里。

### 3.3 采集边界（重要，回应 F-05）

- **只采集契约声明的字段**：`DATA_MODEL.md` §4.2 的 `attributes` 白名单，未声明字段不采集、不上报。
- **不采集环境变量**：`/proc/<pid>/environ` 不读（环境变量是最高危的敏感数据源，见 F-05 问题 2 的答复）。
- **cmdline 前置掩码**：`--password=*`、`--token=*`、`--api-key=*` 等敏感参数在探针侧就掩码（见 §7）。

---

## 4. 上报设计（对齐契约 Q1–Q7 的落地）

### 4.1 批次组装

- 端点：`POST {ZSVIRT_OBS_BACKEND_URL}/api/v1/ingest/batch`，`Content-Type: application/json`。
- 触发：**默认 5s 一批**；事件实时优先（最多缓冲 1s）；达上限（`events ≤ 1000` / `resources ≤ 500` / ≤ 1MB）即发。
- `batchId`：**ULID**（契约 D-055），探针侧自实现最小 ULID 生成器（48 位毫秒时间戳 + 80 位随机数，Base32 编码，约 30 行）；重试复用同一 `batchId`。

### 4.2 至少一次投递（Q2）

- 上报失败（网络错误 / 5xx / 429）→ 批次保留在 `buffer`，按退避重试，`batchId` 不变 → B 幂等去重。
- `429 RATE_LIMITED` → 读 `Retry-After`，按指数退避。
- 重复批次返回与首次一致结果，视为成功。

### 4.3 部分接受处理（Q1 呼应）

- 解析响应 `rejected[]`，按 `index` 定位本批条目，记录到日志（便于定位探针 bug）。
- 资源引用（`resourceRef`）保证同批次或此前已 `accepted`，维护「已确认资源」缓存；极端竞态交给 B 的 `unresolved` 兜底。

### 4.4 时间戳（Q3）

- 所有时间戳 **ISO 8601 UTC 毫秒带 `Z`**（`time.time_ns()` 换算，`datetime` 格式化）。
- 每批带 `sentAt`，供 B 计算 `receivedAt − sentAt` 漂移（阈值 5s）。
- 时钟基准：优先 NTP；无 NTP 退化为 VM 本地时钟，启动时记录偏移假设。

---

## 5. 断网缓冲（Q6，SQLite）

| 项 | 设计 |
|---|---|
| 存储 | SQLite，WAL 模式，表 `pending(batch_id PK, payload, created_at, attempts)` |
| 顺序 | FIFO，按 `created_at` 升序补传 |
| 上限 | 可配置（默认 100MB 或 10000 批），超限丢最旧并计数 |
| 丢弃计数 | 作为 `probe.dropped_batches` 指标随后续批次上报 |
| 崩溃安全 | WAL + 事务提交，重启后自动续传 |

---

## 6. `sourceId` 生成规则（对齐 D-031）

| kind | `sourceId` 规则 | 稳定性来源 |
|---|---|---|
| `container` | 容器 ID 前 12 位 | 容器生命周期内不变 |
| `process` | `starttime + pid`（`/proc/<pid>/stat` 第 22 字段） | 避免 pid 复用冲突 |
| `ai_service` | `服务名 + 监听端口` | 服务实例稳定标识 |
| `agent` / `task` | ULID | 全局唯一 |

统一要求：同一 `agentId` 内唯一、生命周期内不变。`agentId = probe-<vm-uuid 前 8 位>`。

⚠️ **连接符必须用 `.`（非 `:`）**：B 侧把探针资源拼成全局 ID
`{kind}:probe:{agentId}:{sourceId}`（共四段），若 `sourceId` 含冒号会多出一段、
被 `backend/app/normalize/ids.py::probe_resource_id` 以 `InvalidResourceId` 拒收。
故 `process` 为 `12345.678`、`ai_service` 为 `vllm.8000`（见 `SENSITIVE_DATA.md` §8.4）。

---

## 7. 敏感信息前置过滤（回应 F-05 问题 2）

**探针侧愿意先做一层过滤**，双重防护：

1. **不采集**：环境变量（`/proc/<pid>/environ`）。
2. **掩码**：`cmdline` 中匹配 `--password/--token/--api-key/--secret/Authorization` 等键的参数值。
3. **白名单**：`attributes` 只报 `DATA_MODEL.md` §4.2 声明的字段，其余丢弃。
4. **`raw` 前置过滤**：`raw` 若携带环境变量类字段（`*_KEY`/`*_TOKEN` 等），探针侧一并删除，只保留诊断所需的原始载荷；最终兜底仍是 B 的 `normalize` 脱敏管道。

> 探针侧过滤是「第一道」，B 的 `normalize` 脱敏是「兜底」（B 不得假设 A 已脱敏）。

---

## 8. 配置（Q5）

| 变量 | 用途 | 默认 |
|---|---|---|
| `ZSVIRT_OBS_BACKEND_URL` | B 地址 | `http://localhost:8080` |
| `ZSVIRT_OBS_PROBE_TOKEN` | 可选 Bearer token | 无（默认关闭） |
| `ZSVIRT_OBS_VM_ID` | ZSvirt VM UUID（`vmId` 必填） | 无（启动报错） |
| `ZSVIRT_OBS_AGENT_ID` | 探针实例 ID | `probe-<vm-uuid 前 8 位>` |
| `ZSVIRT_OBS_BATCH_SEC` | 批次间隔 | `5` |
| `ZSVIRT_OBS_BUFFER_MAX_MB` | 缓冲上限 | `100` |

优先级：**环境变量 > 配置文件（`config.json`）> 默认值**。凭据只从环境变量注入，不进仓库。零依赖约束下配置文件用 JSON（标准库解析），不用 YAML。

---

## 9. 打包与部署

```bash
# 零依赖运行（推荐）
python3.12 -m probe            # 依赖：仅标准库，无 requirements.txt

# 可选：PyInstaller 单文件（交付形态，非必需）
# pyinstaller --onefile probe/__main__.py
```

运行要求：VM 内需有 **Python 3.12** 与 **Docker socket 读权限**（容器采集）；进程采集仅需 `/proc` 读权限。

---

## 10. 与契约对应关系

| 契约项 | 本方案落地 |
|---|---|
| Q1 资源引用 | §4.3 已确认资源缓存 + 同批次保证 |
| Q2 至少一次 | §4.2 batchId（ULID）复用 + 退避重试 |
| Q3 时间戳 | §4.4 ISO 8601 UTC 毫秒 + sentAt |
| Q4 批量上限 | §4.1 5s/1s/1000/500/1MB |
| Q5 发现 B 地址 | §8 环境变量 + 配置文件 |
| Q6 断网缓冲 | §5 SQLite FIFO + 丢最旧计数 |
| Q7 鉴权 | §8 可选 Bearer token，默认关闭 |
| D-031 sourceId | §6 |
| F-05 敏感字段 | §7 前置过滤 + 白名单 |

---

## 11. 风险与待明确

| # | 风险 | 处置 |
|---|---|---|
| R1 | `inference.*` / `agent.*` 事件依赖被观测对象暴露日志/指标 | 需明确 AI 服务与 Agent 框架的日志路径与格式（见下） |
| R2 | Docker socket 在 VM 内是否可读 | 若不可读，退化为 cgroup 解析 + `/proc` |
| R3 | VM 内是否有 GPU 直通（`nvidia-smi` 可用） | 依赖 ZSvirt 配置（DECISIONS X-04），无则 GPU 指标由 B 的 ZSvirt 侧提供 |
| R4 | `vmId` 从何获得 | 建议环境变量注入，或读 `/sys/class/dmi/id/product_uuid` |

---

## 12. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-17 | 首版：Python 3.12 零依赖探针方案、采集/上报/缓冲/sourceId/脱敏设计 | DRAFT |
