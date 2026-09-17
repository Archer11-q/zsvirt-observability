"""进程采集器：只读解析 /proc。

产出 `process` 资源 + `process.crash` / `process.io_wait.high` 事件。
采集范围：容器内进程 + 直接运行于 VM 上的 AI 框架进程（避免系统进程噪音）。
"""

from __future__ import annotations

from typing import Any

from ..idgen import process_source_id
from ..model import Event, Resource
from ..sensitive import mask_cmdline
from .base import Collector
from .procutil import (
    in_container,
    is_framework,
    list_pids,
    read_cmdline,
    read_stat,
    rss_bytes,
)


class ProcessCollector(Collector):
    name = "process"
    interval = 5.0

    def __init__(self) -> None:
        # pid -> (name, source_id, in_container)，用于崩溃检测
        self._known: dict[int, tuple[str, str, bool]] = {}

    def collect(self) -> tuple[list[Resource], list[Event]]:
        resources: list[Resource] = []
        events: list[Event] = []
        seen: set[int] = set()

        for pid in list_pids():
            seen.add(pid)
            stat = read_stat(pid)
            if not stat:
                continue
            cmdline = read_cmdline(pid)
            in_ctr = in_container(pid)
            is_ai = is_framework(cmdline)

            # 采集范围：容器内进程 或 AI 框架进程
            if not (in_ctr or is_ai):
                continue

            source_id = process_source_id(stat["starttime"], pid)
            name = " ".join(cmdline[:2]) or stat["comm"]
            attrs: dict[str, Any] = {
                "pid": pid,
                "ppid": int(stat["ppid"]),
                "cmdline": mask_cmdline(cmdline),  # 前置掩码敏感参数
                "rssBytes": rss_bytes(pid),
            }
            resources.append(
                Resource(
                    kind="process",
                    source_id=source_id,
                    name=name,
                    status="running",
                    attributes=attrs,
                )
            )
            self._known[pid] = (name, source_id, in_ctr)

            # IO 等待检测：不可中断睡眠（state=D）视为 IO 等待
            if stat["state"] == "D":
                events.append(
                    Event(
                        type="process.io_wait.high",
                        severity="warning",
                        message=f"进程 {name} 处于不可中断睡眠（IO 等待）",
                        resource_kind="process",
                        resource_source_id=source_id,
                    )
                )

        # 崩溃检测：上次已知、本轮消失、且为容器内进程
        for pid in list(self._known):
            if pid not in seen:
                name, source_id, in_ctr = self._known.pop(pid)
                if in_ctr:
                    events.append(
                        Event(
                            type="process.crash",
                            severity="error",
                            message=f"进程 {name} 消失（可能异常退出）",
                            resource_kind="process",
                            resource_source_id=source_id,
                        )
                    )

        return resources, events
