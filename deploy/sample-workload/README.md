# 样例容器化 AI 工作负载（赛题交付要求 d / e）

> 赛题依据：
>
> - **d**：在虚拟机中运行至少一种容器化 AI 工作负载（模型推理服务 / RAG / Agent / 批处理），
>   **提供完整配置**；
> - **e**：至少覆盖三类典型场景（容器/进程异常、GPU 或资源瓶颈、应用/Agent 任务异常），
>   每类需给出告警、关联证据、影响范围与处置建议。
>
> 本目录是 d 的完整配置，也是 e 里「容器/进程异常」那一类的**真实注入与采集**入口。

---

## 1. 交付物清单

| 文件 | 作用 |
|---|---|
| `Dockerfile.inference` | 推理服务镜像（CPU 可跑，模型构建期固化并校验 SHA256） |
| `bake_model.py` | 构建期下载 + 校验模型（失败即构建失败） |
| `serve.py` | 推理服务本体（ONNX Runtime + 真实预训练 DistilBERT） |
| `compose.yaml` | 两个 profile：`normal`（健康基线）/ `starved`（故障） |
| `loadgen.sh` | 负载生成器：`steady` / `burst` / `broken` |
| `collect_real_events.py` | **真实事件采集**：驱动成员 A 的探针采集器，把真实故障上报到 Crosslayer |

**模型是真的**：`distilbert-base-uncased-finetuned-sst-2-english`（SST-2 情感分类），
ONNX 动态量化版 67.5 MB，构建期经 SHA256 校验后固化进镜像。每个请求都跑一次完整
Transformer 前向 —— 不是 `sleep` 假装的负载。

---

## 2. 一次实测驱动的技术栈修正（值得一读）

本目录最早的版本写的是 `pip install vllm==0.8.5`。在没有 GPU 的机器上**跑不起来**：

| 渠道 | 实测可用性 |
|---|---|
| `vllm`（PyPI） | ❌ 只有 CUDA wheel（`vllm-0.8.5-cp38-abi3-manylinux1_x86_64.whl`，311 MB，依赖整套 `nvidia-*`）；无 GPU 时 `import vllm` 失败 |
| `torch`（PyPI） | ⚠️ CUDA 构建，约 766 MB + 数 GB `nvidia-*` 依赖 |
| `torch`（`download.pytorch.org/whl/cpu`） | ❌ 索引页可达，但 wheel 实际落在 `download-r2.pytorch.org` —— **实测容器内网络不可达** |
| **`onnxruntime` + 预训练 ONNX** | ✅ 约 15 MB、纯 CPU、PyPI 可达（本目录现用） |

赛题明文要求「缺特定 GPU 硬件时也能复现实验流程」，所以默认路径必须**确定可跑**。
ONNX Runtime 不是降级：它是**同一个预训练模型**的另一个推理后端。
有 GPU 的 VM 上换回 vLLM 只需改 `serve.py` 与依赖，对外观测接口不变。

---

## 3. 快速开始

### 3.1 构建

```bash
# 默认从官方 HuggingFace 取模型
docker compose build

# 受限网络（huggingface.co 不可达）用可信镜像
HF_ENDPOINT=https://hf-mirror.com docker compose build
```

> **若 `docker.io` 不可达**（企业网/校园网常见），先经可达镜像源取基础镜像再打标签：
>
> ```bash
> docker pull docker.m.daocloud.io/library/python:3.12-slim
> docker tag  docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
> ```
>
> `Dockerfile.inference` 里仍然写 `FROM python:3.12-slim` —— **不把某地的绕行方案
> 固化进交付物**，否则换一个评测环境反而构建不起来。

### 3.2 健康基线（先给评委看"正常长什么样"）

```bash
docker compose --profile normal up -d
curl -s localhost:8000/health
curl -s -X POST localhost:8000/v1/infer \
     -H 'content-type: application/json' -d '{"text":"The diagnosis worked perfectly."}'
```

实测输出：

```json
{"label":"POSITIVE","score":0.999021,"tokens":14,"elapsed_seconds":0.0121}
```

再看负载是否真的在产生信号：

```bash
TARGET=http://localhost:8000 ./loadgen.sh steady     # 稳态
TARGET=http://localhost:8000 ./loadgen.sh burst      # 高并发
TARGET=http://127.0.0.1:9   ./loadgen.sh broken      # 可达性故障
```

### 3.3 故障注入：把限额压到需求之下

```bash
docker compose --profile normal  down
docker compose --profile starved up -d --force-recreate
```

`starved` 的旋钮只有一个：`KV_CACHE_MB=1200` 而 `mem_limit=512m`，
且 `memswap_limit == mem_limit`（**禁用 swap** —— 否则超限部分被换出去，进程活着但极慢，
观测到的是"性能问题"而不是"OOM"，那是另一类故障，混在一起因果就说不清了）。

**实测**：

```
Status=restarting  OOMKilled=true  ExitCode=137  RestartCount=7
MemLimit=536870912  MemSwap=536870912
```

事件流（`docker events`）：

