"""从**真实容器运行时**采集故障事件并上报到 Crosslayer（赛题交付要求 e）。

## 这个脚本存在的理由

`deploy/inject_fault.sh` 走的是 `backend/app/zsvirt/scenarios.py` ——
它按已冻结契约**构造**上报载荷。那条路对演示很方便，但它的证据是**人工模拟**的
（`origin=simulated`）。赛题交付要求 e 要的是"故障场景与**证据**"，
而"证据"的说服力取决于它是不是真的发生过。

这个脚本走另一条路：**真的去问容器运行时**。

    docker 里真的跑着推理服务 → 真的被内存限额压死 → 真的 OOMKilled
    → 本脚本从容器运行时 API 读到这件事 → 按契约上报

## 复用成员 A 的代码，只替换传输层

采集与映射逻辑**一行都没改**：直接用 `agents/probe/collector/container.py`
的 `ContainerCollector`，上报用 `agents/probe/reporter.py` 的 `build_batch`
与 `Reporter`。本文件只做两件事：

1. **传输层替身**：`ContainerCollector` 通过 `/var/run/docker.sock` 访问 Docker
   Engine API。开发机上 Docker Desktop 的 WSL 集成没生效（WSL 内没有该 socket），
   于是这里提供一个 `CliDockerClient`：把 A 用到的两个 API 路径
   （`/containers/json`、`/events`）翻译成 `docker` / `docker.exe` 命令行调用，
   再翻译回 Engine API 的字段形状喂给 A 的采集器。
   **当 socket 存在时（即真实部署的 VM 内），走的是 A 的原始代码路径**，
   这个替身根本不会被构造。

2. **补齐容器运行时真实暴露、但 A 的采集器尚未映射的指标**：`exitCode`、
   `oomKilledCount`、`container_memory_limit_bytes`。这三个值不是编的，
   分别来自 `State.ExitCode`、`State.OOMKilled` / `oom` 事件计数、
   `HostConfig.Memory`。**刻意不造 `container_memory_usage_ratio`** ——
   Docker 不记录容器死亡时的峰值内存，反推一个比值就是编数据。

## 诚实边界（务必连同本文件一起读）

| 事实 | 说明 |
|---|---|
| 容器、OOM、退出码、重启次数 | **真实观测**，来自容器运行时 API |
| 事件来源 `origin` | `probe`（真实采集）。**不是** `simulated` |
| 宿主 VM 资源 | 由本脚本补一条 `vm` 资源代表"容器实际所在的 Linux VM"（Docker Desktop 的 WSL2 虚拟机）。**它不是 ZSvirt 管理的虚拟机**，资源 `name` 里写明了这一点，`vmId` 也用 `devbox00-...` 这种一眼可辨的替身串，**不伪造 ZSvirt UUID** |
| 跨层链的上半段 | 缺失。真实部署里 `vGPU → VM → 容器` 由 ZSvirt 资产 API + 探针 `vmId` 共同锚定，而 `vmId` 的注入方式仍在等命题方答复（外部阻塞 **X-09**） |

`vmId` 形态：B 侧 `ingest/schemas.py::_vm_id_shape` 要求 `vm:zsvirt:<uuid>`，
否则整批 422。这里传一个**结构合法但一眼可辨是替身**的值，
并让资源 `name` 说清楚 —— 伪造一个看起来真实的 ZSvirt UUID 会让
"这条链锚在哪台 VM 上"变成无法核对的断言。

用法：

    python collect_real_events.py                # 采集一次并上报
    python collect_real_events.py --watch 5      # 每 5 秒采集一次（演示时开着）
    python collect_real_events.py --dry-run      # 只打印载荷，不上报

环境变量：`BACKEND`（默认 `http://127.0.0.1:8080`）、`TOKEN`。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.parse
from typing import Any

# ---------------------------------------------------------------- 路径准备
#
# `agents/` 是成员 A 的交付物，包名是 `probe`。本脚本在 `deploy/sample-workload/`，
# 所以要往上找到仓库根再进 `agents/`：deploy/sample-workload → deploy → 仓库根。
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "agents"
if not (AGENTS_DIR / "probe").is_dir():
    print(f"找不到成员 A 的探针包：{AGENTS_DIR / 'probe'}", file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(AGENTS_DIR))

from probe.collector.container import (  # noqa: E402
    ContainerCollector,
    DockerClient,
    _UnixHTTPConnection,
)
from probe.model import Resource  # noqa: E402
from probe.reporter import OK, Reporter, build_batch  # noqa: E402

#: 容器实际所在的 Linux VM 的**替身标识**。
#:
#: 刻意不用真实形状的 UUID：B 侧只校验 `vm:zsvirt:<...>` 的段数与前缀，
#: 这里用 `devbox00-...` 让任何看到这条数据的人都能立刻意识到
#: "这不是 ZSvirt 管的机器"。真实部署下这个值来自命题方给的 VM UUID（X-09）。
STANDIN_VM_ID = "vm:zsvirt:devbox00-0000-4000-8000-000000000001"
STANDIN_VM_SOURCE_ID = "devbox-vm"
AGENT_ID = "devbox-probe-bridge"
DOCKER_SOCKET = "/var/run/docker.sock"


# ================================================================ 传输层替身


class NativeDockerClient(DockerClient):
    """走 unix socket，但**修正 `events()` 的 URL 编码缺陷**。

    ## 缺陷（2026-09-30 实测，成员 A 的 `probe/collector/container.py`）

    A 的 `DockerClient.events()` 这样拼查询串：

        qs = f"/events?since={since}&until={until}&filters={json.dumps(filters)}"

    `json.dumps({"type": ["container"], "event": ["oom", "die", "restart"]})`
    里含**空格**（`": "` 与 `", "`），而 `http.client` 在发送前就拒绝这种 URL：

        http.client.InvalidURL: URL can't contain control characters
        (found at least ' ')

    `InvalidURL` 继承 `HTTPException`，而 `_get` 的 `except` 正好把它吞掉，
    返回 `None` → `events()` 返回 `[]`。**于是容器事件永远是空的**：

    - `ContainerCollector` 从不产出 `container.oom_killed` / `container.restart`
      —— 这两个事件类型在真实采集路径上是**死数据**；
    - B 侧的 `R-CTR-OOM-010` / `R-CTR-RESTART-011` 因此永远不会由真实数据触发；
    - 赛题三类场景里的「容器/进程异常」在真实部署下**无法演出来**；
    - 单测抓不到：它们用小夹具或假 client，而缺陷只在真 URL 进 `http.client` 时出现。

    修法只有一处：`filters` 必须 URL 编码。这里用 `urllib.parse.urlencode`
    整体编码，顺带把 `json.dumps` 的 `separators` 收紧（不含空格）。

    ⚠️ 这是在**替身层**修的，A 的文件没有被改动 —— 按项目纪律，`agents/`
    归成员 A 所有。修正应当由 A 落到 `probe/collector/container.py`，
    同步单见 `docs/PROBE_DEFECT_SYNC.md`。

    ## 缺陷二（更隐蔽）：`_get` 用 `json.loads` 解析**换行分隔的 JSON 流**

    即使把 filters 编码对了也还是拿不到事件，因为 `/events` 的响应体是

        {"Action":"oom", ...}\n{"Action":"die", ...}\n

    —— **NDJSON**，不是一个 JSON 数组。而 A 的 `_get` 结尾是
    `return json.loads(body)`，对多行 NDJSON 必然抛 `JSONDecodeError`，
    又被同一个 `except` 吞掉，返回 `None`。`/containers/json` 返回的是真正的
    JSON 数组，所以那条路径没问题 —— **同一个 `_get` 服务两种响应形状，
    而只有一种被实现**。

    两个缺陷的效果完全一样（`events()` 恒为空），但成因不同、修法也不同，
    所以这里**不复用 `_get`**，直接发请求并逐行解析。
    """

    def events(self, since: int, until: int) -> list[dict[str, Any]]:
        filters = {"type": ["container"], "event": ["oom", "die", "restart"]}
        query = urllib.parse.urlencode(
            {
                "since": since,
                "until": until,
                "filters": json.dumps(filters, separators=(",", ":")),
            }
        )
        # 刻意**不用** `self._get`：它按"单个 JSON 文档"解析，而 `/events`
        # 是 NDJSON 流（见类文档的"缺陷二"）。复用它会再次静默返回空。
        connection = _UnixHTTPConnection(DOCKER_SOCKET)
        try:
            connection.request("GET", f"/events?{query}")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            if response.status >= 400:
                return []
        except (OSError, http.client.HTTPException):
            return []
        finally:
            connection.close()

        events: list[dict[str, Any]] = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events


class CliDockerClient(DockerClient):
    """把 A 用到的三个 Engine API 路径翻译成 `docker` 命令行。

    **只在 `/var/run/docker.sock` 不存在时才构造**（见 `make_client`）。
    在真实部署的 VM 里，A 的原始 `DockerClient` 直接可用，这里不会被用到。

    为什么用命令行而不是直接连 API：某些开发机上 Docker Desktop 的守护进程在
    Windows 侧、通过命名管道暴露；WSL 里既没有 unix socket，也没有命名管道客户端。
    此时 `docker.exe`（Windows 二进制，WSL 可直接执行）是唯一现成的通道。

    命令行这条路顺带绕开了上面那个编码缺陷 —— 过滤器是作为独立 argv 传的，
    不经过 URL 拼接。但**不能因此就不修 A 的代码**：真实部署走的是 socket。
    """

    def __init__(self, binary: str) -> None:
        self._binary = binary

    def _run(self, args: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                [self._binary, *args],
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    def _get(self, path: str) -> Any | None:  # type: ignore[override]
        if path.startswith("/containers/json"):
            return self._containers()
        # `/containers/<id>/json` 是 inspect：`RestartCount` / `State.OOMKilled` /
        # `HostConfig.Memory` 只在这里有，而 `/containers/json` 里没有 ——
        # 这正是 A 的采集器把 `restartCount` 恒报 0 的原因。
        if path.startswith("/containers/") and path.endswith("/json"):
            container_id = path[len("/containers/") : -len("/json")]
            return self._inspect([container_id])
        if path.startswith("/events"):
            return self._events(path)
        return None

    # ---- `/containers/json?all=1` → docker inspect ----
    def _inspect(self, ids: list[str]) -> Any | None:
        raw = self._run(["inspect", *ids])
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _containers(self) -> list[dict[str, Any]]:
        # `docker ps -aq` 给的是短 ID，而 A 的采集器把 `Id[:12]` 当 sourceId ——
        # 用 `--no-trunc` 拿全长 ID，让截断仍然发生在 A 的代码里（保持行为一致）。
        ids_raw = self._run(["ps", "-aq", "--no-trunc"])
        ids = [line.strip() for line in (ids_raw or "").splitlines() if line.strip()]
        if not ids:
            return []

        inspected = self._inspect(ids)
        if not inspected:
            return []

        containers: list[dict[str, Any]] = []
        for item in inspected:
            state = item.get("State") or {}
            config = item.get("Config") or {}
            host_config = item.get("HostConfig") or {}
            containers.append(
                {
                    # 字段名对齐 Docker Engine API，**不是为了好看** ——
                    # A 的采集器按这些键取值，改名就等于改了它的行为。
                    "Id": item.get("Id", ""),
                    "Names": [item.get("Name", "")],
                    "State": state.get("Status", "unknown"),
                    "Image": config.get("Image", ""),
                    "RestartCount": item.get("RestartCount", 0),
                    # --- 下面几项 A 的采集器暂时没读，本脚本用来补指标 ---
                    "ExitCode": state.get("ExitCode"),
                    "OOMKilled": state.get("OOMKilled"),
                    "MemoryLimitBytes": host_config.get("Memory"),
                }
            )
        return containers

    # ---- `/events?since=&until=&filters=` → docker events ----
    def _events(self, path: str) -> list[dict[str, Any]]:
        query = path.split("?", 1)[1] if "?" in path else ""
        params = dict(
            part.split("=", 1) for part in query.split("&") if "=" in part
        )
        args = [
            "events",
            "--since",
            params.get("since", "0"),
            "--until",
            params.get("until", str(int(time.time()))),
            "--filter",
            "type=container",
            "--format",
            "{{json .}}",
        ]
        # 事件过滤器逐个透传：A 的采集器按 Action 分支，多取事件不会改变它的行为，
        # 但少取会让 oom/restart 漏掉 —— 宁可多取。
        for event in ("oom", "die", "restart"):
            args.extend(["--filter", f"event={event}"])

        out = self._run(args)
        events: list[dict[str, Any]] = []
        for line in (out or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events


def make_client() -> tuple[DockerClient, str]:
    """有 unix socket 就用 A 的代码路径（但修掉 events 的编码缺陷），否则用命令行替身。"""
    if os.path.exists(DOCKER_SOCKET):
        return (
            NativeDockerClient(),
            "原生 unix socket（A 的代码路径 + 修正 events 的 URL 编码缺陷）",
        )

    for binary in ("docker", "docker.exe"):
        found = shutil.which(binary)
        if found:
            return CliDockerClient(found), f"命令行替身（{found}）"
    raise SystemExit(
        "找不到可用的 Docker 通道：既没有 /var/run/docker.sock，也没有 docker 可执行文件。"
    )


# ================================================================ 指标补齐


def inspect_containers(client: DockerClient, container_ids: set[str]) -> dict[str, dict[str, Any]]:
    """按 id 取 inspect 结果。

    **为什么必须单独查一次**：`/containers/json` 里**没有** `RestartCount`、
    `State.OOMKilled`、`HostConfig.Memory` —— 这三个只在
    `/containers/<id>/json`（即 `docker inspect`）里。成员 A 的采集器读的是
    `/containers/json`，于是它上报的 `restartCount` **恒为 0**（字段不存在，
    `.get(..., 0)` 拿了默认值）。这是一个静默的错值：不报错、不缺失，只是永远为零，
    而"重启次数为 0"与"重启了 7 次"在诊断里是完全不同的证据。

    这条已登记为需要成员 A 修正的项（`docs/REQUIREMENTS_GAP.md`）。
    """
    result: dict[str, dict[str, Any]] = {}
    for container_id in container_ids:
        if not container_id:
            continue
        data = client._get(f"/containers/{container_id}/json")
        if isinstance(data, dict) and data.get("Id"):
            result[data["Id"]] = data
    return result


def enrich_events(events: list[Any], inspected: dict[str, dict[str, Any]]) -> list[Any]:
    """给 A 采到的容器事件补上运行时**真实暴露**的指标。

    只补能从 API 读到的值，读不到就**不写这个键**（`app/` 的 ZWatch 渠道也是
    这个原则：缺失与"值为 0"在库里必须长得不一样）。

    ⚠️ 这是**替身层**的行为，不是 A 的采集器的行为。
    """
    for event in events:
        info = _match_container(inspected, event.resource_source_id)
        if info is None:
            continue
        state = info.get("State") or {}
        host_config = info.get("HostConfig") or {}
        metrics = dict(event.metrics or {})

        # `exitCode` 只在**容器确实已经退出**时才带上。
        #
        # 理由是实测出来的：容器正在重启循环里跑着的时候，`inspect` 报的
        # `ExitCode` 是 0（"上一次退出码"在这个瞬间是它自己启动成功的那次），
        # 于是会出现"OOM 事件 + exitCode=0"这种自相矛盾的证据 ——
        # 看的人会以为"OOM 了但退出码正常"，从而怀疑整条链。
        # 宁可**不写这个键**（缺失），也不要写一个会误导的值。
        exited = state.get("Status") in ("exited", "dead") or state.get("OOMKilled") is True
        if state.get("ExitCode") is not None and exited:
            metrics["exitCode"] = state["ExitCode"]
        if host_config.get("Memory"):
            metrics["container_memory_limit_bytes"] = host_config["Memory"]
        if state.get("OOMKilled"):
            # 被杀过的次数下界 = 此前重启次数 + 本次。
            # 不用"凭空写 1"：反复被杀时这个数才有区分度，而诊断正需要它。
            metrics["oomKilledCount"] = int(info.get("RestartCount") or 0) + 1

        if metrics:
            event.metrics = metrics
        # 渠道标注写进 `raw.origin`（D-107）：真实采集，不是 simulated。
        raw = dict(event.raw or {})
        raw.setdefault("origin", "probe")
        raw["observedVia"] = "container-runtime-api"
        event.raw = raw
    return events


def _match_container(
    inspected: dict[str, dict[str, Any]], short_id: str
) -> dict[str, Any] | None:
    if not short_id:
        return None
    for full_id, info in inspected.items():
        if full_id.startswith(short_id) or short_id.startswith(full_id[:12]):
            return info
    return None


# ================================================================ 主流程


def repair_event_targets(events: list[Any], resources: list[Resource]) -> list[Any]:
    """补回事件指向的资源。

    ## 缺陷三（第三个独立成因，同一个症状）

    A 的采集器这样取事件归属的容器：

        cid = ev.get("id", "")

    但 Docker Engine API 的 `/events` 响应里**没有顶层 `id` 字段** ——
    容器 ID 在 `Actor.ID`。实测（2026-09-30）：

        ev.get('id')       = None
        ev['Actor']['ID']  = 'ef193b87b224...'   ← 真的在这里

    于是 `source_id = ""[:12] = ""`，**每一条事件的 `resource_source_id` 都是空串**。
    这类事件即便进了库也无法关联到任何资源 —— 比"没有事件"更难排查，
    因为事件数量看起来是对的，只是全都挂不上。

    修法：优先用 `Actor.ID[:12]`（与 A 在资源侧对 `Id[:12]` 的截断规则一致），
    取不到再退回按容器名匹配。**只在 `resource_source_id` 为空时修补** ——
    非空说明 A 那边某天修好了，这里不该覆盖它的结果。
    """
    by_short = {r.source_id: r.source_id for r in resources if r.kind == "container"}
    by_name = {r.name: r.source_id for r in resources if r.kind == "container"}
    repaired = 0

    for event in events:
        if event.resource_source_id:
            continue
        actor = (event.raw or {}).get("Actor") or {}
        short = str(actor.get("ID") or "")[:12]
        if short in by_short:
            event.resource_source_id = by_short[short]
            repaired += 1
            continue
        name = (actor.get("Attributes") or {}).get("name") or ""
        if name in by_name:
            event.resource_source_id = by_name[name]
            repaired += 1

    if repaired:
        print(f"          （修补了 {repaired} 条事件的资源指向：A 读的是不存在的 ev['id']）")
    return events


def collect_once(    collector: ContainerCollector, *, with_host_vm: bool, filter_name: str = ""
) -> tuple[list[Resource], list[Any]]:
    resources, events = collector.collect()

    # 先把事件的资源指向补回来（A 读的 `ev["id"]` 不存在，见 repair_event_targets），
    # **必须排在过滤之前** —— 过滤正是按 `resource_source_id` 判断的，
    # 空串会被当成"不属于观测范围"而全部丢掉，症状就是"一条事件都收不到"。
    events = repair_event_targets(events, resources)

    # ---- 观测范围过滤 ----
    #
    # 成员 A 的 `ContainerCollector` 会列出**这台机器上的所有容器**
    # （实测：连无关的 grafana / mysql / prometheus 一起报上来）。
    # 探针部署在 ZSvirt 虚拟机里时这是对的 —— 那台 VM 上就该只有被测负载。
    # 但开发机上混着一堆别的容器，全报上去会把资源图与告警列表冲淡，
    # 而"演示时列表里一半是不相干的容器"会让评委无法判断哪条才是重点。
    #
    # 因此这里按**名字前缀**收敛观测范围（`--filter-name`，默认 `demo-`）。
    # 真实部署用标签选择器更合适；这里用前缀是因为 A 的采集器不传 filters，
    # 替身层不该为了过滤去改它的调用形状。
    if filter_name:
        resources = [
            r for r in resources if r.kind != "container" or r.name.startswith(filter_name)
        ]
        kept = {r.source_id for r in resources if r.kind == "container"}
        events = [e for e in events if e.resource_source_id in kept]

    if with_host_vm:
        # 补一条宿主 VM 资源，让图是连通的（否则容器没有父节点）。
        # `parentSourceId` 指向它，`status` 为 running —— 容器确实跑在里面。
        vm = Resource(
            kind="vm",
            source_id=STANDIN_VM_SOURCE_ID,
            name="开发机 Linux VM（Docker Desktop WSL2）—— 非 ZSvirt 管理，演示替身",
            status="running",
            attributes={
                "origin": "probe",
                "note": "本资源由采集替身补出；真实部署下 VM 层来自 ZSvirt 资产 API（X-09）",
            },
        )
        for resource in resources:
            if resource.kind == "container" and not resource.parent_source_id:
                resource.parent_source_id = STANDIN_VM_SOURCE_ID
        resources = [vm, *resources]

    # 查 inspect：补齐 A 的采集器读不到的字段（见 `inspect_containers` 的说明）。
    client = collector._client  # noqa: SLF001 - 替身层需要底层数据，见文件头说明
    wanted = {r.source_id for r in resources if r.kind == "container"}
    wanted |= {e.resource_source_id for e in events if e.resource_source_id}
    inspected = inspect_containers(client, wanted)

    # 顺带修正资源上的 `restartCount`：A 的采集器从 `/containers/json` 取，
    # 而那里没有这个字段，于是它**恒为 0**。不修正的话，拓扑上"重启了 7 次"
    # 会显示成"从未重启"，而这是最容易被当成"没问题"的那种错值。
    for resource in resources:
        if resource.kind != "container":
            continue
        info = _match_container(inspected, resource.source_id)
        if info is None:
            continue
        true_count = int(info.get("RestartCount") or 0)
        if resource.attributes.get("restartCount") != true_count:
            resource.attributes = {
                **resource.attributes,
                "restartCount": true_count,
                "restartCountCorrectedBy": "collect_real_events（A 的采集器读的字段不存在）",
            }

    events = enrich_events(events, inspected)
    return resources, events


def main() -> int:
    parser = argparse.ArgumentParser(description="从真实容器运行时采集并上报故障事件")
    parser.add_argument("--watch", type=float, metavar="SEC", help="按间隔持续采集（演示用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印载荷，不上报")
    parser.add_argument(
        "--no-host-vm",
        action="store_true",
        help="不补宿主 VM 资源（保持成员 A 采集器的原始行为：容器无父节点）",
    )
    parser.add_argument(
        "--filter-name",
        default="demo-",
        help="只观测名字以此开头的容器（默认 demo-）。传空串则观测全部容器",
    )
    args = parser.parse_args()

    backend = os.environ.get("BACKEND", "http://127.0.0.1:8080")
    token = os.environ.get("TOKEN", "")

    client, transport = make_client()
    collector = ContainerCollector()
    collector._client = client  # noqa: SLF001 - 注入传输替身，采集逻辑不变

    print(f"采集通道：{transport}")
    print(f"上报地址：{backend}/api/v1/ingest/batch")
    if args.dry_run:
        print("模式：dry-run（不上报）")
    print()

    reporter = Reporter(backend, token)

    #: 心跳间隔。没有事件时也要**定期上报资源清单** —— 否则"被观测负载"
    #: 在故障发生之前根本不出现在拓扑上，演示时评委看不到"健康长什么样"，
    #: 而"先给基线、再给故障"正是这套演示的因果骨架。
    #: 真实探针也是每 `batch_sec` 上报一次，不区分有没有事件。
    heartbeat_sec = 30.0
    last_sent = 0.0

    while True:
        resources, events = collect_once(
            collector, with_host_vm=not args.no_host_vm, filter_name=args.filter_name
        )
        stamp = time.strftime("%H:%M:%S")

        if not events:
            print(f"[{stamp}] 无新事件（资源 {len(resources)} 个）")
        else:
            print(f"[{stamp}] 采到 {len(events)} 条事件、{len(resources)} 个资源：")
            for event in events:
                print(f"          {event.type:26} {event.severity:9} {event.message}")
                if event.metrics:
                    print(f"          └─ 指标 {json.dumps(event.metrics, ensure_ascii=False)}")

        heartbeat_due = last_sent == 0.0 or (time.monotonic() - last_sent) >= heartbeat_sec

        if args.dry_run:
            batch_id, payload = build_batch(AGENT_ID, STANDIN_VM_ID, resources, events)
            print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
        elif events or heartbeat_due:
            batch_id, payload = build_batch(AGENT_ID, STANDIN_VM_ID, resources, events)
            status, body = reporter.send(payload)
            if status == OK:
                data = body.get("data", {})
                accepted = data.get("accepted", {})
                alerts = data.get("alerts", {})
                mark = "" if events else "（心跳，仅资源）"
                print(
                    f"          ✓ 已上报{mark} 批次={batch_id[:12]} "
                    f"资源={accepted.get('resources')} 事件={accepted.get('events')} "
                    f"告警 新建={alerts.get('created', 0)}"
                )
                reporter.log_rejected(batch_id, body)
                last_sent = time.monotonic()
            else:
                print(f"          ✗ 上报失败（{status}）：{json.dumps(body, ensure_ascii=False)[:300]}")
                return 1

        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
