"""批量上报：HTTP POST + 幂等 + 部分接受处理。

对齐契约 Q2（至少一次 + batchId 幂等）与 Q4（批量上限）。
上报结果三态：ok（2xx）/ retry（网络、5xx、429）/ rejected（4xx 客户端错误，不重试）。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .idgen import ulid
from .model import Event, Resource, utc_now_ms

log = logging.getLogger("probe.reporter")

OK = "ok"  # 2xx，成功（可能部分接受）
RETRY = "retry"  # 网络 / 5xx / 429，需重试（入缓冲）
REJECTED = "rejected"  # 4xx 客户端错误，不重试


def build_batch(
    agent_id: str,
    vm_id: str,
    resources: list[Resource],
    events: list[Event],
    batch_id: str | None = None,
    sent_at: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """组装一个上报批次，返回 (batchId, payload)。batchId 用 ULID，重试复用。

    `batch_id` / `sent_at` 可注入固定值：供样例载荷生成（`fixtures/generate_samples.py`）
    与测试使用 —— 契约测试需要确定性输入；缺省时自动生成（运行时行为不变）。
    """
    batch_id = batch_id or ulid()
    payload = {
        "agentId": agent_id,
        "vmId": vm_id,
        "agentVersion": __version__,
        "batchId": batch_id,
        "sentAt": sent_at or utc_now_ms(),
        "resources": [r.to_payload() for r in resources],
        "events": [e.to_payload() for e in events],
    }
    return batch_id, payload


class Reporter:
    """HTTP 批量上报器。"""

    def __init__(self, backend_url: str, token: str = ""):
        self.url = backend_url.rstrip("/") + "/api/v1/ingest/batch"
        self.token = token

    def send(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """发送一个批次，返回 (状态, 响应体)。"""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json", "X-API-Version": "v1"},
            method="POST",
        )
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return OK, _safe_json(resp.read())
        except urllib.error.HTTPError as exc:
            body = _safe_json(exc.read())
            if exc.code == 429 or exc.code >= 500:
                return RETRY, body
            return REJECTED, body
        except (urllib.error.URLError, TimeoutError, OSError):
            return RETRY, {}

    def log_rejected(self, batch_id: str, body: dict[str, Any]) -> None:
        """记录部分接受中被拒的条目（便于定位探针 bug）。"""
        rejected = body.get("data", {}).get("rejected", [])
        for item in rejected:
            log.warning(
                "批次 %s 条目被拒: index=%s reason=%s",
                batch_id,
                item.get("index"),
                item.get("reason"),
            )


def _safe_json(raw: bytes) -> dict[str, Any]:
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
