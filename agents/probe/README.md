# probe/ — VM 内探针（成员 A）

面向 ZSvirt 虚拟机内 AI 工作负载的采集与上报组件。

**零第三方依赖**：只用 Python 3.12 标准库，无需 `pip install`，复制脚本即可运行。

## 运行

```bash
# 1. 设置必填配置（环境变量优先）
export ZSVIRT_OBS_BACKEND_URL=http://<B-host>:8080
export ZSVIRT_OBS_VM_ID=<ZSvirt VM UUID>

# 2. 运行
python3.12 -m probe
```

可选配置见 `config.example.json`（复制为 `config.json` 即可用文件配置）。凭据 `ZSVIRT_OBS_PROBE_TOKEN` 只从环境变量注入，不写入文件。

## 配置项

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `ZSVIRT_OBS_BACKEND_URL` | 后端地址 | `http://localhost:8080` |
| `ZSVIRT_OBS_VM_ID` | ZSvirt VM 全局 ID，**必填**，形态 `vm:zsvirt:<uuid>`（Push 挂 Pull 的唯一锚点） | — |
| `ZSVIRT_OBS_PROBE_TOKEN` | 可选 Bearer token，默认关闭 | — |
| `ZSVIRT_OBS_AGENT_ID` | 探针实例 ID | `probe-<vm-uuid 前 8 位>` |
| `ZSVIRT_OBS_BATCH_SEC` | 批次间隔（秒） | `5` |
| `ZSVIRT_OBS_BATCH_MAX_EVENTS` | 单批事件上限 | `1000` |
| `ZSVIRT_OBS_BATCH_MAX_RESOURCES` | 单批资源上限 | `500` |
| `ZSVIRT_OBS_BUFFER_PATH` | 断网缓冲文件 | `probe_buffer.db` |
| `ZSVIRT_OBS_BUFFER_MAX_MB` | 缓冲上限（MB） | `100` |

优先级：**环境变量 > config.json > 默认值**。

## 目录结构

```
probe/
├── __main__.py        # 入口 + 主循环
├── config.py          # 配置加载
├── model.py           # Resource / Event 数据类
├── idgen.py           # ULID（batchId）+ sourceId 规则
├── sensitive.py       # 前置脱敏过滤
├── buffer.py          # SQLite 断网缓冲（FIFO）
├── reporter.py        # HTTP 批量上报
└── collector/
    ├── procutil.py    # /proc 共享读取（cgroup 解析容器 ID / stat / cmdline）
    ├── process.py     # 进程采集（crash + io_wait 窗口化 + parent 挂容器）
    ├── container.py   # 容器采集（Docker socket）
    ├── ai_service.py  # AI 服务识别（parent 挂容器）
    ├── network.py     # 网络可达性探测（container.network.unreachable）
    └── gpu.py         # GPU 采集（预留，默认不启用）
tests/
├── test_sensitive_vectors.py  # 脱敏一致性校验（零依赖，对齐共享向量）
└── test_collectors.py         # 采集器单测（cgroup 解析 / io_wait / 网络探测）
```

## 测试

```bash
python3.12 agents/probe/tests/test_sensitive_vectors.py   # 脱敏与 B 侧逐条一致
python3.12 agents/probe/tests/test_collectors.py          # 采集器逻辑
```

零第三方依赖（只用标准库 `json` + `unittest` + `mock`）。

## 契约对应

- 上报契约：`docs/API_CONTRACT.md` §3（Q1–Q7 已在 `DECISIONS.md` 冻结）
- 技术方案：`agents/docs/PROBE_DESIGN.md`
- 契约冻结签字：`agents/docs/FREEZE_ACK.md`