```
oom  demo-inference-starved
die  demo-inference-starved  exitCode=137
oom  demo-inference-starved
die  demo-inference-starved  exitCode=137
…（反复重启，这就是「容器/进程异常」场景的第二个信号）
```

### 3.4 把真实事件送进 Crosslayer

```bash
# 终端 1：后端
./deploy/start.sh

# 终端 2：采集器（每 5 秒采一次；无事件时每 30 秒发一次资源心跳）
python3 deploy/sample-workload/collect_real_events.py --watch 5
```

然后按 3.3 注入故障。**实测结果**（真实数据，不是模拟）：

```
[13:39:31] 采到 1 条事件、2 个资源：
          container.oom_killed       critical  容器 demo-inference-starved 被 OOM Killer 终止
          └─ 指标 {"exitCode": 137, "container_memory_limit_bytes": 536870912, "oomKilledCount": 8}
```

Crosslayer 侧的结论：

| 项 | 结果 |
|---|---|
| 告警 | `R-CTR-OOM-010` **firing**，`count=9` |
| 根因 | **`CONTAINER_MEMORY_LIMIT`**，置信 **0.9** |
| 证据来源 | `probe`（**真实采集**，不是 `simulated`） |
| 建议 | `INCREASE_CONTAINER_MEMORY` |

这与模拟演示路径给出的根因与置信度**一致** —— 区别只在于证据是真实观测的。

---

## 4. 与成员 A 的探针是什么关系（重要）

`collect_real_events.py` **复用成员 A 的采集逻辑，一行没改**：
`agents/probe/collector/container.py::ContainerCollector` 负责发现与映射，
`agents/probe/reporter.py::build_batch` / `Reporter` 负责组装与上报。

它只做三件事，**每一件都对应 A 的采集器的一个缺陷**：

| 本脚本的动作 | 对应的缺陷 | 为什么先放在这里 |
|---|---|---|
| `NativeDockerClient.events()` 自己发请求 + 逐行解析 NDJSON | A 的 `events()` 有两个独立缺陷：`filters` 未 URL 编码（抛 `InvalidURL` 被吞）、`_get` 用 `json.loads` 解析 NDJSON 流（抛 `JSONDecodeError` 被吞） | 不修就一条事件都收不到 |
| `repair_event_targets()` 用 `Actor.ID[:12]` 补回资源指向 | A 读的是 `ev["id"]`，而 Engine API 的容器 ID 在 `Actor.ID`，于是每条事件的资源指向都是空串 | 空串事件关联不到任何资源 |
| `inspect_containers()` 补齐 `restartCount` 等指标 | A 从 `/containers/json` 读 `RestartCount`，而该响应没有这个字段 → **恒为 0** | "重启 14 次"显示成"从未重启"是最容易被当成没问题的那种错值 |

**完整复现步骤、四条缺陷的定位过程与建议修法见
[`docs/PROBE_DEFECT_SYNC.md`](../../docs/PROBE_DEFECT_SYNC.md)。**
修好之后，本脚本里这三段都可以删掉 —— **它们的存在本身就是待办事项**。

> 附：本脚本的采集通道会**优先走 `/var/run/docker.sock`**（即 A 的原始代码路径）；
> 只有在 socket 不存在时才退回 `docker` 命令行的替身实现。
> 真实部署在 ZSvirt 虚拟机内，走的是 socket 那条路。

---

## 5. 诚实边界

| 事实 | 说明 |
|---|---|
| 容器、OOM、退出码、重启次数 | **真实观测**，来自容器运行时 API |
| 证据 `source` | `probe` —— **真实采集**，不是 `simulated` |
| 宿主 VM 资源 | 由采集脚本补一条 `vm` 资源代表"容器实际所在的 Linux VM"。**它不是 ZSvirt 管理的虚拟机**：资源 `name` 里写明，`vmId` 用 `devbox00-...` 这种一眼可辨的替身串，**不伪造 ZSvirt UUID** |
| 跨层链的上半段 | 缺失。真实部署里 `vGPU → VM → 容器` 由 ZSvirt 资产 API + 探针 `vmId` 共同锚定，而 `vmId` 的注入方式仍在等命题方答复（外部阻塞 **X-09**） |
| GPU 场景 | 本机与自有 VM 都没有 NVIDIA 设备，GPU 显存那一类**仍是模拟**，不能声称是真跑 |

---

## 6. 有 GPU 的 VM 上怎么换成 vLLM

本目录的样例刻意选了"任何 CPU 都能跑"的后端来保证可复现。真实环境（GPU 直通 VM）上：

1. 把 `serve.py` 换成 vLLM 的 OpenAI 兼容服务
   （`vllm/vllm-openai:v0.8.5`，`--model Qwen/Qwen2.5-0.5B-Instruct`）；
2. `loadgen.sh` 的 `TARGET` 与请求体改回 `/v1/completions`；
3. `KV_CACHE_MB` 的显式旋钮可以去掉 —— vLLM 自己有
   `--gpu-memory-utilization`，那本来就是同一个概念的官方名字。

**观测侧不用改**：Crosslayer 只关心事件与指标，不关心推理后端是谁。
