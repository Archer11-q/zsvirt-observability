"""同进程周期任务测试（`app/scheduler.py`）。

## 这些测试在防什么

在此模块存在之前，`reconcile_all` / `mark_stale` / `mark_gone` 三个函数
**实现完整、测试通过、生产路径上从不执行** —— 没有 lifespan，没有任何周期性调用。
所以这里测的不是"函数对不对"（那是 `test_alert_engine.py` / `test_graph_*` 的事），
而是**它们有没有真的被接上**，以及接上之后会不会变成新的故障源。

重点覆盖：

1. **真的被调用** —— 启动后立刻跑第一轮，并真的把该恢复的告警恢复掉；
2. **恢复窗口真的生效** —— `ALERT_RECOVERY_AFTER_SEC` 此前根本传不进去；
3. **错误隔离** —— 一个任务抛异常不拖垮其他任务，也不让进程退出；
4. **不自锁** —— `_tick` 曾持锁调用还会取同一把锁的 body，两轮重叠会死等；
5. **默认不动演示数据** —— 陈旧阈值默认 0，因为 `app.demo` 的时间基准是固定过去。
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.alerts.engine import reconcile, reconcile_all
from app.config import Settings
from app.enums import AlertState, Observability
from app.graph.repository import mark_stale, upsert_resource
from app.models import Alert, Resource
from app.scheduler import PeriodicTasks, build_periodic_tasks, run_maintenance_once

#: 告警必须挂在真实存在的资源上（`alert.resource_id` 有外键约束 —— 约束在生效）。
ALERT_RESOURCE = "host:probe:probe-x:h0"

#: 必然"证据已停很久" / "刚刚还在报"的两个时刻，用来驱动恢复窗口判定。
LONG_AGO = datetime.now(UTC) - timedelta(hours=2)
RECENT = datetime.now(UTC) - timedelta(minutes=2)


def _ensure_resource(session, resource_id: str = ALERT_RESOURCE, *, seen_at=None) -> None:
    """建出被引用的资源。没有它插入会 ForeignKeyViolation。"""
    upsert_resource(
        session,
        resource_id=resource_id,
        kind="host",
        status="running",
        seen_at=seen_at or datetime.now(UTC),
    )


def _firing_alert(
    alert_id: str,
    *,
    last_fired: datetime,
    state: str = AlertState.FIRING.value,
    resource_id: str = ALERT_RESOURCE,
) -> Alert:
    return Alert(
        id=alert_id,
        rule_id="R-TEST",
        resource_id=resource_id,
        severity="error",
        state=state,
        first_fired_at=last_fired,
        last_fired_at=last_fired,
        aggregation_key=f"R-TEST:{alert_id}",
        evidence_event_ids=["evt_1"],
    )


async def _wait_for(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """轮询等待条件成立。

    `start()` 只负责**调度**协程，不等待第一轮跑完 —— 那是调度器该有的行为。
    测试若立刻断言 `runs >= 1` 就是在赌时序，所以这里等"最终成立"。
    """
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        waited += interval
    return predicate()


# ================================================================ 恢复语义


class TestReconcileAllRecoveryWindow:
    """`reconcile_all` 是**为后台任务建的**入口，它必须能接受配置窗口。"""

    def test_signature_exposes_recovery_after(self) -> None:
        """锁住「参数被透传」这一事实。

        上一版 `reconcile_all` 吞掉了 `recovery_after`，于是
        `ALERT_RECOVERY_AFTER_SEC` 配了也没用 —— 一个"可以配但配了没用"的
        配置项比没有更糟。用签名比对，比跑一遍更直接。
        """
        assert "recovery_after" in inspect.signature(reconcile_all).parameters
        assert "recovery_after" in inspect.signature(reconcile).parameters

    def test_recovers_an_alert_whose_evidence_stopped(self, db_session) -> None:
        """**D-084 的落地验证**：证据停够久的 `firing` 告警转 `resolved`。"""
        _ensure_resource(db_session)
        db_session.add(_firing_alert("alert_rec_1", last_fired=LONG_AGO))
        db_session.commit()

        result = reconcile_all(db_session, recovery_after=timedelta(minutes=10))
        db_session.commit()

        assert "alert_rec_1" in result["recovery"]["resolved"]
        row = db_session.get(Alert, "alert_rec_1")
        assert row.state == AlertState.RESOLVED.value
        assert row.resolved_at is not None

    def test_a_recent_alert_is_not_recovered(self, db_session) -> None:
        """窗口内的告警**不得**被恢复 —— 那是"还在发生"，不是"已恢复"。"""
        _ensure_resource(db_session)
        db_session.add(_firing_alert("alert_rec_2", last_fired=RECENT))
        db_session.commit()

        result = reconcile_all(db_session, recovery_after=timedelta(minutes=10))
        assert "alert_rec_2" not in result["recovery"]["resolved"]

    def test_silenced_alert_is_not_recovered(self, db_session) -> None:
        """静默期内没有新证据是**预期**的，不能据此推断条件解除（D-083）。

        否则等于把静默偷偷变成自动关单，而运维静默的意图往往是"我在处理，先别叫我"。
        """
        _ensure_resource(db_session)
        alert = _firing_alert("alert_rec_3", last_fired=LONG_AGO, state=AlertState.SILENCED.value)
        alert.silenced_until = datetime.now(UTC) + timedelta(hours=1)
        db_session.add(alert)
        db_session.commit()

        result = reconcile_all(db_session, recovery_after=timedelta(minutes=10))
        assert "alert_rec_3" not in result["recovery"]["resolved"]

    def test_maintenance_once_returns_all_three_steps(self) -> None:
        """返回结构是给测试与健康检查用的契约。"""
        result = run_maintenance_once()
        assert set(result) == {"alerts", "stale", "gone"}
        assert "released" in result["alerts"]
        assert "recovery" in result["alerts"]

    def test_maintenance_is_idempotent(self) -> None:
        """重复跑不产生新变化 —— 它每 60 秒就跑一次。"""
        first = run_maintenance_once()
        second = run_maintenance_once()
        assert set(second) == set(first)


# ================================================================ 调度器行为


class TestPeriodicTasksLifecycle:
    @pytest.mark.anyio
    async def test_start_runs_immediately_then_stops_cleanly(self) -> None:
        """**先跑再睡**：服务刚起来时库里可能已有待恢复的告警。

        等一个完整周期会让这段时间的状态是错的。uvicorn 重启时若后台任务
        没被收回，会留下半写状态的 session，所以 stop 也必须干净、可重复。
        """
        tasks = PeriodicTasks(interval_sec=60)
        await tasks.start()
        assert await _wait_for(lambda: tasks.states["alerts"]["runs"] >= 1), (
            "启动后应立刻跑一次（而不是等满一个周期）"
        )
        await tasks.stop()

        runs_after_stop = tasks.states["alerts"]["runs"]
        await tasks.stop()  # 幂等
        assert tasks.states["alerts"]["runs"] == runs_after_stop

    @pytest.mark.anyio
    async def test_a_failing_task_does_not_kill_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**后台任务不能成为新的故障源。**

        告警任务炸了，生命周期任务照样要跑完 —— 否则一次数据库偶发抖动
        会让整个自愈机制静默停摆。
        """
        tasks = PeriodicTasks(interval_sec=60)

        def boom() -> dict[str, Any]:
            raise RuntimeError("模拟数据库抖动")

        monkeypatch.setattr(tasks, "_run_alerts", boom)
        await tasks.start()
        assert await _wait_for(lambda: tasks.states["alerts"]["failures"] >= 1)
        assert await _wait_for(lambda: tasks.states["lifecycle"]["runs"] >= 1)
        await tasks.stop()

        alerts = tasks.states["alerts"]
        # 注意是 camelCase：`states` 直接产出给 `/api/health` 的 JSON 结构
        assert "模拟数据库抖动" in (alerts["lastError"] or "")
        # 另一个任务不受影响
        assert tasks.states["lifecycle"]["failures"] == 0

    @pytest.mark.anyio
    async def test_overlapping_ticks_do_not_deadlock(self) -> None:
        """**回归测试**：`_tick` 持锁调用 body 会自锁。

        `_tick` 曾持有 `_guard` 再调用 `_run_alerts`，而后者也要取同一把锁。
        单独一轮碰巧能过，**两轮重叠就永久阻塞** —— 在服务里表现为后台自愈
        悄悄停摆，且没有任何报错。现在轮次互斥由独立的 `_serialize` 负责。
        """
        tasks = PeriodicTasks(interval_sec=60)
        await asyncio.wait_for(
            asyncio.gather(
                tasks._tick("alerts", tasks._run_alerts),
                tasks._tick("alerts", tasks._run_alerts),
            ),
            timeout=5,
        )
        assert tasks.states["alerts"]["runs"] == 2
        assert tasks.states["alerts"]["failures"] == 0

    @pytest.mark.anyio
    async def test_disabled_lifecycle_is_a_noop_not_a_failure(self) -> None:
        """阈值 0 时生命周期任务是**空操作**，不是失败。

        返回 `None` 而不是 `0`：让"没做"与"做了但没影响任何行"可区分。
        """
        tasks = PeriodicTasks(interval_sec=60, settings=Settings())
        await tasks.start()
        assert await _wait_for(lambda: tasks.states["lifecycle"]["runs"] >= 1)
        await tasks.stop()

        state = tasks.states["lifecycle"]
        assert state["failures"] == 0
        assert state["lastResult"]["stale"] is None
        assert state["lastResult"]["disabled"] is True

    def test_interval_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="interval_sec"):
            PeriodicTasks(interval_sec=0)


