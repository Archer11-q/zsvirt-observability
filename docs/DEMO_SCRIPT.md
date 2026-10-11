# Crosslayer — 演示脚本（3–5 分钟）

> **目标**：让评委在 5 分钟内看到「跨层关联 → 可解释根因」这条主线的**每一环都有出处**，
> 而不是听一段功能清单。
>
> **诚实前提**：演示机没有 GPU，因此 GPU 数据来自**模拟渠道**
> （`SimulatedProvider`）。标注落在三个**可查证**的地方，不是一句口头声明：
>
> | # | 位置 | 演示环境的值 | 真实部署的值 |
> |---|---|---|---|
> | 1 | `GET /api/health` → `components.gpuProvider.mode` | `simulated` | `zsvirt-zwatch` / `guest-smi` |
> | 2 | 每条诊断证据的 `source`（`DiagnosisEvidence.source`） | `simulated` | `zwatch` / `probe` |
> | 3 | 资源属性 `attributes.origin`（拓扑里点开 GPU / vGPU） | `simulated` | `zwatch` |
>
> 第 2 条的链路是：演示载荷在事件的 `raw.origin` 里带 `simulated` →
> `app/diagnosis/service.py::_evidence_source` 映射成证据 `source`。
> `EventSource` 只有 `zsvirt` / `probe` / `derived` 三种（契约冻结），
> 装不下"渠道"，所以走 `raw` —— 这与"不改冻结契约"并不冲突。
>
> **把模拟数据说成真实采集是这个项目最严重的信任事故。** 录制前请按下面
> 「录制操作手册」的三条自检，亲眼确认这三处真的显示 `simulated`。

| 字段 | 值 |
|---|---|
| 时长 | 5 分钟（其中讲解 3.5 分钟，留 1.5 分钟提问） |
| 前置 | 已完成 `docs/DEPLOYMENT.md` §3 的环境搭建 |
| 一次性准备 | `./deploy/start.sh --demo`（**只迁移 + 启动，不灌数据**，见下节） |
| 演示端口 | `8080` —— `deploy/start.sh` 与 `deploy/inject_fault.sh` 的默认值 |

---

## 录制操作手册（录制者先看这一节）

### 为什么要人工录

赛题要的是「**真实环境** 3–5 分钟录屏」，旁白必须是真人的声音。工具链没有
语音旁白能力，也不该由工具去扮演讲解者。所以录制这件事需要一个人坐在机器前，
按下面的时间轴操作，同时口播对应小节的旁白。

### 一次性准备（两个终端）

**终端 A —— 服务（跑起来就不要再动它）**

```bash
cd ~/archer/workspace/zsvirt-observability

pg_isready -h 127.0.0.1 -p 5432          # 必须 accepting connections
cd backend && ./.venv/bin/python -m app.demo reset && cd ..
#   ↑ 清空上一次留下的数据。**不清空的话注入会变成幂等回放**，
#     屏幕上看不到任何变化（batchId 按场景名固定，这是 A 的 Q2 幂等语义）

./deploy/start.sh --demo
#   预期第一屏就打印：
#     演示基线：SIMULATED_DATA_ENABLED=true  GPU_PROVIDER=simulated  ALERT_RECOVERY_AFTER_SEC=0
#   ↑ 这一行必须出现。没有它说明你用的是旧的启动方式，
#     那么 §1 要讲的 simulated 会变成 auto，§4 的 firing 列表会是空的
```

**终端 B —— 操作（§1 之后的命令都在这里敲）**

```bash
cd ~/archer/workspace/zsvirt-observability
```

浏览器另开一个窗口放前端（`http://localhost:5173`，启动方式见 `DEPLOYMENT.md`）。

### 录制前三条自检（**不要跳过**）

