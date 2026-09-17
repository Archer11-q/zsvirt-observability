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
| `app/diagnosis/` | ✅ **诊断引擎已实现**：五步流程、规则求值、置信评分、影响范围传播 |
| `app/api/health.py` | ✅ `GET /api/health`，**真实探测上游**（数据库 / ZSvirt / GPU Provider） |
| `app/api/router.py` | 🔶 v1 业务路由已占位，端点待实现 |
| `app/ingest/` | ⬜ 数据接入（等成员 A 的实现） |
| `app/zsvirt/` | ⬜ ZSvirt 适配层（等命题方提供 API 文档） |
| `app/normalize/` | ⬜ 标准化与脱敏 |
| `app/graph/` | ⬜ 资源图 |
| `app/events/` | ⬜ 事件存储 |
| `app/alerts/` | ⬜ 告警引擎 |

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
