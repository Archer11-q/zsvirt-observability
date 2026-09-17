#!/usr/bin/env bash
#
# Crosslayer 一键启动（可复现）
#
# 三件事：检查环境 → 迁移数据库 → 灌入演示场景并启动服务。
# 任何一步失败都**立刻退出并说明原因**，不带着半成品状态继续 ——
# 演示现场最糟的情况是"看起来启动了但其实是坏的"。
#
# 用法：
#   ./deploy/start.sh                 # 迁移 + 启动服务（不灌数据）
#   ./deploy/start.sh --with-demo     # 迁移 + 灌入三类场景 + 启动服务
#   ./deploy/start.sh --replay        # 只灌数据并打印报告，不启动服务
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="${REPO_ROOT}/backend"
VENV_PY="${BACKEND}/.venv/bin/python"

API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8080}"

WITH_DEMO=0
REPLAY_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --with-demo) WITH_DEMO=1 ;;
    --replay) REPLAY_ONLY=1; WITH_DEMO=1 ;;
    -h|--help)
      sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "未知参数：$arg（-h 查看用法）" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 1. 依赖检查

say "1/4 检查运行环境"

[[ -x "${VENV_PY}" ]] || fail "找不到虚拟环境解释器：${VENV_PY}
  环境搭建步骤见 docs/DEPLOYMENT.md §3（Python 3.12 为源码编译，不是系统包）"

if [[ ! -f "${BACKEND}/.env" ]]; then
  fail "缺少 ${BACKEND}/.env
  请先执行：cp backend/.env.example backend/.env 并填入数据库密码
  （.env 被 .gitignore 排除，凭据不入仓库）"
fi

"${VENV_PY}" - <<'PY' || exit 1
import sys
sys.path.insert(0, ".")
from app.config import get_settings
from app.db import check_connection

settings = get_settings()
ok, detail = check_connection()
if not ok:
    print(f"数据库不可用：{detail}", file=sys.stderr)
    print("  请确认 PostgreSQL 在运行，且 DATABASE_URL 指向的库已创建。", file=sys.stderr)
    sys.exit(1)
print(f"  数据库连接正常（{settings.database_url.rsplit('@', 1)[-1]}）")
print(f"  GPU 数据渠道：{settings.gpu_provider_mode}")
PY

# ---------------------------------------------------------------- 2. 迁移

say "2/4 迁移数据库到最新版本"
(cd "${BACKEND}" && "${VENV_PY}" -m alembic upgrade head)

# ---------------------------------------------------------------- 3. 演示数据

if [[ "${WITH_DEMO}" -eq 1 ]]; then
  say "3/4 灌入演示场景并渲染报告"
  echo "  注意：演示机通常没有 GPU，以下数据由**模拟 GPU 渠道**（SimulatedProvider）产生。"
  echo "        模拟来源（origin=simulated）会在报告与 GET /api/health 中明确标注。"
  (cd "${BACKEND}" && "${VENV_PY}" -m app.demo demo --diagnose-warnings)
else
  say "3/4 跳过演示数据（加 --with-demo 可灌入三类故障场景）"
fi

if [[ "${REPLAY_ONLY}" -eq 1 ]]; then
  say "完成（--replay 模式不启动服务）"
  exit 0
fi

# ---------------------------------------------------------------- 4. 启动

say "4/4 启动 API 服务"
echo "  健康检查：  http://127.0.0.1:${API_PORT}/api/health"
echo "  交互式文档：http://127.0.0.1:${API_PORT}/docs"
echo "  演示要点：  docs/DEMO_SCRIPT.md"
echo
if [[ "${WITH_DEMO}" -eq 0 ]]; then
  echo "  提示：库里还没有演示数据。另开一个终端执行"
  echo "        cd backend && ./.venv/bin/python -m app.demo demo --diagnose-warnings"
  echo
fi

cd "${BACKEND}"
exec "${VENV_PY}" -m uvicorn app.main:app --host "${API_HOST}" --port "${API_PORT}"
