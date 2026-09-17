# backend/ — 后端平台、资源关联、告警与根因诊断

**负责人：成员 B** ｜ **产品名：Crosslayer**

本目录是后端服务的实现位置。

| 文档 | 内容 |
|---|---|
| [../docs/backend/BACKEND_DESIGN.md](../docs/backend/BACKEND_DESIGN.md) | 模块划分、依赖方向、错误处理 |
| [../docs/backend/DIAGNOSIS_DESIGN.md](../docs/backend/DIAGNOSIS_DESIGN.md) | 诊断引擎、规则集、三类故障场景 |
| [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) | 全项目分层与数据流 |
| [../docs/API_CONTRACT.md](../docs/API_CONTRACT.md) | 对外契约（A→B 上报 / B→C 查询） |
| [../docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md) | 环境搭建与配置项 |
| [../docs/TECH-BASELINE.md](../docs/TECH-BASELINE.md) | 语言与依赖基线 |

---

## 当前实现进度

| 模块 | 状态 |
|---|---|
| `app/config.py` | ✅ 配置加载（环境变量 / `.env`，含 GPU Provider 与接入限制） |
| `app/models.py` + `alembic/` | ✅ 6 张表 + 迁移（在全新库上验证 `upgrade` / `downgrade`） |
| `app/enums.py` | ✅ 全部冻结码表的**唯一真源**（含中文文案），带一致性自检 |
| `app/graph/` | ✅ 资源图：纯函数遍历（`algorithms.py`）+ 持久化 + 拓扑裁剪 |
| `app/normalize/` | ✅ 资源 ID 拼装 + 脱敏管道（与探针共享 `shared/sensitive_vectors.json`） |
| `app/events/` | ✅ 事件存储（只追加）+ keyset 游标分页 |
| `app/ingest/` | ✅ `POST /api/v1/ingest/batch`（A 的 Q1–Q7 全部落地：幂等、限流、时钟漂移） |
| `app/alerts/` | 🔶 状态机 / 静默 / 聚合计数 / 批量操作完成；**规则求值未做**（见 `../docs/DEVELOPMENT_PLAN.md` 任务 2） |
| `app/diagnosis/` | ✅ 引擎（五步流程、规则求值、置信评分）+ **服务层（装配上下文 → 落库）** |
| `app/api/` | ✅ **16 个契约端点全部落地**（见下） |
| `app/zsvirt/` | ⬜ ZSvirt 适配层（等命题方提供 API 文档，见 `../docs/DECISIONS.md` §8） |

**已实现端点**（`../docs/API_CONTRACT.md` §4.1 清单，由 OpenAPI schema 核对）：

```
GET  /api/health                            健康与降级（无版本前缀）
POST /api/v1/ingest/batch                   接收 A 的探针上报
GET  /api/v1/topology                       资源拓扑（可裁剪，truncated 标志）
GET  /api/v1/dict                           枚举字典（含中文文案，ETag）
GET  /api/v1/events                         事件查询（时间窗 / 资源 / 类型 / 严重级别）
GET  /api/v1/events/{id}                    事件详情
GET  /api/v1/events/{id}/related-alerts     反向链路：事件 → 采纳它的告警
GET  /api/v1/alerts                         告警列表 + 状态 / 严重级别计数
GET  /api/v1/alerts/{id}                    告警详情
GET  /api/v1/alerts/{id}/evidence           展开证据事件（含缺失报告）
POST /api/v1/alerts/{id}/actions            单条 ack / resolve / silence / unsilence
POST /api/v1/alerts/actions                 批量状态操作（逐条报告）
GET  /api/v1/workloads                      AI 服务负载总览（`ai_service` 聚合视图）
GET  /api/v1/diagnoses                      诊断列表（游标分页 + 根因计数）
GET  /api/v1/diagnosis/{id}                 诊断详情（`?includeEvidence=true` 内联证据事件）
POST /api/v1/diagnoses                      手动触发诊断（`{alertId}` 或 `{anchorResourceId, window}`）
```

测试规模：**406 通过 / 1 跳过**（真实 PostgreSQL 测试库上运行），`ruff check` 全绿。

---

## 本地开发

```bash
# 环境准备见 ../docs/DEPLOYMENT.md §3（Python 3.12 为源码编译，非系统包）

cd backend
~/.local/python/3.12.11/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # 填入数据库密码；.env 已被 .gitignore 排除

python -m pytest -q
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

验证：`curl -s localhost:8080/api/health`

> **无 GPU 环境的降级方式**（赛题要求）：
> `export GPU_PROVIDER=simulated SIMULATED_DATA_ENABLED=true`

---

## 依赖约束

- 运行时依赖**精确锁定版本**，见 [`requirements.txt`](requirements.txt)。
- 例外：`ruff` 使用范围约束 `>=0.16.0,<0.17`，因为 aliyun PyPI 镜像版本滞后
  （镜像 0.16.7 / 上游 0.16.8），精确 pin 会导致安装失败。ruff 是纯开发工具，不影响运行时。
- 引入任何新依赖前须走 [`../docs/TECH-BASELINE.md`](../docs/TECH-BASELINE.md) §5 的流程。

---

## 诊断引擎的三条边界约束

设计见 [`../docs/backend/DIAGNOSIS_DESIGN.md`](../docs/backend/DIAGNOSIS_DESIGN.md)：

1. **不访问数据库、不发 HTTP** —— 只接收装配好的 `DiagnosisContext` 纯数据对象，
   因此可以脱离一切基础设施做单元测试。
2. **规则是声明式数据**，不是代码分支 —— 新故障场景 = 新增规则数据，不新增代码路径。
3. **资源图只读传入**，引擎无副作用。

诚实性红线（已由测试覆盖）：

- 无证据 → 返回 `UNKNOWN`，**不编造根因**；
- 最高分 `< 0.30` → 强制 `UNKNOWN`；
- `confidence` 必须可由 `confidenceBreakdown` 复算；
- 模拟数据来源必须在 `evidence.source` 中标记，**不得伪装成真实采集**。