```bash
# ① 渠道必须是 simulated，不能是 auto
curl -s localhost:8080/api/health | python3 -m json.tool | grep -A4 gpuProvider

# ② 必须没有任何告警（干净基线 —— 这是 §6 负向基线的前提）
curl -s 'localhost:8080/api/v1/alerts?state=firing' | python3 -m json.tool | head -5

# ③ 拓扑必须是「7 节点 6 边」那条完整链
curl -s 'localhost:8080/api/v1/topology?depth=8' \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(len(d['nodes']), '节点', len(d['edges']), '边')"
```

三条都对了再按录制键。任何一条不对，先修好再录 —— 现场改代码的镜头比不录更糟。

### 屏幕布局建议

| 区域 | 内容 | 理由 |
|---|---|---|
| 主区 | 浏览器：拓扑页 | 评委的眼睛应该始终有画面可看 |
| 左下 | 终端 B（命令 + JSON 输出） | 证伪成本最低的证据展示 |
| 右下 | `curl …/api/health` 的输出，**保持常驻** | §1 要指着它说话，来回切窗口会显得心虚 |

字号调大：JSON 输出在 1080p 录像里 12px 是不可读的。

### 时间轴（按这个顺序，旁白对着左侧小节念）

| 时间 | 操作（终端 B） | 屏幕上应看到 | 旁白 |
|---|---|---|---|
| 0:00–0:15 | 无 | 拓扑页 | §0 一句话开场 |
| 0:15–0:45 | `curl -s localhost:8080/api/health \| python3 -m json.tool` | `gpuProvider.mode = simulated`、`scheduler` 组件 | §1 |
| 0:45–1:30 | `curl -s 'localhost:8080/api/v1/topology?depth=8' \| python3 -m json.tool \| head -40` | 7 节点 / 6 边，`isStale=false` | §2 |
| 1:30–3:00 | `./deploy/inject_fault.sh gpu_self_exhausted` 然后 `gpu_neighbor_contention` | 两条告警**规则相同、诊断不同** | §3 |
| 3:00–4:00 | `cd backend && ./.venv/bin/python -m app.demo demo --cross-layer` | 一条跨层结论 | §3.5 |
| 4:00–5:00 | `./deploy/inject_fault.sh container_oom` / `network_failure` | 三条告警；其中**两条**指向同一个 `diagnosisId`，`warning` 那条按设计不自动诊断 | §4 |
| 5:00–5:45 | `curl -s localhost:8080/api/v1/diagnoses/<id> \| python3 -m json.tool` | 三集影响范围互不重叠 | §5 |
| 5:45–6:15 | 静默 + `./deploy/inject_fault.sh healthy` | 静默期内不新建告警；正常负载零告警 | §6 |
| 6:15–6:30 | 无 | 无 | §7 收尾 |

> 表里的总时长写到了 6:30，是为了留出**口令卡顿与命令回显**的余量。
> 成片要求 3–5 分钟，所以实际录制时 §2 / §3.5 可以压掉一半旁白；
> **不要压缩的是 §3（归因区分）与 §6（负向基线）** —— 这两段是评委真正想看的。

### 录制后校验

| 检查 | 方法 |
|---|---|
| 片长 3–5 分钟 | 播放器看时长 |
| 旁白与画面一致 | 抽 §3 那一段对一遍：屏幕上两个根因确实不同 |
| 没有露出敏感信息 | 确认画面里没有数据库口令、内网 IP、`backend/.env` 内容 |
| 出处可查 | 确认讲过的话都能在 `docs/` 里找到对应文档 |

### 常见故障与对策

| 现象 | 原因 | 对策 |
|---|---|---|
| `?state=firing` 返回空 | 告警被自动恢复关掉了 | 确认启动时打印了 `ALERT_RECOVERY_AFTER_SEC=0`；用 `--demo` 重启 |
| 注入后屏幕没变化 | 库里已有同一场景的批次（幂等回放） | `python -m app.demo reset` 后重录 |
| 拓扑里全是离线符号 | 打开了 `RESOURCE_STALE_AFTER_SEC` | 演示模式必须为 `0` |
| `health` 报 `mode=auto` | 没走 `--demo` 基线 | 用 `./deploy/start.sh --demo` 重启 |
| `inject_fault.sh` 说服务不可达 | 服务没起来或不在 8080 | 先 `curl localhost:8080/api/health` |

