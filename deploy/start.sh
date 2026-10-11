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
#   ./deploy/start.sh --demo          # 演示基线 + 迁移 + 启动（**录制演示用这个**）
#   ./deploy/start.sh --with-demo     # 演示基线 + 迁移 + 灌入三类场景 + 启动
#   ./deploy/start.sh --replay        # 演示基线 + 只灌数据并打印报告，不启动服务
#
# `--demo` 只改三个环境变量（见下方"演示基线"），不灌任何数据 ——
# 演示时故障是**现场注入**的，预先把三类场景灌好会让后面的
# `./deploy/inject_fault.sh <场景>` 变成幂等回放（batchId 固定），画面上什么都没发生。
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="${REPO_ROOT}/backend"
VENV_PY="${BACKEND}/.venv/bin/python"

API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8080}"

WITH_DEMO=0
REPLAY_ONLY=0
DEMO_MODE=0
for arg in "$@"; do
  case "$arg" in
    --demo) DEMO_MODE=1 ;;
    --with-demo) WITH_DEMO=1; DEMO_MODE=1 ;;
    --replay) REPLAY_ONLY=1; WITH_DEMO=1; DEMO_MODE=1 ;;
    -h|--help)
      sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "未知参数：$arg（-h 查看用法）" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- 演示基线
#
# 这三项**只影响演示/回放**，不改变 `.env.example` 里面向真实部署的默认值。
# 每一项都对应一个实测到的缺陷（2026-09-30 在 8080 上跑完整演示路径时发现）：
#
# 1. `SIMULATED_DATA_ENABLED` / `GPU_PROVIDER`：演示机没有 GPU，数据由模拟渠道
#    产生。不显式打开时 `GET /api/health` 会报 `mode=auto, simulated=false`，
#    而演示脚本 §1 正是让评委看这个字段 —— **接口说了假话，比没有这个字段更糟**。
# 2. `ALERT_RECOVERY_AFTER_SEC=0`：关闭按墙钟计算的告警自动恢复。演示数据的
#    时间基准是固定的过去时刻（为了可复现），恢复判据拿墙钟一比就"过期"，
#    于是服务起来一个周期内全部告警变 `resolved`、`?state=firing` 返回空。
#    理由与 `RESOURCE_STALE_AFTER_SEC=0` 完全相同，见 `backend/app/config.py`。
if [[ "${DEMO_MODE}" -eq 1 ]]; then
  export SIMULATED_DATA_ENABLED="${SIMULATED_DATA_ENABLED:-true}"
  export GPU_PROVIDER="${GPU_PROVIDER:-simulated}"
  export ALERT_RECOVERY_AFTER_SEC="${ALERT_RECOVERY_AFTER_SEC:-0}"
  printf '\033[2m演示基线：SIMULATED_DATA_ENABLED=true  GPU_PROVIDER=simulated  ALERT_RECOVERY_AFTER_SEC=0\033[0m\n'
fi
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

# 必须在 backend/ 下执行：下面第一行就是 `sys.path.insert(0, ".")`，
# 而 `app` 包位于 backend/。脚本其余两处 Python 调用（迁移、灌数据）都用了
# `(cd "${BACKEND}" && ...)`，这里早先漏了 —— 后果是**按演示脚本的第一条命令
# 就会拿到 ModuleNotFoundError**，而那是评委看到的第一屏。
(
  cd "${BACKEND}" || exit 1
  "${VENV_PY}" - <<'PY'
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
) || exit 1

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
