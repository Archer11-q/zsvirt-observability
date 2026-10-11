"""从**真实容器运行时**采集故障事件并上报到 Crosslayer（赛题交付要求 e）。

## 这个脚本存在的理由

`deploy/inject_fault.sh` 走的是 `backend/app/zsvirt/scenarios.py` ——
它按已冻结契约**构造**上报载荷。那条路对演示很方便，但它的证据是**人工模拟**的
（`source=simulated`）。赛题交付要求 e 要的是"故障场景与**证据**"，
而"证据"的说服力取决于它是不是真的发生过。

这个脚本走另一条路：**真的去问容器运行时**。

    docker 里真的跑着推理服务 → 真的被内存限额压死 → 真的 OOMKilled
    → 成员 A 的探针采集器读到这件事 → 按契约上报

## 复用成员 A 的代码

采集与映射**一行都没改**：直接用 `agents/probe/collector/container.py`
的 `ContainerCollector` 与 `DockerClient`，上报用 `agents/probe/reporter.py`
的 `build_batch` 与 `Reporter`。

> **2026-09-30 更新**：本脚本早先还带着三处"绕过成员 A 采集器缺陷"的补丁
> （`/events` 的 URL 编码、NDJSON 解析、`Actor.ID` 取容器 id）。
> 那些缺陷已由成员 A 在 `fb61f4d` 修好并**实测通过**（事件采得到、`source_id`
> 非空且正确、`restartCount` 为真值），**三处补丁已全部删除**，
> 现在跑的就是 A 的原始实现。过程记录见 `docs/PROBE_DEFECT_SYNC.md`。

本文件现在只做三件 A 的采集器**设计上不做**的事（都不是缺陷）：

| 本脚本的动作 | 为什么 |
|---|---|
| 按名字前缀收敛观测范围（`--filter-name`） | A 的 `ContainerCollector` 会列出**整台机器所有容器**（实测把无关的 grafana / mysql 一起报上来）。探针在 ZSvirt 虚拟机里这是对的（那台 VM 上就该只有被测负载），但开发机上会把资源图冲淡 |
| 补一条宿主 VM 资源当父节点 | A 的采集器**不声称 VM 权威**（设计如此）。没有父节点，容器在图上就是孤立的 |
| 补 `exitCode` / `oomKilledCount` / `container_memory_limit_bytes` | 这三个值容器运行时**真实暴露**（`State.ExitCode` / `RestartCount` / `HostConfig.Memory`），但 A 的采集器目前不上报事件的 `metrics`。已登记为 A 的后续任务（`docs/REQUIREMENTS_GAP.md`）；用 `--no-enrich` 可关掉以观察差别 |

## 诚实边界（务必连同本文件一起读）

| 事实 | 说明 |
|---|---|
| 容器、OOM、退出码、重启次数 | **真实观测**，来自容器运行时 API |
| 证据 `source` | `probe`（**真实采集**）。不是 `simulated` |
| 宿主 VM 资源 | 由本脚本补出，代表"容器实际所在的 Linux VM"。**它不是 ZSvirt 管理的虚拟机**：资源 `name` 写明，`vmId` 用 `devbox00-...` 一眼可辨的替身串，**不伪造 ZSvirt UUID** |
| 跨层链的上半段 | 缺失。真实部署里 `vGPU → VM → 容器` 由 ZSvirt 资产 API + 探针 `vmId` 共同锚定，而 `vmId` 的注入方式仍在等命题方答复（外部阻塞 **X-09**） |

用法：

    python collect_real_events.py                # 采集一次并上报
    python collect_real_events.py --watch 5      # 每 5 秒采集一次（演示时开着）
    python collect_real_events.py --dry-run      # 只打印载荷，不上报
    python collect_real_events.py --no-enrich    # 不补指标（观察 A 的原始输出）

环境变量：`BACKEND`（默认 `http://127.0.0.1:8080`）、`TOKEN`。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
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


# ================================================================ 传输层


class CliDockerClient(DockerClient):
    """把 A 用到的三个 Engine API 路径翻译成 `docker` 命令行。

    **只在 `/var/run/docker.sock` 不存在时才构造**（见 `make_client`）。
    真实部署（以及本机 Docker Desktop 的 WSL 集成正常时）走的是 socket 那条路，
    这个类根本不会被用到。

    实现方式是**只覆盖 `_request`**（返回响应体文本），让 A 的
    `_get` / `_get_stream` / `events` / `list_containers` 全部照原样跑 ——
    包括它对"单个 JSON 文档"与"NDJSON 流"的区分。

    ⚠️ 这一点是有意的：早先这里覆盖的是 `_get`，在 A 修好 NDJSON 之后立刻失效
    （`events()` 不再经过 `_get`），表现为"什么都采不到"。**替身层贴得越浅，
    上游改动时越不容易悄悄坏掉。**
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

    # ---- 只覆盖这一层：把 API 路径变成响应体文本 ----

    def _request(self, path: str) -> str | None:  # type: ignore[override]
        if path.startswith("/containers/json"):
            # 必须返回 JSON **数组**：A 的 `list_containers` 直接把它当列表用。
            return json.dumps(self._inspect_all())

        match = re.match(r"^/containers/([0-9a-zA-Z]+)/json$", path)
        if match:
            data = self._inspect_all([match.group(1)])
            return json.dumps(data[0]) if data else None

        if path.startswith("/events"):
            return self._events_ndjson(path)

        return None

    # ---- 命令行翻译 ----

    def _inspect_all(self, ids: list[str] | None = None) -> list[dict[str, Any]]:
        if ids is None:
            listing = self._run(["ps", "-aq", "--no-trunc"])
            ids = [line.strip() for line in (listing or "").splitlines() if line.strip()]
        if not ids:
            return []
        raw = self._run(["inspect", *ids])
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def _events_ndjson(self, path: str) -> str:
        query = path.split("?", 1)[1] if "?" in path else ""
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
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
        # 事件过滤器逐个透传：多取不会改变 A 的行为，少取会让 oom/restart 漏掉。
        for event in ("oom", "die", "restart"):
            args.extend(["--filter", f"event={event}"])
        return self._run(args) or ""


