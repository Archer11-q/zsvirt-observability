# 样例容器化 AI 工作负载（赛题交付要求 d / e）

| 字段 | 值 |
|---|---|
| 对应交付要求 | **d**（在 VM 中运行至少一种容器化 AI 负载，提供完整配置）<br>**e**（三类故障场景与证据，每类给出告警 / 关联证据 / 影响范围 / 处置建议） |
| 归属 | 全项目（负载配置由 B 维护；探针归 A；界面归 C） |
| 状态 | 配置**已完成**；**在 ZSvirt 虚拟机内实跑与录制仍缺**（见 `docs/REQUIREMENTS_GAP.md`） |

## 1. 为什么选「模型推理服务」

赛题列出四种可选形态（模型推理服务 / RAG 应用 / Agent 服务 / 批处理任务）。选推理服务，
是因为它天然产生本平台要观测的**三层信号**：

| 层 | 信号 | 对应赛题场景 |
|---|---|---|
| 容器层 | 内存限额、OOM、重启 | 容器/进程异常 |
| 进程层 | worker 的 I/O 等待、崩溃 | 容器/进程异常 |
| 服务层 | 推理错误、超时、并发与 QPS | 应用/Agent 任务异常 |

而这三层正是赛题三类故障场景的载体 —— 一个负载就能覆盖全部三类。

## 2. 文件说明

| 文件 | 用途 |
|---|---|
`Dockerfile.inference` | 镜像定义。默认 **CPU 可跑的极小模型**（0.5B），GPU 是可选叠加 |
`compose.yaml` | 两个 profile：`normal`（健康基线）与 `starved`（限额压小 = 故障注入） |
`loadgen.sh` | 负载生成器。**没有它就没有 `inference.*` 信号**，见 §4 |

## 3. 快速开始

```bash
cd deploy/sample-workload

# ① 健康基线（先让评委看到"正常长什么样"）
docker compose --profile normal up -d
./loadgen.sh steady                  # 每 2 秒一次请求

# ② 故障注入：切换成受限限额的同一负载
docker compose --profile normal down
docker compose --profile starved up -d
./loadgen.sh burst                   # 高并发压出瓶颈

# ③ 观察平台侧
curl -s localhost:8080/api/health            | python3 -m json.tool | head
curl -s localhost:8080/api/v1/alerts         | python3 -m json.tool | head -40
curl -s localhost:8080/api/v1/diagnoses      | python3 -m json.tool | head -40
```

**有 GPU 的机器**：在 `docker run`/compose 里加 `--gpus all`，并把 `MODEL_NAME`
换成更大的模型（例如 `Qwen/Qwen2.5-7B-Instruct`）。

## 4. 负载生成器为什么是必需件

一个「跑着但没人调用」的推理服务几乎不产生信号：没有请求就没有
`inference.timeout` / `inference.error`，也没有可对比的延迟趋势。而赛题三类场景里
「应用/Agent 任务异常」正是靠这些信号表达的。

三种模式对应三种演示意图：

| 模式 | 作用 | 产生的信号 |
|---|---|---|
| `steady` | 建立**正常基线** | 正常延迟与成功率（没有基线就无法证明后面是"故障"） |
| `burst` | 压出**资源瓶颈** | 延迟上升、并发排队、可能触发限额 |
| `broken` | 制造**可达性故障** | `container.network.unreachable` / `inference.timeout` |

## 5. 故障注入方式：**限额就是旋钮**

三类场景里有两类（容器异常、资源瓶颈）**不需要任何注入工具**，只需把限额压到
负载真实需求之下：

| 场景 | 注入 | 预期信号 | 预期根因 |
|---|---|---|---|
| 容器/进程异常 | `mem_limit: 512m`（模型需 ~1.2g） | `container.oom_killed`（exitCode 137）+ `container.restart` | `CONTAINER_MEMORY_LIMIT` |
| GPU 或资源瓶颈 | `cpus: 0.5` + `loadgen.sh burst` | 延迟上升、`gpu.utilization.high` 或进程 I/O 等待 | `GPU_MEMORY_EXHAUSTED` / `GPU_NEIGHBOR_CONTENTION` |
| 应用/Agent 异常 | `loadgen.sh broken`（打向不通端口） | `container.network.unreachable` / `inference.timeout` | `NETWORK_UNREACHABLE` |

**为什么这样设计**：注入方式不依赖外部工具，因此**可复现** ——
同一份 `compose.yaml` 在任何装有 Docker 的机器上产生同一类故障。
这也是赛题「稳定性与可复现性 15%」要的东西。

## 6. 无 GPU 时的诚实性

演示机通常没有 GPU。默认配置**就是** CPU 极小模型，而不是"假装有 GPU"：

- 平台在 `GET /api/health` 的 `gpuProvider.mode` 里如实报当前渠道；
- 每条证据带 `origin` 字段（`zsvirt-zwatch` / `probe` / `simulated`）；
- `docs/DEMO_SCRIPT.md` 开场即声明这一点。

赛题明文要求「支持模拟数据或最小化降级模式，以便在缺少特定 GPU 硬件时复现实验流程」——
本配置让这句话成为**默认路径**，而不是一句声明。

## 7. 仍未完成的部分

| 项 | 状态 | 卡在哪 |
|---|---|---|
| 在 **ZSvirt 虚拟机**内实跑 | ⬜ | 需要一台 ZSvirt 虚拟机（宿主机上尚未创建，外部阻塞 X-13） |
| 真实 GPU 直通下的证据链 | ⬜ | 同上 + X-09（探针如何获知 VM 标识） |
| 录制演示视频 | ⬜ | 同上（见 `docs/REQUIREMENTS_GAP.md` §3） |

**但**：本配置在**任意装了 Docker 的机器**（含你本机 VMware 里的 Ubuntu）都能跑，
并产生完整的三类场景信号 —— 只是 `origin` 会显示为 `probe`/`simulated` 而非
`zsvirt-zwatch`。这一点必须如实标注，不得声称是 ZSvirt 环境实测。
