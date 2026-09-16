# deploy/ — 部署与运行脚本

本目录存放**部署脚本、配置样例与故障注入脚本**（不含任何密钥）。

## 已定义内容

| 文档 | 内容 |
|---|---|
| [../docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md) | 环境要求、搭建步骤、配置项、端口 |
| [../docs/TEST_PLAN.md](../docs/TEST_PLAN.md) | 三类故障场景的注入与验证要求 |

## 规划

```text
deploy/
├── .env.example         # 配置样例（真实 .env 已被 .gitignore 排除）
├── fault-injection/     # 三类故障场景的注入脚本
└── compose.yml          # 【待确认】是否需要容器化
```

## 安全红线

- **禁止**提交任何 token、密码、密钥、证书或局域网地址。
- 凭据一律通过环境变量注入，仓库内只保留 `.env.example`。
- 故障注入脚本必须**可逆**：每个注入动作都要有对应的恢复步骤（赛题"可复现"要求）。

> **状态：待补充。** 三个场景的注入脚本归属需三方确认（见
> [../docs/backend/DIAGNOSIS_DESIGN.md](../docs/backend/DIAGNOSIS_DESIGN.md) §5.1 S4）。