### 进阶：让「容器/进程异常」用**真实**容器而不是模拟载荷

上面 §4 注入的是 B 按契约**构造**的载荷（证据 `source=simulated`）。
如果时间允许，让评委看到**真容器真的被 OOM Killer 杀掉**会更有说服力：

```bash
# 终端 C：起一个真容器（CPU 可跑的真实 Transformer 推理服务）
docker compose -f deploy/sample-workload/compose.yaml --profile starved up -d --force-recreate

# 终端 B：真采集（驱动成员 A 的探针采集器，走容器运行时 API）
python3 deploy/sample-workload/collect_real_events.py --watch 5
```

预期屏幕上出现：

```text
container.oom_killed  critical  容器 demo-inference-starved 被 OOM Killer 终止
└─ 指标 {"exitCode": 137, "container_memory_limit_bytes": 536870912, "oomKilledCount": 8}
```

Crosslayer 侧：告警 `R-CTR-OOM-010` firing，根因 `CONTAINER_MEMORY_LIMIT` 置信 **0.9**，
证据 `source=probe`（**真实采集**，不是 `simulated`）。

讲这一段的台词：

> "刚才那三条告警的证据是我们按契约构造的，标着 `simulated`。
> 这一段换成真容器：这个推理服务被 512 MB 的限额压死了，`OOMKilled=true`、
> 退出码 137、重启了 8 次 —— 全部来自容器运行时 API 的真实读数。
> 根因和置信度跟模拟路径给出的**一样**，区别只在证据是不是真的发生过。"

⚠️ **两条必须如实说明**：

1. 成员 A 的容器采集器目前采不到容器事件（四条缺陷，见
   [`PROBE_DEFECT_SYNC.md`](PROBE_DEFECT_SYNC.md)），所以现在要先经
   `collect_real_events.py` 的桥接层绕过去；
2. **GPU 显存那两类仍然是模拟** —— 本机没有 NVIDIA 设备，这一点不要含糊过去。

### 明确不要做的事

- **不要说"这是真实 GPU 数据"**。想讲的是关联与推理能力，不是硬件。
- **不要隐藏模拟来源**。X-07 的答复明确要求区分真实采集 / 真实回放 / 人工模拟；
  声明过再演示是合规的，不声明才是问题。
- **不要念稿式地逐条读 JSON**。指着字段说结论，让画面自己作证。

---

## 无旁白录屏脚本（字幕版，约 3 分 30 秒）

> **官方没有要求旁白。** 任务书对交付要求 g 的原文只有一句：
>
> > 「g. 真实环境演示视频：提供 **3-5 分钟录屏**，展示**部署、正常观测、故障注入、
> > 跨层关联和诊断输出**的完整过程。」
>
> 全文没有出现"旁白 / 讲解 / 语音 / 配音"。**所以人声不是必需项。**
>
> 但有一件**必须做**的事，它在官方第一轮答复里被明确点名（第 13 条，原文）：
>
> > 「建议在采集记录、API 响应、界面、诊断证据和**视频中一致标明来源**，
> > 并区分真实采集、真实数据回放和人工模拟。」
>
> 请注意「**和视频中**」—— 来源标注是视频本身的要求。旁白通常顺手承担了这件事，
> 所以**不用旁白就必须用屏幕字幕顶上**。下面这套字幕就是干这个的。

### 一条铁律：常驻来源角标

录制全程在画面角落保留一个角标（用任何录屏工具的文字贴纸/水印即可）：

```text
┌──────────────────────────────┐
│ 证据来源                      │
│ 容器事件  probe（真实采集）    │
│ 平台层    代为上报（X-09）     │
│ GPU 指标  simulated（模拟）    │
└──────────────────────────────┘
```

