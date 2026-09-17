"""上报限流与运行指标（进程内）。

**为什么不做成分布式限流**：`ADR-0003` 已确认最小依赖（不引入 Redis）。
当前规模是单进程服务 + 单台被观测主机，进程内计数足够，且没有额外运维成本。
未来若横向扩展，需连同 `ADR-0003` 一起重新评审。

**为什么需要它**：

1. 成员 A 的 Q4 明确要求 B 给出 `429` 的触发阈值与 `Retry-After` 语义；
2. `ADR-0003` 要求自行处理背压 —— 探针上报速率超过处理能力时，
   必须有明确的限流策略，不能无限缓冲导致 OOM；
3. 时钟漂移（A 的 Q3）与未识别资源计数（A 的 Q1）需要暴露为健康指标。

实现是滑动窗口计数：只保留窗口内的时间戳，避免长期累积内存。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0
    reason: str = ""


@dataclass
class _Window:
    """滑动窗口内的事件时间戳。"""

    timestamps: deque[datetime] = field(default_factory=lambda: deque())

    def prune(self, cutoff: datetime) -> None:
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()

    def count(self, cutoff: datetime) -> int:
        self.prune(cutoff)
        return len(self.timestamps)

    def add(self, moment: datetime) -> None:
        self.timestamps.append(moment)


class BatchRateLimiter:
    """按批次计的滑动窗口限流器。

    默认：每 60 秒最多 120 批（即平均 2 批/秒）。

    为什么按**批**而不是按事件：探针每 5 秒一批（A 的 Q4），
    120 批/分钟意味着即使探针把间隔缩到 0.5 秒也仍有余量，
    同时在探针异常紧循环时能及时挡住。
    """

    #: 构造时的默认参数。`reset()` 用它们恢复，避免测试改动阈值后污染后续用例。
    DEFAULT_MAX_BATCHES = 120
    DEFAULT_WINDOW_SECONDS = 60

    def __init__(
        self, max_batches: int = DEFAULT_MAX_BATCHES, window_seconds: int = DEFAULT_WINDOW_SECONDS
    ) -> None:
        self.max_batches = max_batches
        self.window_seconds = window_seconds
        self._window = _Window()
        self._lock = threading.Lock()

    def reset(self) -> None:
        """回到初始状态：清空窗口**并恢复阈值**（保持对象身份）。

        必须一并恢复 `max_batches` / `window_seconds` —— 否则测试里为了快速触发
        限流而临时调小的阈值会残留，导致后续用例的请求被意外 429。
        这个坑真实发生过：四个健康/上限测试因此失败。
        """
        with self._lock:
            self._window.timestamps.clear()
        self.max_batches = self.DEFAULT_MAX_BATCHES
        self.window_seconds = self.DEFAULT_WINDOW_SECONDS

    def check_and_record(self, now: datetime | None = None) -> RateLimitDecision:
        """检查是否放行；放行则记录本次。

        返回被拒时应给出的 `Retry-After`：窗口内最早那次记录的过期时间。
        """
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(seconds=self.window_seconds)

        with self._lock:
            current = self._window.count(cutoff)
            if current >= self.max_batches:
                # 最早那次过期后即可再试
                oldest = self._window.timestamps[0]
                retry_after = max(
                    1,
                    int((oldest + timedelta(seconds=self.window_seconds) - moment).total_seconds())
                    + 1,
                )
                return RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=retry_after,
                    reason=(
                        f"rate limit exceeded: {current} batches in "
                        f"{self.window_seconds}s (max {self.max_batches})"
                    ),
                )
            self._window.add(moment)
            return RateLimitDecision(allowed=True)

    def current_count(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(seconds=self.window_seconds)
        with self._lock:
            return self._window.count(cutoff)


@dataclass
class IngestMetrics:
    """接入运行指标（暴露给 `/api/health`）。

    这些数字是**链路完整性**的可观测信号（`API_CONTRACT.md` §4.8）：

    - `clock_drift_ms`：探针与 B 的时钟偏移。超阈值说明跨层关联的时间窗会失效。
    - `unresolved_events`：事件引用的资源尚未上报的次数（占位节点数）。
      持续增长说明探针上报顺序有问题，或资源被 ZSvirt 删除后仍被引用。
    - `discarded`：探针侧因缓冲超限丢弃的事件数（A 的 Q6 会随后续批次上报）。
    - `rejected_items`：Schema 校验失败的条目数。突增说明探针版本与契约不符。
    - `unknown_event_types`：出现非枚举事件类型的次数。
      说明有人在扩事件类型但没走契约变更流程。
    """

    def __init__(self, drift_warn_ms: int = 5000) -> None:
        self.drift_warn_ms = drift_warn_ms
        self.reset()

    def reset(self) -> None:
        """**原地**清零全部计数（保持对象身份）。

        为什么必须原地重置而不能重新赋值一个新对象：本模块导出的是
        **模块级单例**，`app.api.health` 等模块在 import 时就把引用绑定了。
        若 `reset_metrics()` 用 `global metrics; metrics = IngestMetrics()`
        重新绑定，那些模块仍指向**旧对象**，于是测试里的重置与注入全部失效 ——
        症状是"健康端点永远读到默认值"。
        """
        self.clock_drift_ms: int = 0
        self.clock_drift_samples: int = 0
        self.unresolved_events: int = 0
        self.discarded: int = 0
        self.rejected_items: int = 0
        self.unknown_event_types: int = 0
        self.batches_accepted: int = 0
        self.batches_duplicate: int = 0
        self.batches_rate_limited: int = 0
        self.last_batch_at: datetime | None = None

    def observe_clock_drift(self, received_at: datetime, sent_at: datetime) -> int:
        """记录一次时钟漂移观测，返回本次偏移（毫秒）。

        用**指数滑动平均**而不是直接覆盖：单次网络抖动会造成假告警，
        平滑后更能反映持续偏移。A 的 Q3 也把它定位为"持续监测"。
        """
        drift = int((received_at - sent_at).total_seconds() * 1000)
        if self.clock_drift_samples == 0:
            self.clock_drift_ms = drift
        else:
            # α=0.2：兼顾响应速度与抗抖动
            self.clock_drift_ms = int(self.clock_drift_ms * 0.8 + drift * 0.2)
        self.clock_drift_samples += 1
        return drift

    @property
    def clock_drift_exceeds_warn(self) -> bool:
        return abs(self.clock_drift_ms) > self.drift_warn_ms

    def snapshot(self) -> dict[str, object]:
        """健康端点用的快照。"""
        return {
            "clockDriftMs": self.clock_drift_ms,
            "clockDriftWarnMs": self.drift_warn_ms,
            "clockDriftExceedsWarn": self.clock_drift_exceeds_warn,
            "unresolvedEvents": self.unresolved_events,
            "discarded": self.discarded,
            "rejectedItems": self.rejected_items,
            "unknownEventTypes": self.unknown_event_types,
            "batchesAccepted": self.batches_accepted,
            "batchesDuplicate": self.batches_duplicate,
            "batchesRateLimited": self.batches_rate_limited,
            "lastBatchAt": self.last_batch_at.isoformat() if self.last_batch_at else None,
        }


#: 进程内单例。单进程服务，无需跨进程共享。
#: **不要重新绑定这两个名字** —— 见 `IngestMetrics.reset` 的说明。
rate_limiter = BatchRateLimiter()
metrics = IngestMetrics()


def reset_metrics() -> None:
    """测试用：**原地**重置指标与限流窗口。

    保持 `rate_limiter` / `metrics` 的对象身份不变，这样已经 import 了
    它们的模块（`app.api.health` / `app.api.ingest`）看到的是同一份状态。
    """
    metrics.reset()
    rate_limiter.reset()
