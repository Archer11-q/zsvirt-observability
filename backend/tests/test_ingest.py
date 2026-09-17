"""接入层测试（service 层，真实数据库）。

覆盖成员 A 已确认的 7 项语义，每项都对应 A 的原始诉求编号：

| 测试 | A 的诉求 |
|---|---|
| 幂等：重复批次结果与首次一致 | Q2 |
| 逐条返回 resource 结果与 globalId | Q1 |
| 孤儿引用挂占位节点 | Q1 兜底 |
| 批量上限 | Q4 |
| 时钟漂移计算与暴露 | Q3 |
| 迟到批次按 occurredAt 追加 | Q6 |
| 限流决策与 Retry-After | Q4 |

另覆盖脱敏（`SENSITIVE_DATA.md`）与严重级别兜底（C 的要求）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import EventSource, Severity
from app.graph.repository import count_unresolved
from app.ingest.limits import BatchRateLimiter, IngestMetrics
from app.ingest.schemas import IngestBatch
from app.ingest.service import BatchTooLarge, ingest_batch, validate_batch_size
from app.models import Event, Resource
from app.models import IngestBatch as IngestBatchRow

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
AGENT = "probe-3f2a9c10"
VM_ID = "vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"


def make_payload(**overrides) -> dict:
    """构造一批合法上报（对照 A 的探针实际输出形态）。"""
    payload = {
        "agentId": AGENT,
        "vmId": VM_ID,
        "agentVersion": "0.1.0",
        "batchId": "01J0000000000000000000001",
        "sentAt": NOW.isoformat(),
        "resources": [
            {
                "kind": "container",
                "sourceId": "web-0",
                "name": "web-0",
                "status": "running",
                "attributes": {"image": "vllm/vllm-openai", "runtime": "docker"},
            }
        ],
        "events": [
            {
                "occurredAt": (NOW - timedelta(seconds=10)).isoformat(),
                "resourceRef": {"kind": "container", "sourceId": "web-0"},
                "type": "container.oom_killed",
                "severity": "critical",
                "message": "container killed by OOM",
                "metrics": {"exit_code": 137},
            }
        ],
    }
    payload.update(overrides)
    return payload


def fresh_limits() -> tuple[BatchRateLimiter, IngestMetrics]:
    """每个测试用独立的限流器与指标，避免相互干扰。"""
    return BatchRateLimiter(max_batches=1000, window_seconds=60), IngestMetrics()


# ---------------------------------------------------------------- 基础


class TestBasicIngest:
    def test_accepts_resources_and_events(self, db_session: Session) -> None:
        limiter, run_metrics = fresh_limits()
        batch = IngestBatch.model_validate(make_payload())

        result, duplicate = ingest_batch(
            db_session, batch=batch, received_at=NOW, limiter=limiter, run_metrics=run_metrics
        )
        db_session.commit()

        assert duplicate is False
        assert result.accepted.resources == 1
        assert result.accepted.events == 1
        assert result.rejected == []

        # 资源已落库，且 ID 是探针四段形态
        rid = result.resources[0].globalId
        assert rid == f"container:probe:{AGENT}:web-0"
        assert db_session.get(Resource, rid) is not None

        # 事件已落库
        events = list(db_session.execute(select(Event)).scalars())
        assert len(events) == 1
        assert events[0].type == "container.oom_killed"
        assert events[0].severity == Severity.CRITICAL.value
        assert events[0].source == EventSource.PROBE.value
        assert events[0].batch_id == batch.batchId

    def test_returns_per_resource_result_with_global_id(self, db_session: Session) -> None:
        """A 的 Q1：逐条返回 resource 接受结果，A 据此维护「已确认资源」缓存。"""
        limiter, run_metrics = fresh_limits()
        payload = make_payload(
            resources=[
                {"kind": "container", "sourceId": "web-0", "status": "running"},
                {"kind": "ai_service", "sourceId": "vllm", "status": "running"},
                {"kind": "process", "sourceId": "12345.678", "status": "running"},
            ],
            events=[],
        )
        result, _ = ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        assert len(result.resources) == 3
        assert [r.index for r in result.resources] == [0, 1, 2]
        assert all(r.result == "accepted" for r in result.resources)
        assert all(r.globalId for r in result.resources)
        assert result.resources[2].globalId == f"process:probe:{AGENT}:12345.678"

    def test_event_occurred_at_preserved(self, db_session: Session) -> None:
        """`occurredAt` 是产生方给出的时刻，写入时不得被改写为接收时间。"""
        limiter, run_metrics = fresh_limits()
        occurred = NOW - timedelta(minutes=3)
        payload = make_payload(
            events=[
                {
                    "occurredAt": occurred.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "web-0"},
                    "type": "container.restart",
                    "severity": "warning",
                }
            ]
        )
        ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        ev = db_session.execute(select(Event)).scalars().one()
        assert ev.occurred_at.astimezone(UTC) == occurred
        assert ev.received_at.astimezone(UTC) == NOW
        assert ev.occurred_at != ev.received_at


# ---------------------------------------------------------------- 幂等


class TestIdempotency:
    def test_duplicate_batch_replays_first_result(self, db_session: Session) -> None:
        """**A 的 Q2 核心**：重复批次返回与首次**一致**的结果。

        "一致"的准确含义：除 `duplicate` 标记外逐字段相同 ——
        该标记正是用来区分"这是重复投递"的，必须由 False 变为 True。
        A 用这个响应更新自己的「已确认资源」缓存，若逐条结果不同会污染缓存。
        """
        limiter, run_metrics = fresh_limits()
        batch = IngestBatch.model_validate(make_payload())

        first, dup1 = ingest_batch(
            db_session, batch=batch, received_at=NOW, limiter=limiter, run_metrics=run_metrics
        )
        db_session.commit()

        second, dup2 = ingest_batch(
            db_session,
            batch=batch,
            received_at=NOW + timedelta(seconds=30),
            limiter=limiter,
            run_metrics=run_metrics,
        )

        assert dup1 is False
        assert dup2 is True, "重复批次必须被识别"
        assert first.duplicate is False
        assert second.duplicate is True, "重复批次的 duplicate 标记必须为 True"

        # 除 duplicate 外逐字段一致
        assert second.model_dump(exclude={"duplicate"}) == first.model_dump(
            exclude={"duplicate"}
        ), "除 duplicate 标记外，回放结果必须与首次逐字段一致"
        # 逐条 resource 结果尤其重要（A 的「已确认资源」缓存依赖它）
        assert second.resources == first.resources

    def test_duplicate_does_not_write_events_twice(self, db_session: Session) -> None:
        """幂等的副作用：事件不得重复入库。"""
        limiter, run_metrics = fresh_limits()
        batch = IngestBatch.model_validate(make_payload())

        ingest_batch(
            db_session, batch=batch, received_at=NOW, limiter=limiter, run_metrics=run_metrics
        )
        db_session.commit()
        ingest_batch(
            db_session, batch=batch, received_at=NOW, limiter=limiter, run_metrics=run_metrics
        )
        db_session.commit()

        events = list(db_session.execute(select(Event)).scalars())
        assert len(events) == 1, "重复批次不得产生第二条事件"

    def test_duplicate_counted_in_metrics(self, db_session: Session) -> None:
        limiter, run_metrics = fresh_limits()
        batch = IngestBatch.model_validate(make_payload())
        ingest_batch(
            db_session, batch=batch, received_at=NOW, limiter=limiter, run_metrics=run_metrics
        )
        db_session.commit()
        ingest_batch(
            db_session, batch=batch, received_at=NOW, limiter=limiter, run_metrics=run_metrics
        )
        assert run_metrics.batches_duplicate == 1
        assert run_metrics.batches_accepted == 1


# ---------------------------------------------------------------- 部分接受


class TestPartialAcceptance:
    def test_invalid_resource_rejected_others_accepted(self, db_session: Session) -> None:
        """**部分接受**：单条非法不导致整批拒收，且被拒条目带 index。

        `sourceId` 含冒号会让全局 ID 变成五段 —— 必须被拒，
        且错误信息要能指出是哪个 index。
        """
        limiter, run_metrics = fresh_limits()
        payload = make_payload(
            resources=[
                {"kind": "container", "sourceId": "web-0", "status": "running"},
                {"kind": "process", "sourceId": "12345:678", "status": "running"},  # 非法
                {"kind": "ai_service", "sourceId": "vllm", "status": "running"},
            ],
            events=[],
        )
        result, _ = ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        assert result.accepted.resources == 2
        assert len(result.rejected) == 1
        rejected = result.rejected[0]
        assert rejected.index == 1, "被拒条目必须带原始 index，便于定位探针 bug"
        assert rejected.reason == "INVALID_RESOURCE_ID"
        assert "must not contain" in rejected.detail

        results_by_index = {r.index: r for r in result.resources}
        assert results_by_index[0].result == "accepted"
        assert results_by_index[1].result == "rejected"
        assert results_by_index[2].result == "accepted"


# ---------------------------------------------------------------- 孤儿引用


class TestOrphanReference:
    def test_orphan_event_creates_placeholder(self, db_session: Session) -> None:
        """**A 的 Q1 兜底**：事件引用的资源未上报 → 挂占位节点，不丢数据、不报错。"""
        limiter, run_metrics = fresh_limits()
        payload = make_payload(
            resources=[],  # 一个资源都不上报
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "ghost-9"},
                    "type": "container.oom_killed",
                    "severity": "critical",
                }
            ],
        )
        result, _ = ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        assert result.accepted.events == 1, "孤儿事件不得被丢弃"
        assert result.rejected == []
        assert run_metrics.unresolved_events == 1
        assert count_unresolved(db_session) == 1

        ev = db_session.execute(select(Event)).scalars().one()
        placeholder = db_session.get(Resource, ev.resource_id)
        assert placeholder is not None
        assert placeholder.is_placeholder is True
        assert placeholder.kind == "unresolved"

    def test_known_resource_is_not_placeholder(self, db_session: Session) -> None:
        """先上报资源、再发事件 → 事件挂在真实资源上，无占位节点。"""
        limiter, run_metrics = fresh_limits()
        result, _ = ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(make_payload()),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        assert run_metrics.unresolved_events == 0
        assert count_unresolved(db_session) == 0
        ev = db_session.execute(select(Event)).scalars().one()
        assert ev.resource_id == result.resources[0].globalId


# ---------------------------------------------------------------- 上限


class TestLimits:
    def test_events_over_limit_raises(self) -> None:
        batch = IngestBatch.model_validate(
            make_payload(
                events=[
                    {
                        "occurredAt": NOW.isoformat(),
                        "resourceRef": {"kind": "container", "sourceId": "w"},
                        "type": "container.restart",
                        "severity": "warning",
                    }
                    for _ in range(5)
                ]
            )
        )
        with pytest.raises(BatchTooLarge, match="events 数量 5 超过上限 3"):
            validate_batch_size(batch, max_events=3)

    def test_resources_over_limit_raises(self) -> None:
        batch = IngestBatch.model_validate(
            make_payload(
                resources=[
                    {"kind": "container", "sourceId": f"w{i}", "status": "running"}
                    for i in range(4)
                ],
                events=[],
            )
        )
        with pytest.raises(BatchTooLarge, match="resources 数量 4 超过上限 2"):
            validate_batch_size(batch, max_resources=2)

    def test_defaults_match_confirmed_values(self, db_session: Session) -> None:
        """成员 A 的 Q4 已确认：events ≤ 1000、resources ≤ 500、payload ≤ 1MB。"""
        from app.ingest.schemas import (
            DEFAULT_MAX_EVENTS,
            DEFAULT_MAX_PAYLOAD_BYTES,
            DEFAULT_MAX_RESOURCES,
        )

        assert DEFAULT_MAX_EVENTS == 1000
        assert DEFAULT_MAX_RESOURCES == 500
        assert DEFAULT_MAX_PAYLOAD_BYTES == 1024 * 1024


# ---------------------------------------------------------------- 时钟漂移


class TestClockDrift:
    def test_drift_computed_from_sent_and_received(self, db_session: Session) -> None:
        """**A 的 Q3**：B 用 `receivedAt − sentAt` 计算漂移并暴露为健康指标。"""
        limiter, run_metrics = fresh_limits()
        sent = NOW - timedelta(seconds=2)
        payload = make_payload(sentAt=sent.isoformat())

        ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        # 首次观测直接取值（无历史可平滑）
        assert run_metrics.clock_drift_ms == 2000
        assert run_metrics.clock_drift_samples == 1

    def test_drift_smoothing_reduces_single_spike_impact(self) -> None:
        """单次网络抖动不应造成假告警 —— 用指数滑动平均平滑。"""
        m = IngestMetrics(drift_warn_ms=5000)
        m.observe_clock_drift(NOW, NOW)  # 0ms
        m.observe_clock_drift(NOW, NOW - timedelta(seconds=30))  # 单次 30000ms 尖峰
        assert m.clock_drift_ms == 6000, "平滑后不应直接跳到 30000"
        assert m.clock_drift_exceeds_warn is True

    def test_drift_below_threshold_not_flagged(self) -> None:
        m = IngestMetrics(drift_warn_ms=5000)
        m.observe_clock_drift(NOW, NOW - timedelta(seconds=3))
        assert m.clock_drift_ms == 3000
        assert m.clock_drift_exceeds_warn is False

    def test_negative_drift_is_measured(self) -> None:
        """探针时钟超前 B 也是漂移，取绝对值判断是否告警。"""
        m = IngestMetrics(drift_warn_ms=5000)
        m.observe_clock_drift(NOW, NOW + timedelta(seconds=8))
        assert m.clock_drift_ms == -8000
        assert m.clock_drift_exceeds_warn is True


# ---------------------------------------------------------------- 迟到批次


class TestLateBatch:
    def test_late_batch_is_appended_not_dropped(self, db_session: Session) -> None:
        """**A 的 Q6**：断网恢复后补传的历史批次，按 `occurredAt` 追加写入，
        不得因 `occurredAt` 早于当前时间而丢弃或告警。
        """
        limiter, run_metrics = fresh_limits()
        # 先收一条"新"事件
        ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(
                make_payload(
                    batchId="batch-new",
                    events=[
                        {
                            "occurredAt": NOW.isoformat(),
                            "resourceRef": {"kind": "container", "sourceId": "web-0"},
                            "type": "container.restart",
                            "severity": "warning",
                        }
                    ],
                )
            ),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        # 再补传一个 2 小时前的历史批次
        old = NOW - timedelta(hours=2)
        result, _ = ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(
                make_payload(
                    batchId="batch-late",
                    events=[
                        {
                            "occurredAt": old.isoformat(),
                            "resourceRef": {"kind": "container", "sourceId": "web-0"},
                            "type": "container.oom_killed",
                            "severity": "critical",
                        }
                    ],
                )
            ),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        assert result.accepted.events == 1, "迟到批次不得被丢弃"
        assert result.rejected == []
        events = list(db_session.execute(select(Event)).scalars())
        assert len(events) == 2
        old_event = next(e for e in events if e.type == "container.oom_killed")
        assert old_event.occurred_at.astimezone(UTC) == old


# ---------------------------------------------------------------- 限流


class TestRateLimiter:
    def test_allows_within_limit(self) -> None:
        rl = BatchRateLimiter(max_batches=3, window_seconds=60)
        for _ in range(3):
            assert rl.check_and_record(NOW).allowed is True

    def test_rejects_over_limit_with_retry_after(self) -> None:
        """**A 的 Q4**：必须给出明确的 `Retry-After`，A 据此退避而不是无脑重放。"""
        rl = BatchRateLimiter(max_batches=2, window_seconds=60)
        rl.check_and_record(NOW)
        rl.check_and_record(NOW)
        decision = rl.check_and_record(NOW)
        assert decision.allowed is False
        assert decision.retry_after_seconds >= 1
        assert "rate limit exceeded" in decision.reason

    def test_window_slides(self) -> None:
        """窗口滑动后应恢复放行 —— 不能变成永久封锁。"""
        rl = BatchRateLimiter(max_batches=2, window_seconds=60)
        rl.check_and_record(NOW)
        rl.check_and_record(NOW)
        assert rl.check_and_record(NOW).allowed is False
        assert rl.check_and_record(NOW + timedelta(seconds=61)).allowed is True

    def test_retry_after_shrinks_as_window_advances(self) -> None:
        rl = BatchRateLimiter(max_batches=1, window_seconds=60)
        rl.check_and_record(NOW)
        first = rl.check_and_record(NOW).retry_after_seconds
        later = rl.check_and_record(NOW + timedelta(seconds=50)).retry_after_seconds
        assert later < first, "越接近窗口末尾，等待时间应越短"


# ---------------------------------------------------------------- 脱敏


class TestRedactionInIngest:
    def test_raw_secrets_removed_before_persist(self, db_session: Session) -> None:
        """`docs/SENSITIVE_DATA.md`：`raw` 入库前必过脱敏管道。"""
        limiter, run_metrics = fresh_limits()
        payload = make_payload(
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "web-0"},
                    "type": "inference.error",
                    "severity": "error",
                    "raw": {
                        "docker": {
                            "env": {"OPENAI_API_KEY": "sk-leak", "PATH": "/usr/bin"},
                            "image": "vllm",
                        }
                    },
                }
            ]
        )
        ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        ev = db_session.execute(select(Event)).scalars().one()
        assert "OPENAI_API_KEY" not in ev.raw["docker"]["env"]
        assert ev.raw["docker"]["env"]["PATH"] == "/usr/bin"
        assert ev.raw["docker"]["image"] == "vllm"

    def test_cmdline_masked_but_structure_kept(self, db_session: Session) -> None:
        """`cmdline` 掩码参数值但保留结构 —— 保留诊断价值，不暴露凭据。"""
        limiter, run_metrics = fresh_limits()
        payload = make_payload(
            resources=[
                {
                    "kind": "process",
                    "sourceId": "999.1",
                    "status": "running",
                    "attributes": {
                        "cmdline": [
                            "python",
                            "serve.py",
                            "--api-key",
                            "secret123",
                            "--port",
                            "8000",
                        ]
                    },
                }
            ],
            events=[],
        )
        result, _ = ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        res = db_session.get(Resource, result.resources[0].globalId)
        assert res is not None
        assert res.attributes["cmdline"] == [
            "python",
            "serve.py",
            "--api-key",
            "***",
            "--port",
            "8000",
        ]
        assert "secret123" not in str(res.attributes)

    def test_message_connection_string_masked(self, db_session: Session) -> None:
        limiter, run_metrics = fresh_limits()
        payload = make_payload(
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "web-0"},
                    "type": "inference.error",
                    "severity": "error",
                    "message": "connect failed to postgresql://user:hunter2@10.0.0.5:5432/db",
                }
            ]
        )
        ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        ev = db_session.execute(select(Event)).scalars().one()
        assert "hunter2" not in (ev.message or "")
        assert "10.0.0.5" in (ev.message or ""), "主机与库名保留，诊断需要"


# ---------------------------------------------------------------- 严重级别


class TestSeverityFallback:
    def test_omitted_severity_uses_frozen_default(self, db_session: Session) -> None:
        """成员 C 要求：每个 `Event` 都必须带 `severity`，不能为 None。"""
        limiter, run_metrics = fresh_limits()
        payload = make_payload(
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "web-0"},
                    "type": "container.oom_killed",
                    # 故意不传 severity
                }
            ]
        )
        ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        ev = db_session.execute(select(Event)).scalars().one()
        assert ev.severity == Severity.CRITICAL.value

    def test_explicit_severity_wins(self, db_session: Session) -> None:
        limiter, run_metrics = fresh_limits()
        payload = make_payload(
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "web-0"},
                    "type": "container.oom_killed",
                    "severity": "warning",  # 显式给出，应覆盖默认
                }
            ]
        )
        ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()
        ev = db_session.execute(select(Event)).scalars().one()
        assert ev.severity == Severity.WARNING.value

    def test_unknown_event_type_accepted_but_counted(self, db_session: Session) -> None:
        """非枚举事件类型**不拒收**（拒收代价过高），但要让契约违约可见。"""
        limiter, run_metrics = fresh_limits()
        payload = make_payload(
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "web-0"},
                    "type": "some.new.event_type",  # 不在 F-01 枚举中
                    "severity": "warning",
                }
            ]
        )
        result, _ = ingest_batch(
            db_session,
            batch=IngestBatch.model_validate(payload),
            received_at=NOW,
            limiter=limiter,
            run_metrics=run_metrics,
        )
        db_session.commit()

        assert result.accepted.events == 1, "未知类型不得导致拒收"
        assert result.acceptedUnknownTypes == 1
        assert run_metrics.unknown_event_types == 1


# ---------------------------------------------------------------- 批次记录


class TestBatchRecord:
    def test_batch_row_stored_with_result(self, db_session: Session) -> None:
        limiter, run_metrics = fresh_limits()
        batch = IngestBatch.model_validate(make_payload())
        result, _ = ingest_batch(
            db_session, batch=batch, received_at=NOW, limiter=limiter, run_metrics=run_metrics
        )
        db_session.commit()

        row = db_session.get(IngestBatchRow, batch.batchId)
        assert row is not None
        assert row.agent_id == AGENT
        assert row.vm_id == VM_ID
        assert row.accepted_events == 1
        # 保存的 result 必须能还原成与首次响应一致的对象 —— 这是幂等回放的基础
        from app.ingest.schemas import IngestResult

        assert IngestResult.model_validate(row.result).model_dump() == result.model_dump()