这一段角标**同时满足**第 13 条的"在视频中标明来源"和"区分真实采集 / 回放 / 模拟"。
它比旁白更可靠：旁白说错了没法回改，角标一直在那儿。

### 字幕表（左边是时间，中间那列就是原样打在屏幕上的字）

| 时间 | 画面 | 屏幕字幕 | 操作 |
|---|---|---|---|
| 0:00–0:20 | 标题页 + 架构图（分层链 `宿主→GPU→vGPU→VM→容器→服务→Agent`） | **Crosslayer**<br>面向 ZSvirt 虚拟机内 AI 负载的跨层可观测与根因诊断<br><br><small>演示环境：开发机 Ubuntu（Docker Desktop WSL2）<br>GPU 指标为模拟；容器事件为真实采集</small> | 无 |
| 0:20–0:55 | 终端：启动服务 | **① 部署：一条命令**<br>`./deploy/start.sh --demo`<br><br><small>迁移 → 启动 → 健康检查全绿</small> | 敲命令，让 `演示基线：…` 那一行出现 |
| 0:55–1:30 | 浏览器：拓扑页 | **② 正常观测：链路是上报出来的**<br>7 节点 / 6 边，`宿主 → GPU → vGPU → VM → 容器 → 服务 → Agent`<br><br><small>这张图不是画的，由探针的 `parentSourceId` 逐条拼出</small> | 指拓扑图；停 2 秒让评委看清层次 |
| 1:30–2:25 | 终端：注入故障 → 浏览器告警页 | **③ 故障注入：注入 → 告警 → 诊断 全自动**<br>`./deploy/inject_fault.sh container_oom`<br><br><small>一次容器 OOM 产生 3 条告警<br>**2 条**指向同一个 `diagnosisId`；`warning` 那条按设计不自动诊断</small> | 注入；切到告警列表，指 `diagnosisId` 相同的两行 |
| 2:25–2:55 | 终端：跨层场景 | **④ 跨层关联：单来源做不到的那一步**<br>`python -m app.demo demo --cross-layer`<br><br><small>平台层看不到 VM 内的进程<br>探针看不到平台侧的显存<br>只有叠在同一条链上才能得出结论</small> | 跑命令；出示 `GPU_MEMORY_EXHAUSTED` |
| 2:55–3:30 | 浏览器：诊断详情 | **⑤ 诊断输出：根因 / 影响范围 / 建议**<br>根因 `CONTAINER_MEMORY_LIMIT`　置信 **0.9**<br>建议 `INCREASE_CONTAINER_MEMORY`<br><br><small>置信分由 `confidenceBreakdown` 逐项可复算<br>证据不足时返回 `UNKNOWN`，不猜</small> | 指三集影响范围（`affected` / `onChain` / `potentiallyAffected`） |

**时长口径**：官方写的是「**3-5 分钟**」。上表 3:30 落在区间内且留了余量；
**不要低于 3 分钟**（那是偏离交付要求），也没必要硬凑到 5 分钟。
如果时间宽裕，把 1:30–2:25 那段扩成"同时演示真实容器 OOM"（见上一节），
自然就到 4 分多。

### 想连字幕都不打？不建议

纯操作录屏（无字幕无旁白）在形式上不违规，但会丢掉第 13 条要求的**来源标注**，
而且评委无法知道 7 个节点意味着什么。**省掉旁白可以，省掉说明不可以。**
真的一分钟都不想加字的话，至少保留那条常驻来源角标 —— 它是硬要求。

### 用哪条路径录

| 路径 | 证据来源 | 建议 |
|---|---|---|
| `./deploy/start.sh --demo` + `inject_fault.sh` | `simulated`（构造载荷） | 稳、快、可复现，**适合 3 分钟版本** |
| `./deploy/start.sh`（不带 `--demo`）+ `collect_real_events.py` | `probe`（**真实采集**） | 更有说服力，**适合 4-5 分钟版本**；前提是 Docker Desktop 在跑 |

