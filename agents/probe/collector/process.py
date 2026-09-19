"""进程采集器：只读解析 /proc。

产出 `process` 资源 + `process.crash` / `process.io_wait.high` 事件。
采集范围：容器内进程 + 直接运行于 VM 上的 AI 框架进程（避免系统进程噪音）。

- 资源带 `parentSourceId`：容器内进程挂到其所属容器（cgroup 解析容器 ID，
  见 `procutil.container_source_id`），为 B 的资源图提供 container→process 边。
- `process.io_wait.high` 采用**窗口化差分**（对齐 F-01 的"持续偏高"语义）：
  按 `/proc/<pid>/stat` 的 `delayacct_blkio_ticks` 差分占比超阈值且连续 N 轮才报，
  并带 `metrics`（阈值 + 窗口）。内核未开 CONFIG_TASK_DELAY_ACCT 时退化到
  state=`D` 瞬时检测（尽力而为，不重复报）。
"""

from __future__ import annotations

from typing import Any

from ..idgen import process_source_id
from ..model import Event, Resource, utc_now_ms
from ..sensitive import mask_cmdline
from .base import Collector
from .procutil import (
    container_source_id,
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

    # io_wait 窗口化阈值：io_wait 占比（0~1）与连续超阈轮数
    IO_WAIT_RATIO = 0.30
    IO_WAIT_CONSECUTIVE = 2

    def __init__(self) -> None:
        # pid -> (name, source_id, in_container)，用于崩溃检测
        self._known: dict[int, tuple[str, str, bool]] = {}
        # pid -> (utime, stime, blkio_ticks, consecutive_high)，用于 io_wait 窗口化
        self._iowait: dict[int, tuple[int, int, int, int]] = {}
        # pid -> bool：state=D 兜底路径下的上一轮是否已报，避免重复刷屏
        self._d_state_reported: dict[int, bool] = {}
        # sourceId -> 首次观察时间（firstSeenAt）
        self._first_seen: dict[str, str] = {}

    def collect(self) -> tuple[list[Resource], list[Event]]:
        resources: list[Resource] = []
        events: list[Event] = []
        seen: set[int] = set()
        now = utc_now_ms()

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
            # 容器内进程挂到所属容器资源下（container→process 边）
            parent_id = container_source_id(pid) if in_ctr else None
            first_seen = self._first_seen.setdefault(source_id, now)
            resources.append(
                Resource(
                    kind="process",
                    source_id=source_id,
                    name=name,
                    parent_source_id=parent_id,
                    status="running",
                    attributes=attrs,
                    first_seen_at=first_seen,
                    last_seen_at=now,
                )
            )
            self._known[pid] = (name, source_id, in_ctr)

            self._detect_io_wait(pid, stat, name, source_id, events)

        # 崩溃检测：上次已知、本轮消失、且为容器内进程
        for pid in list(self._known):
            if pid not in seen:
                name, source_id, in_ctr = self._known.pop(pid)
                self._iowait.pop(pid, None)
                self._d_state_reported.pop(pid, None)
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

    def _detect_io_wait(
        self,
        pid: int,
        stat: dict[str, str],
        name: str,
        source_id: str,
        events: list[Event],
    ) -> None:
        """io_wait 检测：优先窗口化差分，无 delayacct 时退化到 state=D 兜底。"""
        if "blkio_ticks" in stat:
            self._d_state_reported.pop(pid, None)
            self._detect_io_wait_windowed(pid, stat, name, source_id, events)
        elif stat["state"] == "D":
            # 兜底：内核未开 CONFIG_TASK_DELAY_ACCT，只有瞬时 state=D 可看。
            # 去重：连续 D 态不重复报（进程消失时状态被清，可再次报）。
            if not self._d_state_reported.get(pid):
                events.append(
                    Event(
                        type="process.io_wait.high",
                        severity="warning",
                        message=f"进程 {name} 处于不可中断睡眠（IO 等待，无 delayacct 指标）",
                        resource_kind="process",
                        resource_source_id=source_id,
                    )
                )
                self._d_state_reported[pid] = True
        else:
            self._d_state_reported.pop(pid, None)

    def _detect_io_wait_windowed(
        self,
        pid: int,
        stat: dict[str, str],
        name: str,
        source_id: str,
        events: list[Event],
    ) -> None:
        """按 delayacct_blkio_ticks 差分占比 + 连续轮数判定"持续偏高"。"""
        try:
            utime = int(stat["utime"])
            stime = int(stat["stime"])
            blkio = int(stat["blkio_ticks"])
        except (KeyError, ValueError):
            return

        prev = self._iowait.get(pid)
        if prev is None:
            self._iowait[pid] = (utime, stime, blkio, 0)
            return

        d_utime = utime - prev[0]
        d_stime = stime - prev[1]
        d_blkio = blkio - prev[2]
        total = d_utime + d_stime + d_blkio
        ratio = d_blkio / total if total > 0 else 0.0

        consecutive = prev[3] + 1 if ratio >= self.IO_WAIT_RATIO else 0
        self._iowait[pid] = (utime, stime, blkio, consecutive)

        if consecutive >= self.IO_WAIT_CONSECUTIVE:
            events.append(
                Event(
                    type="process.io_wait.high",
                    severity="warning",
                    message=(
                        f"进程 {name} I/O 等待持续偏高"
                        f"（{ratio:.1%} ≥ {self.IO_WAIT_RATIO:.0%}，连续 {consecutive} 轮）"
                    ),
                    resource_kind="process",
                    resource_source_id=source_id,
                    metrics={
                        "io_wait_pct": round(ratio * 100, 1),
                        "threshold_pct": round(self.IO_WAIT_RATIO * 100, 1),
                        "window_sec": round(self.interval * consecutive, 1),
                    },
                )
            )
