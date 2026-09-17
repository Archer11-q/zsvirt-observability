# 成员 A · 契约冻结签字回执（F-01～F-07）

| 字段 | 值 |
|---|---|
| 回执人 | 成员 A |
| 日期 | 2026-09-17 |
| 对应文档 | `docs/CONTRACT_FREEZE.md` v0.1 |
| 结论 | **F-01～F-07 全部同意**，附 3 条专业意见（见 §3），不阻塞冻结 |

---

## 1. 逐项签字

| 编号 | 内容 | 结论 |
|---|---|---|
| **F-01** | 事件 `type` 完整枚举（17 项） | ✅ **同意** |
| **F-02** | `rootCause` 码表（11 项） | ✅ **同意** |
| **F-03** | `recommendation` 码表（14 项） | ✅ **同意** |
| **F-04** | 基础枚举 + 3 个待定项 | ✅ **同意**（3 个待定项均采纳 B 的推荐） |
| **F-05** | 敏感字段清单 | ✅ **同意**（回答 B 的两个问题，见 §2） |
| **F-06** | 前端技术栈 | ✅ **无异议** |
| **F-07** | 探针技术选型 | ✅ **已拍板：Python 3.12 零依赖**（见 §2） |

---

## 2. 需要 A 明确的问题答复

### F-04.1 三个待定项（采纳 B 推荐）

| 项 | A 的答复 |
|---|---|
| `vgpu` 是否独立 kind | **同意「是」**（ZSvirt 有 MdevDevice 概念，独立建模利于 GPU 归因） |
| `task` 是否纳入首版 | **同意「纳入」**（Agent → Task 是赛题点名的层级） |
| 是否输出 `potentiallyAffected` | **同意「输出」**（与 `affectedResources` 分开；前端展示与否由 C 定） |

### F-05 两个问题（B 点名要 A 答）

**问题 1：探针会采集哪些字段？**

- **resources 的 `attributes`**：严格按 `DATA_MODEL.md` §4.2 白名单——`container`（image、runtime、restartCount、cpuLimit、memLimitBytes）、`process`（pid、ppid、cmdline、rssBytes、cpuPct）、`ai_service`（framework、modelName、endpoint、concurrency、qps）、`agent`/`task`（agentType、sessionId、taskRef 等）。**白名单外的字段不采集、不上报**。
- **events**：`type`、`severity`、`message`、`metrics`、`raw`。其中 `raw` 携带采集时的原始载荷（如 Docker 事件体、日志行），**探针侧先删环境变量类字段**，最终由 B 的 `normalize` 脱敏管道兜底。

**问题 2：探针侧是否愿意先做一层过滤？**

- **愿意，双重防护**：① 不采集环境变量（`/proc/<pid>/environ` 不读）；② `cmdline` 中敏感参数（`--password` / `--token` / `--api-key` / `--secret` / `Authorization` 等）在探针侧掩码；③ `attributes` 白名单制。详见 `agents/docs/PROBE_DESIGN.md` §7。

**附带**：内网 IP **同意保留**（拓扑关联需要，与 B 建议一致）。

### F-07 探针技术选型（A 拍板）

| 项 | 决策 |
|---|---|
| 语言 | **Python 3.12**（与后端基线一致） |
| 依赖 | **零第三方依赖**（纯标准库），部署 = 复制脚本 |
| 采集手段 | `/proc` 解析 + Docker API + `nvidia-smi`（可选）；**不用 eBPF** |
| 断网缓冲 | **SQLite**（标准库 `sqlite3`）+ FIFO 断点续传 |

完整方案见 `agents/docs/PROBE_DESIGN.md`。

---

## 3. 三条专业意见（不阻塞冻结，提请 B 知悉）

1. **`inference.*` 与 `agent.*` 事件的采集前提**：这 4 类事件（`inference.timeout`、`inference.error`、`agent.task.failed`、`agent.network.timeout`）的采集，**依赖被观测的 AI 服务 / Agent 框架暴露日志或指标接口**。探针需要知道日志路径与格式。建议在联调阶段由团队明确「AI 工作负载的日志规范」（路径 + 格式），否则这几类事件只能靠端口/进程启发式，精度有限。

2. **`vmId` 来源**：探针上报的 `vmId`（ZSvirt VM UUID）需明确获取方式。建议 **ZSvirt 侧通过 cloud-init / 环境变量注入**，或探针读 `/sys/class/dmi/id/product_uuid`。这是 A 能否正确锚定到 Pull 资源的先决条件，建议列入 `X-04` 一并向命题方确认。

3. **Docker socket 读权限**：`container.*` 事件依赖 VM 内 `/var/run/docker.sock` 可读。若受限，探针退化为 cgroup 解析 + `/proc`，但会丢失 OOM/重启这类关键事件，请在 ZSvirt 环境确认 socket 挂载方式。

---

## 4. 确认汇总

**成员 A 对 `CONTRACT_FREEZE.md` v0.1 的 F-01～F-07 全部同意（含 F-04.1 三个待定项采纳 B 推荐），F-05 两个问题已答复，F-07 技术选型已拍板。附 3 条不阻塞冻结的意见。**

请成员 B 据此回写 `DECISIONS.md` 并标记 FROZEN。