两条可以都录，剪在一起：先模拟走完全流程，再用真实容器补一段"这次证据是真发生的"。

---

## 0. 一句话开场（15 秒）

> "ZSvirt 上的 AI 负载出问题时，信号是散在宿主机、GPU、虚拟机、容器、服务五个层里的。
> 我们做的是把它们关联成**一条可解释的证据链**，给出根因、影响范围和处置建议 ——
> 而不是再堆一个监控面板。"

---

## 1. 环境与诚实性声明（30 秒）

```bash
curl -s localhost:8080/api/health | python3 -m json.tool | head -30
```

**指给评委看** `components.gpuProvider.mode`：

> "这里显示 `simulated`，因为演示机没有 GPU。赛题要求支持降级/模拟模式，
> 我们的做法是**把'这些数字是模拟的'做成结构上无法混淆的事实** ——
> 渠道名称会一路出现在健康检查、每条诊断证据的 `source` 字段、以及拓扑里
> 资源的 `origin` 属性上，不只是报告里的一句提示。
> 一个模拟读数被当成真实 GPU 数据来讲解，是这个项目最严重的信任事故。"

**接着指证据的来源字段**（这样"三处都标了"就不是空话）：

```bash
curl -s localhost:8080/api/v1/diagnoses | python3 -c "
import json,sys
for d in json.load(sys.stdin)['data']['items'][:1]:
    for e in d['evidence'][:3]:
        print(e['name'], '←', e.get('source'))"
```

> "注意每一条证据都有自己的 `source`。演示环境是 `simulated`，
> 真实部署下这里会变成 `zwatch` 或 `probe` —— 因为**证据链**才是结论的出处，
> 只在仪表盘上标一句'数据为模拟'是不够的。"

**同时指出** `components.database.status` 与 `ingest.unresolvedEvents`：

> "平台自己的状态也在可观测范围内。平台出问题却安静无声，会让所有下游结论失去可信度。"

---

## 2. 讲拓扑：链路是**真实上报**出来的（45 秒）

```bash
curl -s 'localhost:8080/api/v1/topology?depth=8' | python3 -m json.tool | head -40
```

或者直接看演示报告的第 ① 节（`./deploy/start.sh --replay` 的输出）。

> "七个节点、六条边，从宿主机到智能体。这张图不是手工画的 ——
> 它由探针上报的 `parentSourceId` 逐条拼出来，并同步落在邻接表里。
>
> 提一句踩过的坑：我们一度**只有 `parent_id` 没有邻接表**，
> 于是拓扑返回一堆孤立节点、告警计数恒为 0、诊断看不到任何邻居。
> 测试全绿，因为所有测试都自己手工建边 —— **测试自己造夹具就抓不到这类缺陷**。
> 现在有一条端到端用例专门盯住它。"

---

## 3. 场景一：GPU 归因的区分（核心能力，90 秒）

这是最该讲清楚的一段，因为**单来源采集做不到**。

```bash
./deploy/inject_fault.sh gpu_self_exhausted
./deploy/inject_fault.sh gpu_neighbor_contention
```

两条都会产生 `gpu.memory.exhausted` / `gpu.utilization.high` 告警，但诊断结论不同：

| 输入 | 根因 | 建议 |
|---|---|---|
| 宿主 98% + **本机 vGPU 95%** | `GPU_MEMORY_EXHAUSTED`（自己超配） | 降低推理并发 / 检查配额 |
| 宿主 98% + **本机 vGPU 20%** | `GPU_NEIGHBOR_CONTENTION`（邻居占用） | **检查同宿主其他虚拟机** |

> "两者的处置方式完全相反：前者要降自己的并发和 batch size，
> 后者要去找同宿主上别的负载。只看'显存满了'会把邻居的问题当成自己的问题，
> 运维改了半天配置却没有任何效果。
>
> 我们靠**指标对比 + 反证规则**区分：'本机 vGPU 占用很低'这条观测会**反对**
> '本机超配'这个结论。反证是规则数据的一部分，不是代码里的分支。"