def make_client() -> tuple[DockerClient, str]:
    """有 unix socket 就用成员 A 的原件，否则退回命令行替身。"""
    if os.path.exists(DOCKER_SOCKET):
        return DockerClient(), "原生 unix socket（成员 A 的原始实现）"

    for binary in ("docker", "docker.exe"):
        found = shutil.which(binary)
        if found:
            return CliDockerClient(found), f"命令行替身（{found}）"
    raise SystemExit(
        "找不到可用的 Docker 通道：既没有 /var/run/docker.sock，也没有 docker 可执行文件。"
    )


# ================================================================ 指标补齐


def inspect_containers(client: DockerClient, container_ids: set[str]) -> dict[str, dict[str, Any]]:
    """按 id 取 inspect 结果（补齐 A 尚未上报的指标）。"""
    result: dict[str, dict[str, Any]] = {}
    for container_id in container_ids:
        if not container_id:
            continue
        data = client.inspect(container_id)
        if isinstance(data, dict) and data.get("Id"):
            result[data["Id"]] = data
    return result


def _match_container(
    inspected: dict[str, dict[str, Any]], short_id: str
) -> dict[str, Any] | None:
    if not short_id:
        return None
    for full_id, info in inspected.items():
        if full_id.startswith(short_id) or short_id.startswith(full_id[:12]):
            return info
    return None


def enrich_events(events: list[Any], inspected: dict[str, dict[str, Any]]) -> list[Any]:
    """给事件补上运行时**真实暴露**、而 A 的采集器尚未上报的指标。

    只补能从 API 读到的值，读不到就**不写这个键**（`app/` 的 ZWatch 渠道也是
    这个原则：缺失与"值为 0"在库里必须长得不一样）。

    ⚠️ 这是**替身层**的行为。对应的后续任务已登记：
    A 的 `ContainerCollector` 应当自己产出这些指标（`docs/REQUIREMENTS_GAP.md`）。
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
            metrics["oomKilledCount"] = int(info.get("RestartCount") or 0) + 1

        if metrics:
            event.metrics = metrics
        # 渠道标注写进 `raw.origin`（D-107）：真实采集，不是 simulated。
        raw = dict(event.raw or {})
        raw.setdefault("origin", "probe")
        raw["observedVia"] = "container-runtime-api"
        event.raw = raw
    return events


# ================================================================ 主流程


def collect_once(
    collector: ContainerCollector,
    *,
    with_host_vm: bool,
    filter_name: str = "",
    enrich: bool = True,
) -> tuple[list[Resource], list[Any]]:
    resources, events = collector.collect()

    # ---- 观测范围过滤 ----
    #
    # A 的 `ContainerCollector` 会列出**整台机器上的所有容器**（实测：连无关的
    # grafana / mysql 一起报上来）。探针部署在 ZSvirt 虚拟机里时这是对的 ——
    # 那台 VM 上就该只有被测负载。但开发机上混着一堆别的容器，全报上去会把
    # 资源图与告警列表冲淡，演示时评委无法判断哪条才是重点。
    #
    # 真实部署用标签选择器更合适；这里用名字前缀是因为 A 的采集器不传 filters，
    # 替身层不该为了过滤去改它的调用形状。
    if filter_name:
        resources = [
            r for r in resources if r.kind != "container" or r.name.startswith(filter_name)
        ]
        kept = {r.source_id for r in resources if r.kind == "container"}
        events = [e for e in events if e.resource_source_id in kept]

    if with_host_vm:
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

    if enrich:
        client = collector._client  # noqa: SLF001 - 替身层需要底层客户端
        wanted = {r.source_id for r in resources if r.kind == "container"}
        wanted |= {e.resource_source_id for e in events if e.resource_source_id}
        events = enrich_events(events, inspect_containers(client, wanted))

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
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="不补指标（观察成员 A 采集器的原始输出，用于确认后续任务是否必要）",
    )
    args = parser.parse_args()

    backend = os.environ.get("BACKEND", "http://127.0.0.1:8080")
    token = os.environ.get("TOKEN", "")

    client, transport = make_client()
    collector = ContainerCollector()
    collector._client = client  # noqa: SLF001 - 注入传输层，采集逻辑不变

    print(f"采集通道：{transport}")
    print(f"上报地址：{backend}/api/v1/ingest/batch")
    print(f"指标补齐：{'关闭（--no-enrich）' if args.no_enrich else '开启'}")
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
            collector,
            with_host_vm=not args.no_host_vm,
            filter_name=args.filter_name,
            enrich=not args.no_enrich,
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
            _, payload = build_batch(AGENT_ID, STANDIN_VM_ID, resources, events)
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