class TestBuildPeriodicTasks:
    def test_disabled_by_config_returns_none(self) -> None:
        """`SCHEDULER_ENABLED=false` 时不构造调度器。

        本地演示需要这个开关：不希望后台悄悄把演示数据标成陈旧。
        """
        assert build_periodic_tasks(Settings(SCHEDULER_ENABLED=False)) is None

    def test_enabled_by_default(self) -> None:
        assert build_periodic_tasks(Settings()) is not None


# ================================================================ 资源生命周期


class TestResourceLifecycleDefaults:
    def test_default_thresholds_disable_the_lifecycle_step(self) -> None:
        """**默认关闭是有意的，不是漏配。**

        `app.demo` 的演示数据时间基准是固定的过去时刻（为了可复现），
        默认开启陈旧标记会让全部资源立刻变 `stale` —— 语义正确，但演示画面上
        一片离线符号。等准备演示时再按当时的策略打开。
        """
        cfg = Settings()
        assert cfg.resource_stale_after_sec == 0
        assert cfg.resource_gone_after_sec == 0

    def test_stale_marking_works_when_enabled(self, db_session) -> None:
        """显式给了阈值，陈旧资源确实会被标记 —— 关闭不等于坏掉。"""
        _ensure_resource(
            db_session, "host:probe:probe-x:h_stale", seen_at=datetime.now(UTC) - timedelta(hours=5)
        )
        db_session.commit()

        affected = mark_stale(db_session, older_than_seconds=3600)
        db_session.commit()
        assert affected >= 1

        row = db_session.get(Resource, "host:probe:probe-x:h_stale")
        assert row.observability == Observability.STALE.value

    def test_fresh_resource_is_not_marked(self, db_session) -> None:
        """刚有心跳的资源不能被标陈旧 —— 否则整个陈旧机制就是噪声源。"""
        _ensure_resource(db_session, "host:probe:probe-x:h_fresh")
        db_session.commit()

        mark_stale(db_session, older_than_seconds=3600)
        db_session.commit()

        row = db_session.get(Resource, "host:probe:probe-x:h_fresh")
        assert row.observability == Observability.ACTIVE.value


# ================================================================ 健康可见性


class TestSchedulerIsVisibleInHealth:
    def test_health_reports_disabled_when_lifespan_did_not_run(self, client: TestClient) -> None:
        """未进入上下文 ⇒ 调度器未启动 ⇒ 报 `disabled` 而**不是**故障。

        `TestClient(app)` 不进入上下文就不会触发 lifespan，这是本项目测试的常态，
        不该让 `/api/health` 变 degraded。
        """
        comp = client.get("/api/health").json()["components"]["scheduler"]
        assert comp["status"] == "ok"
        assert comp["detail"]["enabled"] is False