**指着 `confidenceBreakdown` 给评委看**：

> "置信分不是黑盒概率，是规则加权评分，每条贡献都能逐项相加复算 ——
> 契约要求它必须可复算，我们的测试也断言了这一点。"

```bash
curl -s localhost:8080/api/v1/diagnoses | python3 -m json.tool | head -50
```

---

## 3.5 跨层场景：单来源采集做不到的那一步（60 秒）

```bash
cd backend
./.venv/bin/python -m app.demo demo --cross-layer
```

一条命令灌入**三份**批次：平台层（host → gpu → vgpu → vm）、成员 A 的探针样例
（VM 内的 `process.io_wait.high`）、B 的 GPU 渠道（`gpu.memory.exhausted`）。

> "这是本项目最该讲的一段。探针在 VM 里**看不到**平台侧显存，GPU 渠道**看不到**
> VM 内的进程。任何单一来源都只能看到一半 —— 只有把两者叠在同一条资源链上，
> 才能得出'这个容器的推理服务慢，是因为它那块 vGPU 显存被打满了'。
>
> 注意演示脚本用的是**成员 A 交付的真实样例载荷**，不是我们自造的数据。"

**对照说明（准备被问）**：只灌探针样例时**不会**得出 GPU 根因 ——
我们用一组对照测试把这一点固定下来（`test_cross_layer.py`），
证明结论来自关联而不是"看到 I/O 等待就报显存耗尽"。

> ⚠️ **如实说明（这一段建议主动讲，不要等被问）**：平台层与探针层之间的 ID 映射，
> **宿主 / GPU 段已经打通**了 —— `app/zsvirt/harvest.py` 把 ZWatch 读数映射成
> `host:zsvirt:{uuid}` / `gpu:zsvirt:{serial}` 并落图（D-101）。
> **VM 段还没有**：它需要 ZSvirt 的 VM UUID，而探针 `vmId` 的注入方式一直没拿到
> 答复（外部阻塞 X-09）。所以这个演示里 `vGPU → VM → 容器` 这一段仍是**代为上报**。
>
> "我们没有把这一段藏起来：`docs/REQUIREMENTS_GAP.md` 里逐条写着哪些是真桥接、
> 哪些是代为上报，以及缺哪一份材料才能补上。"

---

## 4. 场景二 + 三：一次事故一条结论（60 秒）

```bash
./deploy/inject_fault.sh container_oom
./deploy/inject_fault.sh network_failure
```

| 场景 | 期望根因 | 置信 |
|---|---|---|
| 容器被 OOM Killer 终止 + 反复重启 | `CONTAINER_MEMORY_LIMIT` | 0.9 |
| 容器网络不可达 + 智能体任务失败 | `NETWORK_UNREACHABLE` | 0.85 |

**这里要主动讲一个我们改过的设计**：

> "一次容器 OOM 会产生**三条**告警：`R-CTR-OOM-010`（critical）、
> `R-CTR-RESTART-011`（warning）、`R-AIS-ERROR-021`（error）。我们最初的实现是
> **每条告警诊断一次**，结果写出三条内容几乎相同的结论 —— 诊断列表变成噪声。
>
> 现在按'资源可达闭包相交'把告警聚成**事故**，一次事故一条结论。
>
> 注意这里只有**两条**指向同一个 `diagnosisId`：warning 那条我们**故意没有**
> 自动诊断 —— 低级告警数量多，自动跑会灌出一堆 `UNKNOWN` 把诊断列表冲淡。
> 它是可点的，用户想要结论可以一键触发。**该跳过的跳过，并说明跳过原因**，
> 比'全都诊断一遍显得很勤奋'更有用。"

```bash
curl -s 'localhost:8080/api/v1/alerts?state=firing' \
  | python3 -c "import json,sys; [print(a['ruleId'], a['severity'], '→', a['diagnosisId']) for a in json.load(sys.stdin)['data']['items']]"
```

