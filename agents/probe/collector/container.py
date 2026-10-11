"""容器采集器：通过 Docker Unix socket 查询清单与事件。

产出 `container` 资源 + `container.oom_killed` / `container.restart` 事件。
若 Docker socket 不可用（无权限 / 非 Linux / 未挂载），静默降级返回空。

历史缺陷（2026-09-30 成员 B 缺陷同步单，已修复）：
- `/events` 的 `filters` 未做 URL 编码（json.dumps 默认带空格 → InvalidURL 被吞）。
- `/events` 返回 NDJSON 流，却用 `json.loads` 解析整段（必然 JSONDecodeError 被吞）。
- 事件容器 ID 在 `Actor.ID`，却读顶层 `id`（source_id 恒为空串）。
- `restartCount` 在 `/containers/<id>/json`（inspect），却读 `/containers/json`（恒 0）。
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import time
import urllib.parse
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
    """极简 Docker API 客户端（清单 + 事件 + inspect，零第三方依赖）。

    两种响应形状分开处理：`/containers/json` 与 `/containers/<id>/json` 是
    单个 JSON 文档（`_get`），`/events` 是换行分隔的 NDJSON 流（`_get_stream`）。
    """

    def __init__(self, sock_path: str = DOCKER_SOCKET):
        self._sock_path = sock_path

    def _request(self, path: str) -> str | None:
        """底层 GET，返回响应体文本；任何网络/HTTP 错误返回 None。"""
        conn = _UnixHTTPConnection(self._sock_path)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            return None if resp.status >= 400 else body
        except (OSError, http.client.HTTPException):
            return None
        finally:
            conn.close()

    def _get(self, path: str) -> Any | None:
        """单个 JSON 文档（`/containers/json`、`/containers/<id>/json`）。"""
        body = self._request(path)
        if body is None:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def _get_stream(self, path: str) -> list[dict[str, Any]]:
        """NDJSON 流（`/events`）。逐行解析，单行坏掉不丢整批。"""
        body = self._request(path)
        if body is None:
            return []
        out: list[dict[str, Any]] = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def list_containers(self) -> list[dict[str, Any]]:
        data = self._get("/containers/json?all=1")
        return data if isinstance(data, list) else []

    def inspect(self, container_id: str) -> dict[str, Any] | None:
        data = self._get(f"/containers/{container_id}/json")
        return data if isinstance(data, dict) else None

    def events(self, since: int, until: int) -> list[dict[str, Any]]:
        filters = {"type": ["container"], "event": ["oom", "die", "restart"]}
        query = urllib.parse.urlencode(
            {
                "since": since,
                "until": until,
                # separators 收紧去空格 + urlencode 编码引号/方括号，缺一不可
                "filters": json.dumps(filters, separators=(",", ":")),
            }
        )
        return self._get_stream(f"/events?{query}")


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
            attributes: dict[str, Any] = {
                "image": c.get("Image", ""),
                "runtime": "docker",
            }
            # restartCount 只在 inspect（/containers/<id>/json）；失败或缺失时不报
            # 错值（缺字段比"重启 0 次"更诚实 —— 缺陷 4）。
            info = self._client.inspect(cid) if cid else None
            if info and "RestartCount" in info:
                attributes["restartCount"] = info["RestartCount"]
            first_seen = self._first_seen.setdefault(source_id, now)
            resources.append(
                Resource(
                    kind="container",
                    source_id=source_id,
                    name=name,
                    status=status,
                    attributes=attributes,
                    first_seen_at=first_seen,
                    last_seen_at=now,
                )
            )

        # 事件：轮询 since..until 窗口，取 oom / restart
        now = int(time.time())
        for ev in self._client.events(self._last_event_time, now):
            etype = ev.get("Action", "")
            actor = ev.get("Actor") or {}
            eid = actor.get("Attributes", {}).get("name", "")
            # 容器 ID 在 Actor.ID，不在顶层 id（缺陷 3）
            cid = actor.get("ID", "")
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
