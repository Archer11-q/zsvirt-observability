# 交付要求覆盖与缺口

> 赛题《【智算云赛道】ZSvirt 赛题》交付要求 a–g 共七项。本文是**唯一**回答
> 「我们各自做到什么程度、缺的那部分为什么缺、什么条件下能补齐」的地方。
>
> 三条规矩：
>
> 1. **已完成**必须给出**仓库内的路径**，不接受"已经做了"这种说法；
> 2. **缺口**必须写清是**外部阻塞**还是**我们没做** —— 两者性质完全不同；
> 3. 不允许把"本地能跑通"写成"在 ZSvirt 环境里验证过"。
>
> 相关文档：[`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md) §6（外部阻塞登记）、
> [`DEPLOYMENT.md`](DEPLOYMENT.md) §7（演示环境）、[`DEMO_SCRIPT.md`](DEMO_SCRIPT.md)（录制脚本）。

---

## 1. 总览

| # | 赛题要求（摘要） | 状态 | 交付物位置 |
|---|---|---|---|
| a | 方案说明文档 | ✅ **完成** | `docs/` 全量（见 §2） |
| b | 可运行原型（Web 控制台，≥4 项能力） | ✅ **完成** | `backend/`、`frontend/`、`deploy/start.sh` |
| c | 虚拟机内探针 / 采集组件 | ✅ **完成** | `agents/`（含开销、权限、卸载说明） |
| d | 样例 AI 工作负载 | 🔶 **配置完成 + 已在开发机上真实跑通；未在 ZSvirt VM 内实跑** | `deploy/sample-workload/` |
| e | 三类故障场景与证据 | 🔶 **三类全部实现；「容器/进程异常」已用真实容器跑通，另两类仍为模拟** | `backend/app/zsvirt/scenarios.py`、`deploy/sample-workload/collect_real_events.py` |
| f | 可复现材料 | ✅ **完成** | 见 §4 |
| g | 真实环境 3–5 分钟演示视频 | ⛔ **未产出** | 脚本就绪：`docs/DEMO_SCRIPT.md` |

**一句话结论**：a / b / c / f 四项可交付且自洽；**d / e 完成到"配置 + 可复现证据"，
差最后一步"在命题方环境里真实跑一遍"；g 依赖 d / e 与一名能录音的录制者。**

---

## 2. a. 方案说明文档 —— ✅ 完成

| 要求 | 文档 |
|---|---|
| 总体架构 | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| 部署边界 | [`DEPLOYMENT.md`](DEPLOYMENT.md)、[`COMPATIBILITY.md`](COMPATIBILITY.md) |
| 数据模型 | [`DATA_MODEL.md`](DATA_MODEL.md) |
| 采集方式 | [`../agents/docs/PROBE_DESIGN.md`](../agents/docs/PROBE_DESIGN.md)、`DATA_MODEL.md` §4.2.1 |
| 资源关联逻辑 | [`DATA_MODEL.md`](DATA_MODEL.md) §3、[`backend/DIAGNOSIS_DESIGN.md`](backend/DIAGNOSIS_DESIGN.md) §3 |
| 告警策略 | [`backend/ALERT_DESIGN.md`](backend/ALERT_DESIGN.md) |
| 事件关联与诊断流程 | [`backend/DIAGNOSIS_DESIGN.md`](backend/DIAGNOSIS_DESIGN.md) |
| 与 ZSvirt 的集成点 | `ARCHITECTURE.md` §5、[`API_CONTRACT.md`](API_CONTRACT.md) §3 |
| 权限 / 安全设计 | [`SENSITIVE_DATA.md`](SENSITIVE_DATA.md)、`DEPLOYMENT.md` §4.2 |

## 3. b. 可运行原型 —— ✅ 完成

赛题点名要求"至少完成资源拓扑、工作负载健康、告警/事件详情和一次根因关联展示"：

| 赛题点名能力 | 实现 | 端点 |
|---|---|---|
| 资源拓扑 | 前端拓扑页 + 可达闭包裁剪 | `GET /api/v1/topology` |
| 工作负载健康 | 前端工作负载卡片（含 `rootCauseScope`） | `GET /api/v1/workloads` |
| 告警 / 事件详情 | 告警列表 / 详情 / 证据展开 / 事件反向链路 | `GET /api/v1/alerts`、`/events` |
| 根因关联展示 | 诊断详情（三集影响范围 + 可复算置信分） | `GET /api/v1/diagnosis/{id}` |

一键启动：`./deploy/start.sh --demo`；录制路径见 [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md)。

## 4. c / f —— ✅ 完成

**c. 探针**：`agents/`。纯标准库、零第三方依赖；
性能开销、权限需求、卸载方式逐项写在 `agents/docs/PROBE_DESIGN.md`。

**f. 可复现材料**：

| 要求 | 位置 |
|---|---|
| 源代码 | 仓库 `develop` 分支 |
| 部署脚本 | `deploy/start.sh`、`deploy/inject_fault.sh` |
| 配置样例 | `backend/.env.example` |
| README | [`../README.md`](../README.md) |
| 开源许可证 | `LICENSE` |
| 第三方依赖清单 | [`DEPENDENCIES.md`](DEPENDENCIES.md) |
| 最小化环境要求 | `DEPENDENCIES.md` §6、[`TECH-BASELINE.md`](TECH-BASELINE.md) §5.1 |

---

## 5. d. 样例 AI 工作负载 —— 🔶 开发机已真实跑通，未在 ZSvirt VM 内实跑

**已完成，且已在开发机上真实跑通**（`deploy/sample-workload/`）：

| 文件 | 内容 |
|---|---|
| `Dockerfile.inference` | CPU 可跑的推理服务镜像；模型构建期下载并 **SHA256 校验**后固化 |
| `bake_model.py` | 模型固化与校验（校验失败即构建失败，不让"权重悄悄变了"混过去） |
| `serve.py` | 真实预训练 Transformer（DistilBERT / SST-2，ONNX 量化版）推理服务 |
| `compose.yaml` | 两个 profile：`normal`（2 GiB）/ `starved`（512 MiB）——**故障旋钮就是内存限额** |
| `loadgen.sh` | 负载生成器：`steady` / `burst` / `broken` |
| `collect_real_events.py` | 驱动成员 A 的探针采集器，把**真实**容器故障上报到 Crosslayer |
| `README.md` | 逐个命令的跑法、实测数据与诚实边界 |

**实测记录（2026-09-30，开发机 Docker）**：

| 项 | 结果 |
|---|---|
| 推理真实性 | `{"label":"POSITIVE","score":0.999,"tokens":14,"elapsed_seconds":0.012}` —— 真实 Transformer 前向 |
| 健康 profile 内存 | RSS 739 MB（限额 2 GiB） |
| 故障注入 | `OOMKilled=true` / `ExitCode=137` / `RestartCount` 递增 / `MemSwap==MemLimit`（已禁用 swap） |
| 事件流 | `oom → die(exitCode=137)` 反复出现 |
| Crosslayer 结论 | 告警 `R-CTR-OOM-010` firing；根因 `CONTAINER_MEMORY_LIMIT` 置信 **0.9**；建议 `INCREASE_CONTAINER_MEMORY`；证据 `source=probe` |

**一次实测驱动的技术栈修正**：本目录最早的版本写的是 `pip install vllm==0.8.5`。
实测该版本在 PyPI 上只有 **CUDA wheel**，无 GPU 时 `import vllm` 失败；
而 `torch` 的 CPU 索引虽然可达，wheel 实际落在 `download-r2.pytorch.org`，
容器内**网络不可达**。也就是说那份配置的注释写着"CPU 可跑"，实际在演示机上跑不起来
—— 正是本项目一直在防的那类缺陷。现改为 ONNX Runtime + 预训练 ONNX，依赖从数 GB 降到约 90 MB。

**缺口**：没有在命题方的 **ZSvirt 虚拟机**里实际运行过（开发机上已跑通）。

| 原因 | 性质 |
|---|---|
| 宿主机上没有可用的 Linux 虚拟机，GPU 直通虚拟机需命题方协助创建（**X-13**） | 外部阻塞 |
| 基础镜像 Ready 的只有 CentOS 7.9（qcow2）与 Windows Server 2019（ISO），无预装 AI 负载的 Linux 镜像（**X-03 部分关闭**） | 外部阻塞 |
| 命题方侧材料已确认**无法取得** | 外部阻塞 |

**能自行补齐到什么程度**（自有 VMware + Ubuntu 可用）：

可以在自有 Ubuntu 里跑完 `deploy/sample-workload/` 全流程，得到**真实的容器 OOM /
重启 / 网络不可达事件**。但必须如实标注两点：

1. 资源 `origin` 是 `probe`（VM 内探针）或 `simulated`（B 构造），**不是** `zsvirt-zwatch`；
2. 平台层资源（host / gpu / vgpu）仍是代为上报 —— `vGPU → VM → 容器` 这一段的
   真实 ID 桥接待 **X-09**（见 §8）。

## 6. e. 三类故障场景与证据 —— 🔶 一类已用真实容器跑通，两类仍是模拟

赛题规定的三类场景**全部实现且逐类给出四项产物**（告警、关联证据、影响范围、处置建议）：

| 赛题场景 | 演示场景名 | 根因 | 置信 | 建议 |
|---|---|---|---|---|
| 容器 / 进程异常 | `container_oom` | `CONTAINER_MEMORY_LIMIT` | 0.90 | 提高限额 / 优化模型内存占用 |
| GPU 或资源瓶颈 | `gpu_self_exhausted` | `GPU_MEMORY_EXHAUSTED` | 0.35 | 降低并发 / 检查配额 |
| GPU 或资源瓶颈（归因区分） | `gpu_neighbor_contention` | `GPU_NEIGHBOR_CONTENTION` | — | 检查同宿主其它虚拟机 |
| 应用 / Agent 任务异常 | `network_failure` | `NETWORK_UNREACHABLE` | 0.85 | 检查容器网络与上游依赖 |
| **负向基线** | `healthy` | 无 | — | **不产生任何告警** |

复现方式：`./deploy/start.sh --replay`（一键打印全部场景报告），
测试侧见 `tests/test_multi_scenario.py`、`tests/test_cross_layer.py`。

**已升级的一项**（2026-09-30）：**「容器/进程异常」这一类现在是真实证据** ——
`deploy/sample-workload/` 里真的跑着一个容器化推理服务，真的被 512 MiB 限额压死，
真的 `OOMKilled`，由成员 A 的采集器（经桥接层绕开其四条缺陷）从容器运行时 API
读到并上报，Crosslayer 给出 `CONTAINER_MEMORY_LIMIT`（置信 0.9）与处置建议。
与模拟演示路径的结论**一致**，区别只在证据来源：`probe`（真实）而非 `simulated`。

**剩余缺口**：另外两类（GPU 或资源瓶颈、应用/Agent 任务异常）**仍是模拟 / 回放**。

| 原因 | 性质 |
|---|---|
| 没有 ZSvirt 虚拟机可注入（X-13） | 外部阻塞 |
| 平台告警记录为空，无法演示"关联 ZSvirt 告警"（X-11） | 外部阻塞 |
| 探针 `vmId` 注入方式未定，无法锚定真实 VM（X-09） | 外部阻塞 |

**能自行补齐到什么程度**：在自有 Ubuntu 上注入容器 OOM / 网络故障，这三类中的
**两类半**（容器异常、网络异常、以及 CPU 内存瓶颈）都能拿到真实事件；
**GPU 显存那两类仍然只能是模拟** —— 本机与自有 VM 都没有 NVIDIA 设备。
这一点在录制时必须说明，不能含糊。

## 7. g. 真实环境演示视频 —— ⛔ 未产出

| 项 | 状态 |
|---|---|
| 录制脚本 | ✅ [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md)（含旁白、时间轴、预期输出、常见提问） |
| 录制操作手册 | ✅ `DEMO_SCRIPT.md` §0「录制操作手册」 |
| 环境前提 | ⛔ 依赖 d / e（命题方 VM） |
| 录制者 | ⛔ **需要人来录** —— 旁白必须是人的声音；且演示目标环境不在本机 |

**关于"能不能自动录"**：不能。原因有两条，都不是技术怠工：

1. 演示数据在**本机**，而赛题要的是"真实环境演示"，本机没有 GPU、也没有 ZSvirt；
2. 旁白需要真人语音，工具链没有这个能力。

**降级方案（如果最终拿不到命题方环境）**：在自有 Ubuntu 上录制，片头用
15 秒明确声明"本次演示在自建 Ubuntu 环境运行，GPU 数据来自模拟渠道，
平台层资源为代为上报"。**声明过再演示是合规的；不声明才是问题**（X-07 答复
明确要求区分真实采集 / 真实回放 / 人工模拟）。

---

## 8. 已登记但**不会**在本次交付里补齐的实现缺口

如实列出，避免评审时被当成隐瞒。这些都有决策记录，不是遗漏。

| # | 缺口 | 影响 | 决策 / 阻塞 |
|---|---|---|---|
| 1 | **VM 层命名空间桥接未实现**：`vGPU → VM → 容器` 仍是代为上报 | 跨层链在真实部署下会断在 VM 这一环 | **X-09**（外部阻塞）。宿主 / GPU 段已通（D-101） |
| 2 | **无法关联 ZSvirt 平台告警**：`GetAlarmData` / `QueryActiveAlarm` 实测查不到记录 | 赛题点名要求"关联 ZSvirt 告警"这一条无法演示 | **X-11**（外部阻塞） |
| 3 | `GuestSmiProvider` 未实现 | VM 内 `nvidia-smi` 渠道缺失 | **X-04**：GPU 为**直通**（非 MDEV），归因能力受限；VM 内能否 `nvidia-smi` 始终未获答复 |
| 4 | **告警类证据的来源恒为 `probe`** | `EvidenceKind.ALERT` 的证据无法标注渠道 | 已知。本次只修了**事件**证据的来源映射（见下），告警证据留待后续 |
| 5 | `shared` 作用域的推荐文案自相矛盾 | 标为 `shared`（非自身问题）的行，推荐文案仍写「本机超配」 | **用户明确决定不改**，记录在案 |
| 6 | 🔴 **成员 A 的容器采集器采不到任何容器事件** | `container.oom_killed` / `container.restart` 在真实采集路径上是**死数据**；赛题三类场景里的「容器/进程异常」在真实部署下无法演出来 | **四条独立缺陷叠加，全部被静默吞掉**（详见 [`PROBE_DEFECT_SYNC.md`](PROBE_DEFECT_SYNC.md)）。已在桥接层绕过以跑通场景，**修正需由成员 A 落到 `agents/probe/collector/container.py`** |
| 7 | 桥接层承担了三处本该在探针里的修正 | 真实部署走 socket，桥接层不在链路上 —— 换句话说**修好之前，真实部署仍然采不到容器事件** | 同上。桥接层那三段的**存在本身就是待办事项** |

> 第 4 条的背景：`EvidenceSource.SIMULATED` 与 `DiagnosisEvidence.source` 字段
> 一直存在，但 2026-09-30 实测发现**从来没有任何真实数据流填过它** ——
> 模拟 GPU 渠道产出的读数落库后是 `source="probe"`，与探针真实采集无法区分。
> 已修复事件证据这条路径（`app/diagnosis/service.py::_evidence_source`，
> 回归测试见 `tests/test_provenance.py`）；**告警证据那条路径尚未处理**。

---

## 9. 补齐路径（"什么条件下能补什么"）

| 如果能拿到… | 就能补齐 | 谁来做 |
|---|---|---|
| 一台带 GPU 直通的 Linux VM（X-13） | d 的真实运行、e 的真实注入、g 的真实演示 | 命题方 + 成员 A |
| 探针 `vmId` 的注入途径（X-09） | 跨层链在真实 ID 上闭合（缺口 1） | 命题方答复 + 成员 A/B |
| 至少一条平台告警记录（X-11） | "关联 ZSvirt 告警"（缺口 2） | 命题方 |
| ZWatch 响应字段样例（X-12） | 适配器的单一假设可被验证 | 命题方 |
| 一名能录音的录制者 | g 的视频产出 | 队伍 |
| 自有 Ubuntu（**已具备**） | 容器 / 网络类真实故障注入；GPU 类仍为模拟 | 队伍 |
| 成员 A 修掉容器采集器的四条缺陷 | 真实部署下「容器/进程异常」场景可用 | 成员 A |

---

## 10. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.1 | 2026-09-30 | **实测更新**：d 在开发机上真实跑通（真容器 / 真 OOM / 真采集）；e 的「容器/进程异常」升级为真实证据；技术栈由 vLLM 改为 ONNX Runtime（实测 vLLM 无 GPU 不可用）；新增缺口 6/7（成员 A 容器采集器四条缺陷）|
| v1.0 | 2026-09-30 | 首版：a–g 逐项覆盖、d/e/g 缺口与原因、5 项记录在案的实现缺口、补齐路径 |
