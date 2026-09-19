"""容器采集器：通过 Docker Unix socket 查询清单与事件。

产出 `container` 资源 + `container.oom_killed` / `container.restart` 事件。
若 Docker socket 不可用（无权限 / 非 Linux / 未挂载），静默降级返回空。
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import time
from typing import Any

from ..model import Event, Resource, utc_now_ms
from .base import Collector

log = logging.getLogger("probe.collector.container")

DOCKER_SOCKET = "/var/run/docker.sock"


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection 的 Unix socket 变体，用于访问 Docker socket。"""

    def __init__(self, sock_path: str):
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._sock_path)


class DockerClient:
    """极简 Docker API 客户端（仅清单 + 事件，零第三方依赖）。"""

    def __init__(self, sock_path: str = DOCKER_SOCKET):
        self._sock_path = sock_path

    def _get(self, path: str) -> Any | None:
        conn = _UnixHTTPConnection(self._sock_path)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 400:
                return None
            return json.loads(body)
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            return None
        finally:
            conn.close()

    def list_containers(self) -> list[dict[str, Any]]:
        data = self._get("/containers/json?all=1")
        return data if isinstance(data, list) else []

    def events(self, since: int, until: int) -> list[dict[str, Any]]:
        filters = {"type": ["container"], "event": ["oom", "die", "restart"]}
        qs = f"/events?since={since}&until={until}&filters={json.dumps(filters)}"
        data = self._get(qs)
        return data if isinstance(data, list) else []


class ContainerCollector(Collector):
    name = "container"
    interval = 10.0

    def __init__(self) -> None:
        self._client = DockerClient()
        self._last_event_time = int(time.time())
        # sourceId -> 首次观察时间（firstSeenAt）
        self._first_seen: dict[str, str] = {}

    def collect(self) -> tuple[list[Resource], list[Event]]:
        resources: list[Resource] = []
        events: list[Event] = []
        now = utc_now_ms()

        containers = self._client.list_containers()
        for c in containers:
            cid = c.get("Id", "")
            source_id = cid[:12]
            name = (c.get("Names") or ["unknown"])[0].lstrip("/")
            state = c.get("State", "unknown")
            status = "running" if state == "running" else (
                "error" if state in ("dead", "restarting") else "stopped"
            )
            first_seen = self._first_seen.setdefault(source_id, now)
            resources.append(
                Resource(
                    kind="container",
                    source_id=source_id,
                    name=name,
                    status=status,
                    attributes={
                        "image": c.get("Image", ""),
                        "runtime": "docker",
                        "restartCount": c.get("RestartCount", 0),
                    },
                    first_seen_at=first_seen,
                    last_seen_at=now,
                )
            )

        # 事件：轮询 since..until 窗口，取 oom / restart
        now = int(time.time())
        for ev in self._client.events(self._last_event_time, now):
            etype = ev.get("Action", "")
            eid = (ev.get("Actor") or {}).get("Attributes", {}).get("name", "")
            cid = ev.get("id", "")
            source_id = cid[:12]
            if etype == "oom":
                events.append(
                    Event(
                        type="container.oom_killed",
                        severity="critical",
                        message=f"容器 {eid or source_id} 被 OOM Killer 终止",
                        resource_kind="container",
                        resource_source_id=source_id,
                        raw=ev,
                    )
                )
            elif etype == "restart":
                events.append(
                    Event(
                        type="container.restart",
                        severity="warning",
                        message=f"容器 {eid or source_id} 发生重启",
                        resource_kind="container",
                        resource_source_id=source_id,
                        raw=ev,
                    )
                )
        self._last_event_time = now
        return resources, events