**指出其中两条指向同一个 `diagnosisId`，并主动解释第三条为什么没有。**

实测输出形如（`diagnosisId` 只有两条非空）：

```text
  R-AIS-ERROR-021   error    → diag_01M4MM...
  R-CTR-RESTART-011 warning  → None          ← 低级告警，按设计不自动诊断
  R-CTR-OOM-010     critical → diag_01M4MM...
```

> "`warning` 那条是 `None`，不是漏了 —— 这是有意的（上面刚讲过）。
> 顺手提一句：接口会把跳过原因和数量**如实报出来**，
> 上报响应里的 `skippedBelowSeverity` 就是它。"

> ⚠️ 如果这条命令**返回空**：说明告警被自动恢复关掉了。演示模式
> （`./deploy/start.sh --demo`）把 `ALERT_RECOVERY_AFTER_SEC` 设为 `0`（关闭恢复）
> 正是为了避免这一幕 —— 演示数据的时间基准是固定的过去时刻（为了可复现），
> 恢复判据拿墙钟一比就全部"过期"，服务起来一个调度周期内 `firing` 会变空。

---

## 5. 影响范围与建议（45 秒）

```bash
curl -s localhost:8080/api/v1/diagnoses/<id> | python3 -m json.tool
```

指着三组集合：

> "影响范围分成三组，**互不重叠**：
> `affectedResources` 是有证据支持的和它的下游；
> `onChain` 是证据到锚点之间的过渡层；
> `potentiallyAffected` 是结构上相关但**没有证据**的。
>
> 为什么必须分开：如果把它们合并，'影响范围'就退化成全量拓扑，
> 等于什么都没说。这是我们和前端一起定的契约。
>
> 还要指出**不在**任何一组里的：离链的祖先（比如证据在 GPU 时的宿主机）。
> 它提供了证据所在的层，但它本身没有被影响。"

然后指 `recommendation`：

> "建议是**针对这个根因**的，不是通用套话。容器内存限额触顶给的是
> '提高容器内存限额或优化模型内存占用'。"

---

## 6. 处置闭环与负向基线（30 秒）

```bash
# 先取一条生效中告警的 id（不要手抄，现场抄错会很尴尬）
ALERT=$(curl -s 'localhost:8080/api/v1/alerts?state=firing' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['items'][0]['id'])")
echo "$ALERT"

# 静默它，再注入一次，验证静默期内不再打扰
curl -s -X POST "localhost:8080/api/v1/alerts/$ALERT/actions" \
     -H 'content-type: application/json' -d '{"action":"silence","silenceSeconds":600}'
./deploy/inject_fault.sh container_oom
```

> "静默期内不会新建告警，但**证据仍在累加** —— 静默是'别打扰我'，不是'停止观测'。
> 而且静默**不等于已解决**：如果把它当成自动关单，一个还在发生的问题会被悄悄关掉。"

```bash
./deploy/inject_fault.sh healthy    # 正常负载
curl -s localhost:8080/api/v1/alerts
```

> "正常负载**一条告警都不产生**。这条负向基线比正向用例更重要 ——
> 一个'什么都报警'的引擎看起来什么都能干，实际上等于没有告警。"

---

## 7. 收尾（15 秒）

> "整条链路是：探针按契约上报 → 规则自动产生告警 → 事故聚合 → 诊断给出根因、
> 影响范围与建议 → 回写到告警上。
>
> 所有结论都带 `ruleSetVersion`，规则改动后仍能回答'这条结论当时按什么规则得出'；
> 所有置信分都能由贡献项复算；没有证据时我们返回 `UNKNOWN` 而不是猜一个根因。"

---

## 附：常见提问

