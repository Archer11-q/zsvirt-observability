"""标识生成：ULID（batchId）与 sourceId 规则。

- `batchId` 用 ULID（契约 D-055），时间单调有序，重试复用同一值以实现幂等。
- `sourceId` 规则见 DATA_MODEL D-031：container=容器ID前12位、process=starttime+pid、
  ai_service=服务名+端口、agent/task=ULID。
"""

from __future__ import annotations

import os
import time

# Crockford Base32（ULID 标准字符集，去掉了易混淆的 I/L/O/U）
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """生成 ULID：48 位毫秒时间戳 + 80 位随机数，Crockford Base32 编码。"""
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(timestamp, 10) + _encode(randomness, 16)


def _encode(value: int, length: int) -> str:
    chars = [""] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _B32[value & 0x1F]
        value >>= 5
    return "".join(chars)


def process_source_id(starttime: str, pid: int) -> str:
    """进程 sourceId = starttime + pid，避免 pid 复用导致冲突（D-031）。"""
    return f"{starttime}:{pid}"


def ai_service_source_id(service_name: str, port: int) -> str:
    """AI 服务 sourceId = 服务名 + 监听端口（D-031）。"""
    return f"{service_name}:{port}"
