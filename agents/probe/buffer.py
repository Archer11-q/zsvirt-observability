"""断网缓冲：SQLite FIFO（对齐 Q6）。

批次上报失败时落盘，恢复后按序补传；超限丢最旧并计数（丢弃数随后续批次上报）。
WAL 模式保证崩溃安全。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger("probe.buffer")


class Buffer:
    """基于 SQLite 的 FIFO 持久化缓冲。"""

    def __init__(self, path: str, max_bytes: int):
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pending ("
            " batch_id TEXT PRIMARY KEY,"
            " payload   TEXT NOT NULL,"
            " created_at INTEGER NOT NULL,"
            " attempts  INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        self._conn.commit()
        self.dropped = 0  # 因超限被丢弃的批次数（随后续批次上报）

    def put(self, batch_id: str, payload: dict[str, Any]) -> None:
        """写入一个待上报批次（幂等：同 batch_id 覆盖）。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending "
                "(batch_id, payload, created_at, attempts) VALUES (?, ?, ?, 0)",
                (batch_id, json.dumps(payload), int(time.time() * 1000)),
            )
            self._conn.commit()
            self._enforce_limit()

    def peek_all(self) -> list[tuple[str, dict[str, Any]]]:
        """按 FIFO 顺序返回所有待上报批次。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT batch_id, payload FROM pending ORDER BY created_at ASC"
            ).fetchall()
            return [(row[0], json.loads(row[1])) for row in rows]

    def ack(self, batch_id: str) -> None:
        """上报成功后移除。"""
        with self._lock:
            self._conn.execute("DELETE FROM pending WHERE batch_id = ?", (batch_id,))
            self._conn.commit()

    def inc_attempts(self, batch_id: str) -> None:
        """记录一次失败重试。"""
        with self._lock:
            self._conn.execute(
                "UPDATE pending SET attempts = attempts + 1 WHERE batch_id = ?",
                (batch_id,),
            )
            self._conn.commit()

    def size(self) -> int:
        """待上报批次数。"""
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def _enforce_limit(self) -> None:
        """总字节超限时丢弃最旧的批次。"""
        total = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM pending"
        ).fetchone()[0]
        while total > self._max_bytes:
            row = self._conn.execute(
                "SELECT batch_id FROM pending ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                break
            self._conn.execute("DELETE FROM pending WHERE batch_id = ?", (row[0],))
            self.dropped += 1
            total = self._conn.execute(
                "SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM pending"
            ).fetchone()[0]
            log.warning("缓冲超限，丢弃最旧批次 %s（已累计丢弃 %d 批）", row[0], self.dropped)
        self._conn.commit()
