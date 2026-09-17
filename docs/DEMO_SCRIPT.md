# Crosslayer — 演示脚本（3–5 分钟）

> **目标**：让评委在 5 分钟内看到「跨层关联 → 可解释根因」这条主线的**每一环都有出处**，
> 而不是听一段功能清单。
>
> **诚实前提**：演示机没有 GPU，因此 GPU 数据来自**模拟渠道**
> （`SimulatedProvider`）。这一点会在报告、`GET /api/health` 与每条证据的
> `origin` 字段里**明确标注**，不会伪装成真实采集。真实部署下该字段是
> `zsvirt-zwatch` 或 `probe`。

| 字段 | 值 |
|---|---|
| 时长 | 5 分钟（其中讲解 3.5 分钟，留 1.5 分钟提问） |
| 前置 | 已完成 `docs/DEPLOYMENT.md` §3 的环境搭建 |
| 一次性准备 | `./deploy/start.sh --with-demo`（迁移 + 灌数据 + 启动） |

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
> 渠道名称会一路出现在健康检查、每条证据的来源字段和演示报告的开头。
> 一个模拟读数被当成真实 GPU 数据来讲解，是这个项目最严重的信任事故。"

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

> "一次容器 OOM 会产生三条告警：OOM、重启、推理错误。我们最初的实现是
> **每条告警诊断一次**，结果写出三条内容几乎相同的结论 —— 诊断列表变成噪声。
>
> 现在按'资源可达闭包相交'把告警聚成**事故**，一次事故一条结论，
> 事故内所有告警都关联到它。这是运维真正的工作单位。"

```bash
curl -s 'localhost:8080/api/v1/alerts?state=firing' \
  | python3 -c "import json,sys; [print(a['ruleId'], a['severity'], '→', a['diagnosisId']) for a in json.load(sys.stdin)['data']['items']]"
```

**指出三条告警指向同一个 `diagnosisId`。**

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
# 静默一条告警，再注入一次，验证静默期内不再打扰
curl -s -X POST localhost:8080/api/v1/alerts/<id>/actions \
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
| ZWatch 接了吗？ | 未接，ZSvirt premium 模块是否在测试环境启用待命题方确认（外部阻塞 X-07）。**不按未确认的接口写适配器** —— 那只能靠猜 |
| 置信分怎么来的？ | 规则加权评分，非统计学概率；`confidenceBreakdown` 逐项可复算；最高分 < 0.30 强制返回 UNKNOWN |
| 为什么不每条告警都诊断？ | 一次事故会产生多条告警，逐条诊断会刷出重复结论。按"资源可达闭包相交"聚成事故后诊断一次 |
| 为什么有告警没有诊断？ | `warning` 级告警不自动诊断（避免灌一堆 UNKNOWN），用户可按需一键诊断。响应里会如实报告跳过原因与数量 |
| 数据是模拟的，结论还有意义吗？ | 有意义的是**关联与推理过程**：同一套规则在真实数据上给出同样的结论形态。模拟只替换了输入来源，`origin` 字段把它标注清楚 |
| 用了哪些中间件？ | 只有 Python + FastAPI + PostgreSQL。刻意不引入 Kafka / Redis / Prometheus 等（`ADR-0003`），全部运行依赖 13 个 |

---

## 复现性声明

演示数据可复现：`SimulatedProvider` 用**显式种子**的独立随机源，场景参数固定，
时间基准固定为 `2026-09-17T12:00:00Z`。因此同一台机器上重复运行得到同样的数字，
讲解词不会与界面对不上。

```bash
cd backend
./.venv/bin/python -m app.demo list                    # 列出场景
./.venv/bin/python -m app.demo demo --scenario container_oom
./.venv/bin/python -m app.demo reset                   # 清空（保留表结构）
```