| 提问 | 回答要点 |
|---|---|
| GPU 数据是真实采集的吗？ | 演示机没有 GPU，走 `SimulatedProvider`，`origin=simulated` 全程可见；真实部署用 ZWatch 或 VM 内探针（`DATA_MODEL.md` §4.2.1） |
| ZWatch 接了吗？ | **接了**（`app/zsvirt/watch.py`）：认证会话 + 提前续期 + 元数据缓存 + 5 个 GPU 指标。X-05 / X-07 已关闭（命题方确认 ZWatch 已启用在用、模拟数据标注合规）。**响应形状是单一假设**：字段不符即抛 `ZWatchSchemaError`，不静默返回空 —— 字段样例仍在索要（X-12） |
| 置信分怎么来的？ | 规则加权评分，非统计学概率；`confidenceBreakdown` 逐项可复算；最高分 < 0.30 强制返回 UNKNOWN |
| 为什么不每条告警都诊断？ | 一次事故会产生多条告警，逐条诊断会刷出重复结论。按"资源可达闭包相交"聚成事故后诊断一次 |
| 为什么有告警没有诊断？ | `warning` 级告警不自动诊断（避免灌一堆 UNKNOWN），用户可按需一键诊断。响应里会如实报告跳过原因与数量 |
| 数据是模拟的，结论还有意义吗？ | 有意义的是**关联与推理过程**：同一套规则在真实数据上给出同样的结论形态。模拟只替换了输入来源，`origin` 字段把它标注清楚 |
| 用了哪些中间件？ | 只有 Python + FastAPI + PostgreSQL。刻意不引入 Kafka / Redis / Prometheus 等（`ADR-0003`），全部运行依赖 13 个 |
| 你怎么证明"模拟数据可识别"不是口号？ | 三处可查证：`health` 的 `gpuProvider.mode`、每条证据的 `source`、资源属性的 `origin`；回归测试在 `tests/test_provenance.py` |
| 为什么告警不会自动恢复？ | 演示模式把 `ALERT_RECOVERY_AFTER_SEC` 设为 `0`（关闭）。演示数据时间基准固定在过去，恢复判据拿墙钟一比就全部过期 —— 真实部署应设为 600 秒 |
| 平台层资源是真桥接还是代为上报？ | `宿主 → GPU → vGPU` 是真桥接（`zsvirt/harvest.py`，D-101）；`vGPU → VM → 容器` 是**代为上报**，缺探针 `vmId` 的注入方式（X-09）。见 `REQUIREMENTS_GAP.md` |

---

## 复现性声明

演示数据可复现：`SimulatedProvider` 用**显式种子**的独立随机源，场景参数固定，
时间基准固定为 `2026-09-17T12:00:00Z`。因此同一台机器上重复运行得到同样的数字，
讲解词不会与界面对不上。

**这个"固定在过去"的时间基准有一个必须知道的副作用**：告警自动恢复、资源
`stale` / `gone` 都是拿**墙钟**去比数据时间，于是演示数据一进库就已经"过期"。
所以演示模式（`./deploy/start.sh --demo`）显式关掉这两类判定：

| 配置 | 演示值 | 不关掉的后果（实测） |
|---|---|---|
| `ALERT_RECOVERY_AFTER_SEC` | `0`（关闭） | 服务起来一个调度周期内全部告警变 `resolved`，`?state=firing` 返回空 |
| `RESOURCE_STALE_AFTER_SEC` / `RESOURCE_GONE_AFTER_SEC` | `0`（关闭） | 拓扑上全部资源立刻变 `stale`，画面到处是离线符号 |

这三项的**真实部署值**见 `backend/.env.example`。

> ⚠️ `inject_fault.sh` 的 `batchId` 按场景名固定，**重复注入同一个场景是幂等回放**
> （不重复写事件 —— 这本身就是对成员 A 的 Q2 幂等语义的演示）。
> 想看到"重新注入产生新证据"，先 `cd backend && ./.venv/bin/python -m app.demo reset`，
> 或者换一个场景注入。

```bash
cd backend
./.venv/bin/python -m app.demo list                    # 列出场景
./.venv/bin/python -m app.demo demo --scenario container_oom
./.venv/bin/python -m app.demo reset                   # 清空（保留表结构）
```
