"""网络可达性采集器：对容器内 AI 服务的监听端口做 TCP 连通性探测。

产出 `container.network.unreachable` 事件（F-01 场景三根因候选）。

- 探测目标从容器内 AI 框架进程的 `--port` 派生（复用 ai_service 的识别逻辑），
  事件 `resourceRef` 指向该进程所属容器（cgroup 解析）。
- **只在「可达 → 不可达」转换时报告一次**（避免每次轮询都刷事件），
  恢复可达后重置，可再次报告；首次探测即不可达不报（服务可能尚未就绪）。
- 仅覆盖与探针同处宿主机网络（端口已映射到宿主）的情况；bridge 网络未
  映射端口的服务探测不到，属环境限制而非缺陷。
"""

from __future__ import annotations

import socket

from ..model import Event, Resource
from .base import Collector
from .procutil import (
    container_source_id,
    extract_port,
    in_container,
    is_framework,
    list_pids,
    read_cmdline,
)


class NetworkCollector(Collector):
    name = "network"
    interval = 15.0

    PROBE_HOST = "127.0.0.1"
    PROBE_TIMEOUT = 2.0

    def __init__(self) -> None:
        # (host, port) -> 上一次是否可达；None 表示首次探测尚未记录
        self._up: dict[tuple[str, int], bool] = {}

    def collect(self) -> tuple[list[Resource], list[Event]]:
        events: list[Event] = []
        targets: dict[tuple[str, int], str] = self._discover()

        for (host, port), cid in targets.items():
            up = self._probe(host, port)
            prev = self._up.get((host, port))
            self._up[(host, port)] = up
            if prev is True and not up:  # 可达 → 不可达
                events.append(
                    Event(
                        type="container.network.unreachable",
                        severity="error",
                        message=f"容器 {cid} 内 AI 服务 {host}:{port} 网络不可达",
                        resource_kind="container",
                        resource_source_id=cid,
                        metrics={
                            "target": f"{host}:{port}",
                            "timeout_ms": int(self.PROBE_TIMEOUT * 1000),
                            "attempts": 1,
                        },
                    )
                )

        # 清理已消失的目标（进程退出后不再探测）
        for t in list(self._up):
            if t not in targets:
                self._up.pop(t, None)

        return [], events

    def _discover(self) -> dict[tuple[str, int], str]:
        """发现探测目标：容器内 AI 框架进程的 (host, port) → 容器 sourceId。"""
        targets: dict[tuple[str, int], str] = {}
        for pid in list_pids():
            cmdline = read_cmdline(pid)
            if not is_framework(cmdline):
                continue
            # 事件主体是 container，只探测容器内服务
            if not in_container(pid):
                continue
            port = extract_port(cmdline)
            if port is None:
                continue
            cid = container_source_id(pid)
            if cid is None:
                continue
            targets.setdefault((self.PROBE_HOST, port), cid)
        return targets

    def _probe(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=self.PROBE_TIMEOUT):
                return True
        except OSError:
            return False
