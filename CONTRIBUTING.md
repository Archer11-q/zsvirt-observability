# 协作约定

本项目由三位成员并行开发，通过统一的目录边界与接口契约协作。

---

## 1. 仓库结构

```text
├── backend/    后端平台、资源关联、告警、根因诊断   （成员 B）
├── agents/     ZSvirt 虚拟机内探针与数据采集        （成员 A）
├── frontend/   可视化展示                          （成员 C）
├── deploy/     部署脚本、配置样例、故障注入脚本      （三方共同）
└── docs/       项目真源文档（架构 / 数据模型 / API 契约 / 兼容性 / 部署 / 测试）
```

**只修改自己负责的目录。** 需要改别人的模块时，先沟通，不要直接动。

---

## 2. 分支模型

```text
main                 # 发布 / 稳定版本，只读为主
develop              # 团队集成分支（默认分支）
feature/member-a/*   # 成员 A
feature/member-b/*   # 成员 B
feature/member-c/*   # 成员 C
```

| 规则 | 说明 |
|---|---|
| ✅ 允许 | 在 `feature/member-x/*` 上开发，完成后开 PR 合入 `develop` |
| ❌ 禁止 | 直接在 `main` 或 `develop` 上开发 |
| ❌ 禁止 | `git push --force`（任何分支） |
| ❌ 禁止 | 删除或覆盖他人的提交 |
| ❌ 禁止 | 绕过 PR 直接推 `develop` / `main`（初始骨架除外） |

`main` 只接受经过联调验收的稳定版本。

---

## 3. 首次克隆

```bash
git clone git@github.com:Archer11-q/zsvirt-observability.git
cd zsvirt-observability
git checkout develop          # 克隆后若不在 develop 上

# 建立自己的工作分支
git checkout -b feature/member-a/probe-bootstrap   # 成员 A 示例
```

---

## 4. 提交与 PR

- 提交信息用英文或中文均可，但要说清**做了什么、为什么**。
- 涉及契约变更的提交，必须在信息中标注 `BREAKING` 或 `NON-BREAKING`。
- PR 请说明：改了什么、影响了谁、如何验证、如何回滚。

---

## 5. 接口契约纪律（最重要）

`docs/API_CONTRACT.md` 与 `docs/DATA_MODEL.md` 是三方之间的契约。

**禁止只改代码不改契约。** 任何字段的新增、改名、删除或类型改变，必须：

1. 先修改契约文档；
2. 在提交中标注 `BREAKING` / `NON-BREAKING`；
3. 通知受影响的成员；
4. 同步更新测试与示例响应。

### 各自的契约位置

| 角色 | 需要关注 |
|---|---|
| 成员 A | `docs/API_CONTRACT.md` §3（A → B 上报契约）、§3.3 的 Q1–Q7 |
| 成员 B | §3 与 §4 全部，以及 §5（ZSvirt 适配待确认项） |
| 成员 C | `docs/API_CONTRACT.md` §4（B → C 查询契约）、§4.7 的 Q8–Q14 |

---

## 6. 文档权威等级

冲突时以高优先级为准，**并要求停止并报告，而不是自行"顺手改文档"**。

```text
1. 成员已确认的全项目架构/设计结论
2. docs/API_CONTRACT.md、docs/DATA_MODEL.md、docs/TECH-BASELINE.md
3. 模块设计文档与 docs/ADR/
4. 现有测试与代码
5. 团队职责 / 流程文档
6. 个人推测
```

---

## 7. 技术基线

见 [`docs/TECH-BASELINE.md`](docs/TECH-BASELINE.md)。当前已确认：

| 项 | 结论 |
|---|---|
| 后端语言 | Python **3.12.x**（注意：开发机默认可能是其他版本，项目环境必须显式用 3.12） |
| 后端框架 | FastAPI |
| 数据库 | PostgreSQL + SQLAlchemy |
| 运行时依赖 | **最小化**：不引入消息队列、搜索引擎、分布式追踪后端 |
| 前端 | 由成员 C 决定（尚未确认） |
| 探针 | 由成员 A 决定，**可能使用 C / C++**（尚未确认） |

**未经确认不得升级 / 降级核心语言、框架、数据库或基础镜像**；不得引入 Kafka、Elasticsearch、Jaeger、OTel Collector、eBPF 等重大组件。

---

## 8. 仓库清洁红线

**禁止提交：**

- 任何 token、密码、密钥、证书、真实凭据
- 局域网地址、个人机器私有配置、绝对路径
- 内部材料：职责文档、AI 提示词与对话记录、内部评审记录、比赛评分讨论、个人分工材料
- 未采用的架构方案与临时实验（除非整理成正式 ADR）
- 代码图谱缓存、AI 索引等辅助产物

**约定：**

- 内部材料放 `.project-internal/`，该目录已被 `.gitignore` 排除。
- 凭据一律通过环境变量注入；仓库内只保留 `.env.example`。
- 提交前用 `git status` 与 `git diff --cached` 自查一遍。

> `.gitignore` 同时覆盖 Python / C-C++ / Node 三种工具链，这是刻意设计：仓库是多语言 monorepo。
> 不要因为"某块看着用不到"而删除它。

---

## 9. 环境注意事项（成员 B 开发机勘测结果，供参考）

| 项 | 状态 |
|---|---|
| Docker | 该 WSL 发行版未启用 Docker Desktop 集成 |
| GPU | `/dev/nvidia*` 不存在，开发机看不到 GPU |
| eBPF | `unprivileged_bpf_disabled=2`，`sudo` 需交互密码 |
| PostgreSQL | 未安装 |

这些是**开发机**的限制，不是项目架构限制。VM 内采集与远程 ZSvirt 集群不受影响。
