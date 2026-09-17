# 架构决策记录（ADR）

本目录保存**已做出的重要架构决策**及其背景、备选方案与后果。

ADR 的价值不在于纪录"我们选了什么"，而在于纪录**"当时为什么这么选、排除了什么"**——避免后人重复讨论已被否决的方案。

---

## 编号约定

```
ADR/NNNN-短横线标题.md
例：ADR/0001-resource-id-scheme.md
```

编号单调递增，**不复用、不删除**。若决策被推翻，不修改原文，而是新增一条 ADR 并在原文顶部标注 `Superseded by ADR-NNNN`。

## 状态取值

| 状态 | 含义 |
|---|---|
| `Proposed` | 已提出，等待团队确认 |
| `Accepted` | 已确认，具有约束力 |
| `Rejected` | 已否决（保留记录，避免重复提议） |
| `Superseded by ADR-NNNN` | 已被新决策取代 |
| `Deprecated` | 不再适用，但未被取代 |

## 模板

见 [`template.md`](template.md)。

## 决策权威等级

ADR 属于第 3 优先级（见团队控制协议 §6）：

```
1. 成员已确认的全项目架构/设计结论
2. API_CONTRACT / DATA_MODEL / TECH-BASELINE
3. 模块设计文档与 ADR          ← 本目录
4. 现有测试与代码
5. 团队职责/流程文档
6. AI 自己的推测
```

**冲突时以高优先级为准，并要求停止并报告，而不是自行"顺手改文档"。**

---

## 当前 ADR 索引

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](0001-python-fastapi-postgres-baseline.md) | 后端技术基线选定 Python + FastAPI + PostgreSQL | **Accepted**（成员已确认） |
| [0002](0002-resource-id-scheme.md) | 资源 ID 采用 `{kind}:{source}:{sourceId}` 方案 | **Accepted**（2026-09-17 转正：A 给出 `sourceId` 规则，C 确认不解析 ID） |
| [0003](0003-minimal-runtime-dependencies.md) | 运行时依赖最小化：不引入消息队列与搜索引擎 | **Accepted**（2026-09-17 扩展至 Prometheus / Grafana / OTel / Jaeger） |

> **2026-09-17 状态变化说明**：ADR-0002 由 `Proposed` 转 `Accepted` —— 其唯一阻塞点
> （探针侧 `sourceId` 生成规则）已由成员 A 在 `agents/docs/REPORTING_CONTRACT.md` §3 解决。
> ADR-0003 的适用范围同日扩大：成员决策**不引入**赛题"可使用"的 Prometheus / Grafana /
> OpenTelemetry / Jaeger，理由是「稳定性与可复现性」为评分项（15%），自建栈的可复现性更优。
