# deploy/ — 部署、注入与演示材料

**负责人：三方共同** ｜ **产品名：Crosslayer**

| 文件 | 用途 |
|---|---|
| [`start.sh`](start.sh) | **一键启动**：检查环境 → 迁移数据库 → （可选）灌入演示场景 → 启动 API |
| [`inject_fault.sh`](inject_fault.sh) | **故障注入**：通过真实 HTTP 接口把赛题场景注入到运行中的服务 |
| [`../docs/DEMO_SCRIPT.md`](../docs/DEMO_SCRIPT.md) | **演示脚本**：3–5 分钟的讲解顺序、要点与常见提问 |
| [`reconcile_frontend.py`](reconcile_frontend.py) | **前后端对账**：对运行中的服务实测响应，与前端声明的类型逐字段比对 |
| [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md) | 环境搭建、配置项、部署步骤 |

---

## 快速开始

```bash
# 一次性准备：迁移 + 灌入三类故障场景 + 启动服务
./deploy/start.sh --with-demo

# 只想看报告、不启动服务
./deploy/start.sh --replay

# 服务已在运行时，注入单个场景
./deploy/inject_fault.sh container_oom
./deploy/inject_fault.sh all
```

`start.sh` 会在任一环节失败时**立刻退出并说明原因**（缺 `.env`、数据库不可达、
迁移失败）。演示现场最糟的情况是"看起来启动了但其实是坏的"。

---

## 演示数据是模拟的，而且这一点必须说

演示机通常没有 GPU，因此 GPU 指标来自 `app/zsvirt` 的 `SimulatedProvider`。
赛题要求支持降级/模拟模式，`docs/DATA_MODEL.md` §4.2.1 的诚实性要求是
**模拟数据必须可识别**。我们的做法是让它结构上无法混淆：

| 位置 | 表现 |
|---|---|
| `GET /api/health` | `components.gpuProvider.mode == "simulated"` |
| 每条 GPU 读数 | `origin == "simulated"`（必填字段，不是可选标签） |
| 演示报告开头 | 明确声明数据来源 |
| `inject_fault.sh` / `start.sh` | 运行时打印声明 |

> 一条模拟读数被当成真实 GPU 数据来讲解，是这个项目最严重的信任事故 ——
> 比任何功能缺失都糟。

---

## 可复现性

演示数据在**同一台机器上重复运行得到同样的数字**：

- GPU 指标由 `SimulatedProvider(profile, seed)` 产生，种子是常量、随机源是
  独立的 `random.Random` 实例（不受同进程其他代码影响）；
- 场景定义与时间基准固定（`2026-09-17T12:00:00Z`）；
- 场景的 `batchId` 固定，重复灌入走**幂等回放**，不会重复写事件。

```bash
cd backend
./.venv/bin/python -m app.demo list         # 列出可用场景
./.venv/bin/python -m app.demo demo --scenario container_oom
./.venv/bin/python -m app.demo show --json  # 机器可读的报告
./.venv/bin/python -m app.demo reset        # 清空数据（保留表结构）
```

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `BASE_URL` | `http://127.0.0.1:8080` | `inject_fault.sh` 的目标服务 |
| `TOKEN` | 空 | 服务启用鉴权时填 `API_AUTH_TOKEN` |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8080` | `start.sh` 的监听地址 |

---

## 尚未包含的内容（诚实说明）

| 项 | 状态 |
|---|---|
| 容器化部署（Dockerfile / compose） | 未做。开发环境所在发行版没有 Docker，无法验证的配置不会写进仓库 |
| 真实 GPU 上的注入脚本 | 未做。需要命题方提供测试环境（外部阻塞 X-02 / X-05） |
| ZSvirt 平台侧的资源同步 | 未接。`zwatch` 是否在测试环境启用待确认（X-07） |
