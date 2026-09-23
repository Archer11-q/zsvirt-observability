"""任务 6.4：前后端端点对账（对运行中的服务实测响应，与前端声明的类型逐项比对）。

只做**静态 + 实测**对账，不跑前端：本机没有浏览器/Node 在 WSL 侧，而且
"字段名不一致"这类契约漂移靠比对响应就能抓到（那正是最容易漏的一类）。

对账内容：
  1. 前端调用清单里的每个端点在服务上**真实存在**（否则是 404 而不是渲染问题）；
  2. 响应里出现的字段，前端类型都声明了（**反向**最要紧：前端少声明会静默 undefined）；
  3. 前端**必需**的字段（类型定义里没有 `?`）在响应里都存在；
  4. 枚举取值在前端声明与 `/api/v1/dict` 之间一致。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

import httpx

BASE = "http://127.0.0.1:8000"

#: **仓库是真源**，`/mnt/d/...` 只是暂存镜像（可能落后，且不含后端新模块）。
#: 顺序必须如此：镜像优先过一次，就会对着旧前端得出"✓ 未发现不一致"这种
#: 假阴性结论 —— 比不跑对账更危险，因为它给了一个可信的外观。
_REPO_FE = pathlib.Path("/home/archer/workspace/zsvirt-observability/frontend/src")
_MIRROR_FE = pathlib.Path("/mnt/d/CLion/zsvirt-observability/frontend/src")

FE = _REPO_FE if _REPO_FE.exists() else _MIRROR_FE
USING_MIRROR = FE != _REPO_FE

if not (FE / "types.ts").exists():
    raise SystemExit(
        f"找不到前端源码（尝试过 {_REPO_FE} 与 {_MIRROR_FE}）；"
        "对账必须在仓库工作区内运行"
    )

TYPES = (FE / "types.ts").read_text(encoding="utf-8")
ENDPOINTS = (FE / "api/endpoints.ts").read_text(encoding="utf-8")

problems: list[str] = []
notes: list[str] = []

#: 把"对的是哪一份前端"写进输出。静默推断路径 = 无法发现对错了源。
notes.append(f"前端源码：{FE}")
if USING_MIRROR:
    problems.append(
        f"正在对账**暂存镜像**而非仓库（{FE}）：镜像会落后，结论不可信。"
        "请在仓库工作区内运行本脚本"
    )


def declared_types() -> dict[str, tuple[list[str], list[str], str]]:
    """`类型名 → (必需字段, 可选字段, 类型体原文)`。

    正则要同时匹配两种写法：`export interface X {`（空格）与
    `export type X = {`（等号）。只写 `[={]` 会漏掉前者 —— 第一次跑就漏了全部
    接口，报告成"前端声明类型 0 个"，看起来像前端没有类型定义。

    保留类型体原文是为了后面做**内联枚举**对账（如 `status: 'running' | ...`）。
    """
    out: dict[str, tuple[list[str], list[str], str]] = {}
    for name, body in re.findall(
        r"export (?:interface|type) (\w+)(?:<[^>]*>)?[^{=]*[={]?\s*\{(.*?)\n\}", TYPES, re.S
    ):
        required = re.findall(r"^\s{2}(\w+):", body, re.M)
        optional = re.findall(r"^\s{2}(\w+)\?:", body, re.M)
        out[name] = (required, optional, body)
    return out


TYPES_MAP = declared_types()
notes.append(f"前端声明类型 {len(TYPES_MAP)} 个")

# ---------------------------------------------------------------- 端点存在性
paths = sorted(set(re.findall(r"'(/api/v1/[^']*)'", ENDPOINTS)))
notes.append(f"前端调用端点 {len(paths)} 个")

# 已知会被前端拼 id 的路径，替换成一个真实 id 再探活
sample_ids = {}


def probe(path: str) -> tuple[int, dict | None]:
    """探活一个端点。**只对读端点用 GET**：`/alerts/actions` 是 POST，
    GET 它会得到 405/404，那是方法不匹配而不是端点缺失 —— 把它当成"端点不可用"
    会报出一个假问题。"""
    concrete = path
    for token in ("${id}", "{id}", "${alertId}", "{alertId}"):
        if token in concrete:
            key = "alert" if "alert" in token.lower() else "id"
            concrete = concrete.replace(token, sample_ids.get(key, "unknown"))
    if "unknown" in concrete:
        return 0, None

    method = "POST" if concrete.endswith("/actions") else "GET"
    try:
        if method == "POST":
            resp = httpx.request(method, f"{BASE}{concrete}", json={}, timeout=10)
            # 400/422 说明端点存在、只是我们没给合法请求体
            return (200 if resp.status_code in (200, 400, 422) else resp.status_code), None
        resp = httpx.get(f"{BASE}{concrete}", timeout=10)
    except httpx.HTTPError as exc:  # pragma: no cover
        return -1, {"error": str(exc)}
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


# 先准备真实 id
alerts = httpx.get(f"{BASE}/api/v1/alerts", params={"limit": 1}, timeout=10).json()
events = httpx.get(f"{BASE}/api/v1/events", params={"limit": 1}, timeout=10).json()
diagnoses = httpx.get(f"{BASE}/api/v1/diagnoses", params={"limit": 1}, timeout=10).json()
if alerts["data"]["items"]:
    sample_ids["alert"] = alerts["data"]["items"][0]["id"]
if events["data"]["items"]:
    sample_ids["id"] = events["data"]["items"][0]["id"]
if diagnoses["data"]["items"]:
    sample_ids.setdefault("id", diagnoses["data"]["items"][0]["id"])

for path in paths:
    status, _ = probe(path)
    if status == 0:
        notes.append(f"  skip（需要 id）  {path}")
    elif status != 200:
        problems.append(f"端点不可用 HTTP {status}：{path}")
    else:
        notes.append(f"  ok   {path}")

# ---------------------------------------------------------------- 字段比对
# 端点到前端类型的映射（按前端页面实际消费的对象）
CHECK = {
    "/api/v1/health": "HealthResponse",
    "/api/v1/topology": "TopologyNode",
    "/api/v1/events": "EventItem",
    "/api/v1/alerts": "AlertItem",
    # 工作负载的条目类型名是 `Workload` 而不是 `WorkloadItem` —— 写错名字会让
    # 该端点在报告里显示"前端未声明"而被静默跳过，那种检查比没有更危险
    "/api/v1/workloads": "Workload",
    "/api/v1/diagnoses": "Diagnosis",
}


def item_of(payload: dict, path: str) -> dict | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    if path == "/api/v1/health":
        return data
    for key in ("items", "nodes", "alerts"):
        value = data.get(key)
        if isinstance(value, list) and value:
            return value[0]
    # 单对象端点
    return data if "id" in data else None


for path, type_name in CHECK.items():
    status, payload = probe(path)
    if status != 200 or payload is None:
        continue
    sample = item_of(payload, path)
    if sample is None:
        notes.append(f"  {path}: 无样本数据，跳过字段比对")
        continue

    required, optional, _body = TYPES_MAP.get(type_name, ([], [], ""))
    if not required and not optional:
        notes.append(f"  {path}: 前端未声明 {type_name}，跳过")
        continue

    declared = set(required) | set(optional)
    actual = set(sample)

    missing_in_fe = actual - declared
    if missing_in_fe:
        problems.append(
            f"{path}: 响应字段前端未声明 {sorted(missing_in_fe)}（{type_name}）"
        )

    missing_in_api = set(required) - actual
    if missing_in_api:
        problems.append(
            f"{path}: 前端必需字段在响应里缺失 {sorted(missing_in_api)}（{type_name}）"
        )

    notes.append(
        f"  {type_name}: 声明 {len(declared)} 字段，响应 {len(actual)} 字段，"
        f"未声明 {len(missing_in_fe)}，缺失 {len(missing_in_api)}"
    )

# ---------------------------------------------------------------- 枚举对账
dict_payload = httpx.get(f"{BASE}/api/v1/dict", timeout=10).json()["data"]
dict_map = dict_payload.get("data", dict_payload)
notes.append("")
notes.append(f"字典段落：{sorted(dict_map)}")

for section, name in (
    ("severity", "Severity"),
    ("alertState", "AlertState"),
    ("resourceStatus", "ResourceStatus"),
):
    declared = re.findall(rf"export type {name} = ([^\n;]+)", TYPES)
    api_values = set(dict_map.get(section, {}))
    if not declared:
        # 前端可能把枚举内联在字段上（如 `status: 'running' | 'stopped' | ...`）。
        # 这种情况也要对账 —— 漏掉它等于放过一整类漂移。
        #
        # 必须限定到**具体接口**：`status` 这个字段名在 `HealthResponse`（值域
        # `ok|degraded|down`）和 `Workload`/`TopologyNode`（值域是资源业务状态）
        # 里都出现。不限定接口就会把两套无关值域并成一个集合，报出假的不一致
        # （第一次跑就报了这个假问题）。
        owner = {"ResourceStatus": "Workload", "Observability": "TopologyNode"}.get(name)
        if owner is None or owner not in TYPES_MAP:
            notes.append(f"  {name}: 前端未声明")
            continue
        body = TYPES_MAP[owner][2]
        # 只取**目标字段那一行**的联合字面量，不要把接口里其他字段的值域一起收进来
        # （`Workload` 里 `observability` 与 `status` 值域不同，混收会互相污染）。
        field_name = {"ResourceStatus": "status", "Observability": "observability"}[name]
        found: set[str] = set()
        for line in re.findall(rf"^\s{{2}}{field_name}\??: ([^\n]+)", body, re.M):
            found |= set(re.findall(r"'([^']+)'", line))
        if not found:
            notes.append(f"  {name}: 前端未声明（{owner} 里也没有内联 {field_name}）")
            continue
        if found != api_values:
            problems.append(
                f"{section} 内联枚举不一致（取自 {owner}）："
                f"仅前端有 {sorted(found - api_values)}，"
                f"仅后端有 {sorted(api_values - found)}"
            )
        else:
            notes.append(f"  {name}: {len(found)} 项一致（前端内联于 {owner}）")
        continue
    fe_values = set(re.findall(r"'([^']+)'", declared[0]))
    if fe_values != api_values:
        problems.append(
            f"{section} 枚举不一致：仅前端有 {sorted(fe_values - api_values)}，"
            f"仅后端有 {sorted(api_values - fe_values)}"
        )
    else:
        notes.append(f"  {name}: {len(fe_values)} 项一致")

# ---------------------------------------------------------------- 输出
report = ["=== 任务 6.4 前后端端点对账 ===", *notes, ""]
if problems:
    report.append(f"发现 {len(problems)} 处不一致：")
    report.extend("  ✗ " + p for p in problems)
else:
    report.append("✓ 未发现不一致")

pathlib.Path("/tmp/fe_reconcile.txt").write_text("\n".join(report), encoding="utf-8")
print("\n".join(report))
sys.exit(1 if problems else 0)
