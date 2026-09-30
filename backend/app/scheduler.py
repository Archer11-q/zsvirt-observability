"""同进程周期任务调度（`ARCHITECTURE.md` §4.4 的「同进程调度」选项）。

## 为什么需要这个模块

在此之前，`app/` 里**没有任何后台调度**：`main.py` 没有 lifespan，全库搜不到
`asyncio.create_task` / `BackgroundTasks` / 周期性调用。于是四个函数虽然实现完整、
测试通过，**在生产路径上从不执行**：

| 函数 | 本该做什么 | 曾经的后果 |
|---|---|---|
| `reconcile_all` | 告警**自动恢复**（D-084）+ 静默到期释放（D-083） | 告警永远停在 `firing`；静默永不释放 |
| `mark_stale` | 长时间无心跳的资源标 `stale` | `observability` 永远是 `active`，`staleness` 字段永远为空 |
| `mark_gone` | 极长时间无心跳标 `gone` | 同上 |

这不是"缺少新功能"，而是**已冻结的决策没有落地**——`DECISIONS.md` 里 D-083/D-084
写得很清楚，`API_CONTRACT.md` §4.2 也承诺了 `staleness` 给前端提示数据陈旧。
文档承诺了、代码实现了、测试通过了，但没人调用，于是三处全部失效。

## 为什么是同进程

`ARCHITECTURE.md:199` 曾把「同进程调度 / 独立 worker 进程」列为【待确认】。
选择同进程，理由是赛题「最小化环境能否复现」是独立评分项（15%）：
独立 worker 多一个常驻进程，就多一处演示现场起不来的可能，而收益在当前规模
（单机原型、三类场景）下不可测。

## 为什么不用 APScheduler

`ADR-0003` 的最小依赖纪律。这里要的只是"每隔 N 秒调一次同步函数"，
用 `asyncio` 标准库即可，不为此引入一个调度框架。

## 与请求路径隔离

- **自己的工作线程**：`asyncio.to_thread` 让同步 SQLAlchemy 不阻塞事件循环。
- **自己的 session**：`session_scope()` 每次新开、用完提交关闭，**不碰请求的 session**。
- **一次只跑一个 tick**：全局锁。否则陈旧标记与告警恢复可能在同一秒并发写同一批行。
- **错误隔离**：单个任务抛异常只记录并计数，不影响其他任务，也不让 uvicorn 退出。
  后台任务的职责是让系统自愈，它自己不能成为新的故障源。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.alerts.engine import reconcile_all
from app.config import Settings, get_settings
from app.db import session_scope
from app.graph.repository import mark_gone, mark_stale

log = logging.getLogger("crosslayer.scheduler")

__all__ = ["PeriodicTasks", "TASK_NAMES", "build_periodic_tasks", "run_maintenance_once"]

#: 任务名（也是 `/api/health` 里报告用的键）。
TASK_NAMES = ("alerts", "lifecycle")


# ================================================================ 单次执行


def run_maintenance_once(settings: Settings | None = None) -> dict[str, Any]:
    """执行一次维护：告警恢复 + 静默到期 + 资源生命周期标记。

    **顺序有语义**：先告警后资源。`mark_gone` 的判据是"已经是 stale"，
    所以 `mark_stale` 必须先于它；两者在同一次调用内按序执行，一轮就能从
    `active` 走到 `stale`（而不是要等两个周期）。

    返回各步骤的结果，便于测试断言与 `/api/health` 展示。
    """
    cfg = settings or get_settings()
    result: dict[str, Any] = {}

    with session_scope() as session:
        # 告警：自动恢复 + 静默到期。**必须 flush**（reconcile_all 内部已处理，
        # 见 `alerts/engine.py` 的说明：本项目关闭了 autoflush）。
        alerts = reconcile_all(session, recovery_after=cfg.alert_recovery_after)
        result["alerts"] = alerts

        # 资源生命周期。阈值为 0 表示**关闭**该步（见 config 的说明：
        # 演示数据的时间基准是固定的过去时刻，开启后会全体变 stale）。
        stale_after = cfg.resource_stale_after_sec
        gone_after = cfg.resource_gone_after_sec
        if stale_after > 0:
            result["stale"] = mark_stale(session, older_than_seconds=stale_after)
        else:
            result["stale"] = None
        if stale_after > 0 and gone_after > stale_after:
            result["gone"] = mark_gone(session, older_than_seconds=gone_after)
        else:
            result["gone"] = None

    return result


# ================================================================ 周期循环


@dataclass
class _TaskState:
    """单个任务的运行状态。`/api/health` 会读它。"""

    name: str
    interval_sec: int
    runs: int = 0
    failures: int = 0
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_duration_ms: int | None = None
    last_error: str | None = None
    last_result: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intervalSec": self.interval_sec,
            "runs": self.runs,
            "failures": self.failures,
            "lastStartedAt": self.last_started_at.isoformat() if self.last_started_at else None,
            "lastFinishedAt": (
                self.last_finished_at.isoformat() if self.last_finished_at else None
            ),
            "lastDurationMs": self.last_duration_ms,
            "lastError": self.last_error,
            "lastResult": self.last_result,
        }


class PeriodicTasks:
    """周期任务集合。

    生命周期由 FastAPI 的 lifespan 管理（`app/main.py`）：启动时开任务，
    关闭时**协作式取消**并等待收尾，避免 uvicorn 重启时留下半写状态的 session。
    """

    def __init__(
        self,
        *,
        interval_sec: int,
        settings: Settings | None = None,
    ) -> None:
        if interval_sec < 1:
            raise ValueError("interval_sec must be >= 1")
        self.interval_sec = interval_sec
        self._settings = settings
        self._states: dict[str, _TaskState] = {
            "alerts": _TaskState(name="alerts", interval_sec=interval_sec),
            "lifecycle": _TaskState(name="lifecycle", interval_sec=interval_sec),
        }
        self._tasks: list[asyncio.Task[None]] = []
        #: 启停互斥（`start` / `stop` 之间）。
        self._guard = asyncio.Lock()
        #: 轮次互斥：一次只允许一个 tick 在跑。告警恢复与资源标记会写同一批行，
        #: 并发没有意义。**与 `_guard` 分开**：`_tick` 持这把锁时不能再取
        #: `_guard`，否则两轮重叠会自锁。
        self._serialize = asyncio.Lock()
        self._stopping = asyncio.Event()

    # ---- 对外 ----

    @property
    def states(self) -> dict[str, dict[str, Any]]:
        return {name: state.as_dict() for name, state in self._states.items()}

    async def start(self) -> None:
        """启动循环。

        **立刻跑第一轮**（见 `_loop`），但 `start()` 本身只负责调度、不等待结果 ——
        调用方（lifespan）不该被后台任务的耗时拖住启动。测试若需要断言第一轮的结果，
        应当轮询 `states`（见 `tests/test_scheduler.py` 的 `_wait_for`）。
        """
        async with self._guard:
            if self._tasks:
                return
            self._stopping.clear()
            self._tasks = [
                asyncio.create_task(self._loop("alerts", self._run_alerts), name="cl-alerts"),
                asyncio.create_task(
                    self._loop("lifecycle", self._run_lifecycle), name="cl-lifecycle"
                ),
            ]
        log.info("周期任务已启动：interval=%ss", self.interval_sec)

    async def stop(self) -> None:
        async with self._guard:
            tasks = self._tasks
            if not tasks:
                return
            self._stopping.set()
            for task in tasks:
                task.cancel()
        # `return_exceptions=True`：取消会抛 CancelledError，那不是错误。
        # 在锁外等待：任务收尾时可能还要更新 states，持锁等待会自锁。
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self._guard:
            if self._tasks is tasks:
                self._tasks = []
        log.info("周期任务已停止")

    # ---- 内部 ----

    async def _loop(self, name: str, body: Callable[[], dict[str, Any]]) -> None:
        """固定间隔循环。

        **先跑再睡**：服务刚起来时数据库里可能已经有需要恢复的告警
        （例如上次进程被强杀），等一个完整周期会让这段时间的状态是错的。
        """
        first = True
        while not self._stopping.is_set():
            if not first:
                try:
                    # 用 wait_for 而不是 sleep：停止时能被立刻唤醒，不必等满一个周期
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_sec)
                    break  # 收到停止信号
                except TimeoutError:
                    pass
            first = False
            await self._tick(name, body)

    async def _tick(self, name: str, body: Callable[[], dict[str, Any]]) -> None:
        """跑一轮，并记录状态。

        **不持 `_guard`**：锁的职责是"不让两轮同时跑"，而 `_run_alerts`
        内部还要取同一把锁 —— 持锁调用 body 会自锁（两轮重叠时直接死等）。
        这里的互斥由 `_serialize` 保证。
        """
        async with self._serialize:
            state = self._states[name]
            state.runs += 1
            state.last_started_at = datetime.now(UTC)
            started = time.perf_counter()
            try:
                # 同步 SQLAlchemy → 放线程里，不阻塞事件循环
                result = await asyncio.to_thread(body)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务不能成为新的故障源
                state.failures += 1
                state.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("周期任务 %s 失败：%s", name, state.last_error, exc_info=True)
            else:
                state.last_result = result
                state.last_error = None
            finally:
                state.last_finished_at = datetime.now(UTC)
                state.last_duration_ms = int((time.perf_counter() - started) * 1000)

    def _run_alerts(self) -> dict[str, Any]:
        """告警维护：自动恢复 + 静默到期（**同步**，由 `_tick` 放线程池跑）。

        与资源生命周期**分开跑**，而不是合成一个任务：两者的失败原因不同
        （数据库 vs 逻辑），合在一起会让"哪个坏了"变得看不清。
        """
        cfg = self._settings or get_settings()
        with session_scope() as session:
            return {"alerts": reconcile_all(session, recovery_after=cfg.alert_recovery_after)}

    def _run_lifecycle(self) -> dict[str, Any]:
        """资源生命周期：`stale` → `gone`。阈值为 0 时是**空操作**。"""
        cfg = self._settings or get_settings()
        stale_after = cfg.resource_stale_after_sec
        if stale_after <= 0:
            # 显式关闭。返回 None 而不是 0，让"没做"与"做了但没影响任何行"可区分。
            return {"stale": None, "gone": None, "disabled": True}
        with session_scope() as session:
            stale = mark_stale(session, older_than_seconds=stale_after)
            gone = (
                mark_gone(session, older_than_seconds=cfg.resource_gone_after_sec)
                if cfg.resource_gone_after_sec > stale_after
                else None
            )
        return {"stale": stale, "gone": gone}

    def _recovery_after(self) -> Any:
        return (self._settings or get_settings()).alert_recovery_after


def build_periodic_tasks(settings: Settings | None = None) -> PeriodicTasks | None:
    """按配置构造调度器。`SCHEDULER_ENABLED=false` 时返回 `None`。

    关闭开关是有用的：本地跑演示、或者只想手动触发一次时，不希望后台
    悄悄改库（例如把演示数据标成 `stale`）。
    """
    cfg = settings or get_settings()
    if not cfg.scheduler_enabled:
        log.info("周期任务已按配置关闭（SCHEDULER_ENABLED=false）")
        return None
    return PeriodicTasks(interval_sec=cfg.scheduler_interval_sec, settings=cfg)
