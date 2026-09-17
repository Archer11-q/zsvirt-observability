#!/usr/bin/env bash
#
# 故障注入（演示用）：把一个赛题故障场景注入到正在运行的服务。
#
# 与 `python -m app.demo seed` 的区别：
#   - 本脚本走**真实的 HTTP 接口**（`POST /api/v1/ingest/batch`），
#     因此同时验证了上报契约与鉴权路径；
#   - 它不直接操作数据库，所以可以对着任意一台已启动的服务注入。
#
# 用法：
#   ./deploy/inject_fault.sh                      # 列出可用场景
#   ./deploy/inject_fault.sh container_oom        # 注入指定场景
#   ./deploy/inject_fault.sh all                  # 依次注入全部场景
#   SCENARIOS="gpu_self_exhausted" ./deploy/inject_fault.sh
#
# 环境变量：
#   BASE_URL   服务地址，默认 http://127.0.0.1:8080
#   TOKEN      若服务启用了鉴权，填 API_AUTH_TOKEN
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="${REPO_ROOT}/backend"
VENV_PY="${BACKEND}/.venv/bin/python"
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
TOKEN="${TOKEN:-}"

if [[ ! -x "${VENV_PY}" ]]; then
  echo "找不到虚拟环境解释器：${VENV_PY}" >&2
  exit 1
fi

CMD=("$@")
if [[ ${#CMD[@]} -eq 0 ]]; then
  echo "可用场景："
  (cd "${BACKEND}" && "${VENV_PY}" -m app.demo list)
  echo
  echo "用法：$0 <场景名|all>"
  exit 0
fi

# 先确认服务在跑。**先探活再注入** —— 否则注入失败的原因会是"连接被拒绝"，
# 与"场景定义有问题"混在一起，难以区分。
if ! curl -sf -m 5 "${BASE_URL}/api/health" >/dev/null; then
  echo "服务不可达：${BASE_URL}/api/health" >&2
  echo "  请先启动服务（./deploy/start.sh），或用 BASE_URL=... 指定地址。" >&2
  exit 1
fi

BASENAME="${CMD[0]}"
case "${BASENAME}" in
  all) SCENARIO_ARGS=() ;;
  *)   SCENARIO_ARGS=("${BASENAME}") ;;
esac

cd "${BACKEND}"
# 由 Python 生成上报体并通过 HTTP 提交，避免在 shell 里拼 JSON
SCENARIO_ARGS_JSON="$("${VENV_PY}" - "${SCENARIO_ARGS[@]+"${SCENARIO_ARGS[@]}"}" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1:]))
PY
)"

BASE_URL="${BASE_URL}" TOKEN="${TOKEN}" SCENARIO_ARGS_JSON="${SCENARIO_ARGS_JSON}" \
"${VENV_PY}" - <<'PY'
import json
import os
import sys

import httpx

from app.zsvirt.scenarios import SCENARIO_NAMES, build_batch, describe_scenarios

base_url = os.environ["BASE_URL"].rstrip("/")
token = os.environ.get("TOKEN", "")
requested = json.loads(os.environ["SCENARIO_ARGS_JSON"])
names = list(SCENARIO_NAMES) if not requested else requested

unknown = [n for n in names if n not in SCENARIO_NAMES]
if unknown:
    print(f"未知场景：{unknown}", file=sys.stderr)
    print("可用：" + ", ".join(SCENARIO_NAMES), file=sys.stderr)
    sys.exit(2)

headers = {"content-type": "application/json"}
if token:
    headers["authorization"] = f"Bearer {token}"

descriptions = describe_scenarios()
exit_code = 0

for name in names:
    payload = build_batch(name)
    print(f"\n=== 注入 {name} ===")
    print(f"    {descriptions[name]}")

    try:
        response = httpx.post(
            f"{base_url}/api/v1/ingest/batch", json=payload, headers=headers, timeout=20.0
        )
    except httpx.HTTPError as exc:
        print(f"    ✗ 请求失败：{exc}", file=sys.stderr)
        exit_code = 1
        continue

    if response.status_code != 200:
        print(f"    ✗ HTTP {response.status_code}：{response.text[:300]}", file=sys.stderr)
        exit_code = 1
        continue

    data = response.json()["data"]
    alerts = data.get("alerts", {})
    auto = alerts.get("autoDiagnosis", {})
    print(
        f"    ✓ 资源 {data['accepted']['resources']}  事件 {data['accepted']['events']}  "
        f"重复批次={data['duplicate']}"
    )
    print(
        f"      告警 新建 {alerts.get('created', 0)} / 累加 {alerts.get('updated', 0)}"
        f"  自动诊断 {len(auto.get('linked', {}))} 条"
    )
    for key, why in (
        ("skippedBelowSeverity", "低级告警按需诊断（见 docs/DEMO_SCRIPT.md）"),
        ("skippedAlreadyDiagnosed", "已有结论（幂等）"),
    ):
        count = auto.get(key, 0)
        if count:
            print(f"      跳过 {count} 条：{why}")

# 注入后打印当前告警与诊断，方便现场确认
try:
    alerts = httpx.get(f"{base_url}/api/v1/alerts", params={"limit": 20}, timeout=10.0).json()
    diagnoses = httpx.get(f"{base_url}/api/v1/diagnoses", params={"limit": 20}, timeout=10.0).json()
except httpx.HTTPError:
    sys.exit(exit_code)

print("\n--- 当前告警 ---")
for item in alerts["data"]["items"]:
    print(
        f"  [{item['severity']:8s}] {item['ruleId']:20s} {item['title']}"
        f"  state={item['state']} count={item['count']}"
    )

print("\n--- 当前诊断 ---")
for item in diagnoses["data"]["items"]:
    print(
        f"  {item['rootCause']:26s} conf={item['confidence']}  "
        f"规则集={item['ruleSetVersion']}"
    )
    if item["recommendation"]:
        advice = "；".join(r["code"] for r in item["recommendation"])
        print(f"      建议：{advice}")

sys.exit(exit_code)
PY
