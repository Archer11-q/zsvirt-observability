"""探针入口：装配 + 主循环。运行：`python -m probe`。

职责：启动采集器线程 → 主循环聚合 → 脱敏/组装 → 上报（失败入 SQLite 缓冲补传）。
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import threading
import time

from . import __version__
from . import config
from .buffer import Buffer
from .collector import DEFAULT_COLLECTORS
from .model import Event, Resource
from .reporter import OK, RETRY, REJECTED, Reporter, build_batch

log = logging.getLogger("probe")


def _collector_loop(collector, out_queue: "queue.Queue") -> None:
    """采集器线程：周期采集，结果放入队列；异常不拖垮探针。"""
    while True:
        try:
            resources, events = collector.collect()
            for r in resources:
                out_queue.put(("resource", r))
            for e in events:
                out_queue.put(("event", e))
        except Exception:  # noqa: BLE001 —— 单采集器异常不影响整体
            log.exception("采集器 %s 异常", collector.name)
        time.sleep(collector.interval)


def _flush_buffer(buffer: Buffer, reporter: Reporter) -> None:
    """优先补传缓冲里的旧批次（FIFO，断网恢复后按序重发）。"""
    for batch_id, payload in buffer.peek_all():
        status, body = reporter.send(payload)
        if status == OK:
            buffer.ack(batch_id)
            reporter.log_rejected(batch_id, body)
        elif status == REJECTED:
            log.warning("缓冲批次 %s 被拒（4xx），丢弃", batch_id)
            buffer.ack(batch_id)
        else:  # RETRY：网络未恢复，停止补传避免空转
            buffer.inc_attempts(batch_id)
            break


def _report_loop(
    cfg: config.Config,
    buffer: Buffer,
    reporter: Reporter,
    in_queue: "queue.Queue",
    stop: threading.Event,
) -> None:
    resources: list[Resource] = []
    events: list[Event] = []
    last_flush = time.time()

    while not stop.is_set():
        _flush_buffer(buffer, reporter)

        # 从队列取数据（非阻塞，尽量清空）
        try:
            while True:
                kind, item = in_queue.get_nowait()
                if kind == "resource":
                    resources.append(item)
                else:
                    events.append(item)
        except queue.Empty:
            pass

        # 上报触发条件：数量达上限 / 到 5s / 有事件且到 1s
        now = time.time()
        has_event = bool(events)
        flush = (
            len(events) >= cfg.batch_max_events
            or len(resources) >= cfg.batch_max_resources
            or now - last_flush >= cfg.batch_sec
            or (has_event and now - last_flush >= cfg.event_flush_sec)
        )
        if not (flush and (resources or events)):
            time.sleep(0.1)
            continue

        # 截取一个符合上限的批次，剩余留待下一批
        batch_events = events[: cfg.batch_max_events]
        batch_resources = resources[: cfg.batch_max_resources]

        # 1MB 上限（契约 Q4）：超限则减半 events，减掉的后半自动留待下一批
        batch_id, payload = build_batch(
            cfg.agent_id, cfg.vm_id, batch_resources, batch_events
        )
        while (
            len(json.dumps(payload).encode("utf-8")) > cfg.batch_max_bytes
            and len(batch_events) > 1
        ):
            batch_events = batch_events[: len(batch_events) // 2]
            batch_id, payload = build_batch(
                cfg.agent_id, cfg.vm_id, batch_resources, batch_events
            )

        del events[: len(batch_events)]
        del resources[: len(batch_resources)]
        status, body = reporter.send(payload)
        last_flush = time.time()

        if status == OK:
            reporter.log_rejected(batch_id, body)
        elif status == RETRY:
            buffer.put(batch_id, payload)
            log.warning("上报失败，批次 %s 入缓冲待重试", batch_id)
        else:  # REJECTED
            log.warning("批次 %s 被拒（4xx），丢弃", batch_id)


def main() -> None:
    cfg = config.load()
    config.validate(cfg)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("探针启动 v%s agentId=%s vmId=%s", __version__, cfg.agent_id, cfg.vm_id)

    stop = threading.Event()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass  # 非主线程环境忽略

    buffer = Buffer(cfg.buffer_path, cfg.buffer_max_mb * 1024 * 1024)
    reporter = Reporter(cfg.backend_url, cfg.probe_token)
    in_queue: "queue.Queue" = queue.Queue()

    for cls in DEFAULT_COLLECTORS:
        threading.Thread(
            target=_collector_loop, args=(cls(), in_queue), daemon=True
        ).start()

    try:
        _report_loop(cfg, buffer, reporter, in_queue, stop)
    finally:
        buffer.close()
        log.info("探针退出，缓冲中待补传批次 %d 批", buffer.size())


if __name__ == "__main__":
    main()
