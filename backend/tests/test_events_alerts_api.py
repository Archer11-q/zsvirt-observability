"""事件与告警查询端点测试。

重点验证**游标分页的稳定性**（成员 C 的 Q11 明确要求"避免重复/漏数据"），
以及告警状态机的三种结果区分：

| 情形 | 期望 |
|---|---|
| 重复操作（已处目标状态） | 200 + `changed=False`，**不报错** |
| 合法迁移 | 200 + `changed=True` |
| 非法迁移 | 409 CONFLICT |

以及两条契约红线：

- 告警**必须**能展开证据（`DATA_MODEL.md` §5.2 的可解释性下限）
- 时间统一 UTC 输出（不暴露会话时区偏移）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.enums import AlertState, EventSource, ResourceKind, ResourceStatus, Severity
from app.graph.repository import ensure_edge, upsert_resource
from app.models import Alert, Event

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)

HOST = "host:zsvirt:h1"
GPU = "gpu:zsvirt:g0"
VGPU = "vgpu:zsvirt:v0"
VM = "vm:zsvirt:vm0"
CTR = "container:probe:probe-x:web-0"
AIS = "ai_service:probe:probe-x:vllm"


@pytest.fixture
def client(db_engine: Engine):
    from sqlalchemy.orm import sessionmaker

    from app.db import get_db
    from app.ingest.limits import reset_metrics

    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)

    def _override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    reset_metrics()

    from app.main import app

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def seed_resources(session: Session, seen_at: datetime = NOW) -> None:
    upsert_resource(
        session,
        resource_id=HOST,
        kind=ResourceKind.HOST.value,
        name="node-01",
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
    )
    upsert_resource(
        session,
        resource_id=GPU,
        kind=ResourceKind.GPU.value,
        parent_id=HOST,
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
    )
    upsert_resource(
        session,
        resource_id=VM,
        kind=ResourceKind.VM.value,
        parent_id=GPU,
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
    )
    upsert_resource(
        session,
        resource_id=CTR,
        kind=ResourceKind.CONTAINER.value,
        parent_id=VM,
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
    )
    ensure_edge(session, HOST, GPU)
    ensure_edge(session, GPU, VM)
    ensure_edge(session, VM, CTR)
    session.flush()


def seed_events(session: Session, count: int, *, start: datetime = NOW) -> list[str]:
    """插入 `count` 条事件，每秒一条。返回 id 列表（按发生时间升序）。"""
    ids: list[str] = []
    for i in range(count):
        eid = f"evt_{i:026d}"
        session.add(
            Event(
                id=eid,
                occurred_at=start + timedelta(seconds=i),
                received_at=start + timedelta(seconds=i),
                source=EventSource.PROBE.value,
                resource_id=CTR,
                type="container.restart" if i % 2 == 0 else "inference.timeout",
                severity=Severity.WARNING.value if i % 2 == 0 else Severity.ERROR.value,
                message=f"event {i}",
                metrics={"i": i},
                raw={"docker": {"image": "vllm"}},
            )
        )
        ids.append(eid)
    session.flush()
    return ids


def seed_alert(
    session: Session,
    alert_id: str,
    *,
    evidence: list[str],
    state: str = AlertState.FIRING.value,
    severity: str = Severity.CRITICAL.value,
    fired_at: datetime | None = None,
) -> None:
    moment = fired_at or NOW
    session.add(
        Alert(
            id=alert_id,
            rule_id="R-CONTAINER-OOM-001",
            resource_id=CTR,
            severity=severity,
            state=state,
            first_fired_at=moment,
            last_fired_at=moment,
            count=1,
            evidence_event_ids=evidence,
            aggregation_key=f"container.oom_killed:{CTR}",
            title="容器 OOM",
        )
    )
    session.flush()


# ================================================================ 事件


class TestEventQuery:
    def test_list_returns_newest_first(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        seed_events(db_session, 5)
        db_session.commit()

        body = client.get("/api/v1/events").json()
        items = body["data"]["items"]
        assert len(items) == 5
        times = [i["occurredAt"] for i in items]
        assert times == sorted(times, reverse=True), "默认按时间降序（最新在前）"

    def test_event_shape(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        seed_events(db_session, 1)
        db_session.commit()

        ev = client.get("/api/v1/events").json()["data"]["items"][0]
        assert set(ev) >= {
            "id",
            "occurredAt",
            "receivedAt",
            "source",
            "resourceId",
            "type",
            "severity",
            "message",
            "metrics",
            "raw",
        }
        assert ev["metrics"]["i"] == 0
        assert ev["raw"]["docker"]["image"] == "vllm"

    def test_times_are_utc(self, client: TestClient, db_session: Session) -> None:
        """时间必须统一 UTC 输出 —— 暴露会话时区偏移会让前端比较出错。"""
        seed_resources(db_session)
        seed_events(db_session, 1)
        db_session.commit()
        ev = client.get("/api/v1/events").json()["data"]["items"][0]
        assert ev["occurredAt"].endswith("+00:00")
        assert ev["receivedAt"].endswith("+00:00")

    def test_time_window_filter(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        seed_events(db_session, 10)  # NOW .. NOW+9s
        db_session.commit()

        r = client.get(
            "/api/v1/events",
            params={
                "from": (NOW + timedelta(seconds=3)).isoformat(),
                "to": (NOW + timedelta(seconds=5)).isoformat(),
            },
        )
        items = r.json()["data"]["items"]
        assert len(items) == 3

    def test_type_filter_multi_value(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        seed_events(db_session, 6)
        db_session.commit()

        r = client.get("/api/v1/events", params=[("type", "inference.timeout"), ("limit", "100")])
        items = r.json()["data"]["items"]
        assert items and all(i["type"] == "inference.timeout" for i in items)

    def test_min_severity_filter(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        seed_events(db_session, 6)
        db_session.commit()

        items = client.get("/api/v1/events", params={"minSeverity": "error"}).json()["data"][
            "items"
        ]
        assert items and all(i["severity"] == "error" for i in items)

    def test_invalid_min_severity_returns_400(self, client: TestClient) -> None:
        r = client.get("/api/v1/events", params={"minSeverity": "fatal"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_invalid_window_returns_400(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/events",
            params={
                "from": NOW.isoformat(),
                "to": (NOW - timedelta(hours=1)).isoformat(),
            },
        )
        assert r.status_code == 400

    def test_get_single_event(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 3)
        db_session.commit()
        r = client.get(f"/api/v1/events/{ids[0]}")
        assert r.status_code == 200
        assert r.json()["data"]["id"] == ids[0]

    def test_unknown_event_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/events/evt_nope")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "EVENT_NOT_FOUND"
        assert r.json()["error"]["traceId"]


class TestEventCursorPagination:
    """**C 的 Q11 核心**：游标分页必须稳定，不漏不重。"""

    def test_backward_pagination_covers_all_without_overlap(
        self, client: TestClient, db_session: Session
    ) -> None:
        seed_resources(db_session)
        seed_events(db_session, 25)
        db_session.commit()

        seen: list[str] = []
        cursor = None
        for _ in range(10):  # 安全上限，防止死循环
            params = {"limit": 7}
            if cursor:
                params["cursor"] = cursor
            body = client.get("/api/v1/events", params=params).json()
            seen.extend(i["id"] for i in body["data"]["items"])
            cursor = body["meta"]["page"]["nextCursor"]
            if not cursor:
                break

        assert len(seen) == 25, f"应翻完 25 条，实际 {len(seen)}"
        assert len(seen) == len({*seen}), "分页不得返回重复记录"

    def test_next_cursor_absent_on_last_page(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        seed_events(db_session, 5)
        db_session.commit()
        body = client.get("/api/v1/events", params={"limit": 5}).json()
        assert body["meta"]["page"]["nextCursor"] is None

    def test_concurrent_insert_does_not_shift_pagination(
        self, client: TestClient, db_session: Session
    ) -> None:
        """**offset 分页会在这里出错**：翻页途中插入更新的事件，
        offset 偏移导致漏读。keyset 游标锚定在具体记录上，不受影响。
        """
        seed_resources(db_session)
        ids = seed_events(db_session, 10)
        db_session.commit()

        first = client.get("/api/v1/events", params={"limit": 5}).json()
        first_ids = [i["id"] for i in first["data"]["items"]]
        cursor = first["meta"]["page"]["nextCursor"]

        # 插入 3 条"更新"的事件（模拟并发上报）
        for j in range(3):
            db_session.add(
                Event(
                    id=f"evt_new{j:023d}",
                    occurred_at=NOW + timedelta(seconds=100 + j),
                    received_at=NOW,
                    source=EventSource.PROBE.value,
                    resource_id=CTR,
                    type="container.restart",
                    severity=Severity.WARNING.value,
                )
            )
        db_session.commit()

        second = client.get("/api/v1/events", params={"limit": 5, "cursor": cursor}).json()
        second_ids = [i["id"] for i in second["data"]["items"]]

        assert not (set(first_ids) & set(second_ids)), "两页不得重叠"
        assert set(second_ids) <= set(ids), "第二页应仍是原有记录（未被新插入影响）"

    def test_after_cursor_supports_incremental_polling(
        self, client: TestClient, db_session: Session
    ) -> None:
        """**轮询场景（C 的 Q11）**：`after` 只返回比游标更新的记录，升序便于顺序处理。

        模拟一次真实轮询：

        1. 已有 5 条旧事件（t0..t4），把游标停在 t1
        2. `after=t1` → 应只拿到 t2/t3/t4，升序
        3. 新来一条 t5（模拟 C 两次轮询之间产生的数据）
        4. 用上一次的最后一条作游标再拉 → 应只拿到 t5，**不重不漏**
        """
        from app.api.schemas.common import encode_cursor

        seed_resources(db_session)
        seed_events(db_session, 5)  # t0 .. t4
        db_session.commit()

        all_items = client.get("/api/v1/events", params={"limit": 100}).json()["data"]["items"]
        # 默认降序 → 反转成时间升序，便于按时间推理
        ascending = list(reversed(all_items))
        assert len(ascending) == 5

        pivot = ascending[1]  # t1
        pivot_cursor = encode_cursor(datetime.fromisoformat(pivot["occurredAt"]), pivot["id"])

        r = client.get("/api/v1/events", params={"after": pivot_cursor, "limit": 100})
        items = r.json()["data"]["items"]

        # 应只拿到 t2/t3/t4
        assert [i["id"] for i in items] == [i["id"] for i in ascending[2:]]
        times = [datetime.fromisoformat(i["occurredAt"]) for i in items]
        assert times == sorted(times), "after 语义下按时间升序返回，便于顺序处理"

        # ---- 模拟两次轮询之间产生的新数据 ----
        newer = datetime.fromisoformat(ascending[-1]["occurredAt"]) + timedelta(seconds=1)
        db_session.add(
            Event(
                id="evt_new000000000000000000001",
                occurred_at=newer,
                received_at=newer,
                source=EventSource.PROBE.value,
                resource_id=CTR,
                type="container.restart",
                severity=Severity.WARNING.value,
            )
        )
        db_session.commit()

        # 用上一批的最后一条作为新游标
        next_cursor = encode_cursor(
            datetime.fromisoformat(items[-1]["occurredAt"]), items[-1]["id"]
        )
        r2 = client.get("/api/v1/events", params={"after": next_cursor, "limit": 100})
        items2 = r2.json()["data"]["items"]

        assert [i["id"] for i in items2] == ["evt_new000000000000000000001"], (
            "只应拿到新增那一条，既不重复也不漏"
        )
        assert not ({i["id"] for i in items} & {i["id"] for i in items2})

    def test_after_cursor_returns_empty_when_nothing_new(
        self, client: TestClient, db_session: Session
    ) -> None:
        """没有新数据时返回空列表（而不是报错）—— 轮询大多时候是空响应。"""
        from app.api.schemas.common import encode_cursor

        seed_resources(db_session)
        seed_events(db_session, 3)
        db_session.commit()

        newest = client.get("/api/v1/events").json()["data"]["items"][0]
        cursor = encode_cursor(datetime.fromisoformat(newest["occurredAt"]), newest["id"])
        items = client.get("/api/v1/events", params={"after": cursor}).json()["data"]["items"]
        assert items == []

    def test_cursor_and_after_are_mutually_exclusive(self, client: TestClient) -> None:
        r = client.get("/api/v1/events", params={"cursor": "x", "after": "y"})
        assert r.status_code == 400
        assert "互斥" in r.json()["error"]["message"]

    def test_invalid_cursor_returns_400(self, client: TestClient) -> None:
        r = client.get("/api/v1/events", params={"cursor": "!!!not-base64!!!"})
        assert r.status_code == 400
        assert "游标非法" in r.json()["error"]["message"]

    def test_empty_result(self, client: TestClient, db_session: Session) -> None:
        body = client.get("/api/v1/events").json()
        assert body["data"]["items"] == []
        assert body["meta"]["page"]["count"] == 0


# ================================================================ 告警


class TestAlertQuery:
    def test_list_returns_counts(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 3)
        seed_alert(db_session, "alert_1", evidence=[ids[0]])
        seed_alert(
            db_session,
            "alert_2",
            evidence=[ids[1]],
            state=AlertState.RESOLVED.value,
            severity=Severity.WARNING.value,
        )
        db_session.commit()

        body = client.get("/api/v1/alerts").json()
        data = body["data"]
        assert len(data["items"]) == 2
        # 状态计数是**全量**的，不随筛选变化
        assert data["stateCounts"]["firing"] == 1
        assert data["stateCounts"]["resolved"] == 1
        assert data["stateCounts"]["acked"] == 0
        assert data["stateCounts"]["silenced"] == 0
        # 活动告警的严重级别分布只统计 firing/acked
        assert data["activeSeverityCounts"]["critical"] == 1
        assert data["activeSeverityCounts"]["warning"] == 0

    def test_alert_shape_includes_evidence(self, client: TestClient, db_session: Session) -> None:
        """`evidenceEventIds` 必须暴露 —— 可解释性的基础。"""
        seed_resources(db_session)
        ids = seed_events(db_session, 2)
        seed_alert(db_session, "alert_1", evidence=[ids[0], ids[1]])
        db_session.commit()

        alert = client.get("/api/v1/alerts").json()["data"]["items"][0]
        assert alert["evidenceEventIds"] == [ids[0], ids[1]]
        assert alert["ruleId"] == "R-CONTAINER-OOM-001"
        assert alert["aggregationKey"].startswith("container.oom_killed")
        assert alert["firstFiredAt"].endswith("+00:00")

    def test_state_filter(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 2)
        seed_alert(db_session, "a1", evidence=[ids[0]])
        seed_alert(db_session, "a2", evidence=[ids[1]], state=AlertState.RESOLVED.value)
        db_session.commit()

        items = client.get("/api/v1/alerts", params={"state": "firing"}).json()["data"]["items"]
        assert [i["id"] for i in items] == ["a1"]

    def test_invalid_state_returns_400(self, client: TestClient) -> None:
        r = client.get("/api/v1/alerts", params={"state": "pending"})
        assert r.status_code == 400

    def test_evidence_event_id_reverse_lookup(
        self, client: TestClient, db_session: Session
    ) -> None:
        """按证据事件反查告警（运维从症状倒推）。"""
        seed_resources(db_session)
        ids = seed_events(db_session, 3)
        seed_alert(db_session, "a1", evidence=[ids[0]])
        seed_alert(db_session, "a2", evidence=[ids[1]])
        db_session.commit()

        items = client.get("/api/v1/alerts", params={"evidenceEventId": ids[0]}).json()["data"][
            "items"
        ]
        assert [i["id"] for i in items] == ["a1"]

    def test_get_single_alert_and_404(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 1)
        seed_alert(db_session, "a1", evidence=[ids[0]])
        db_session.commit()

        assert client.get("/api/v1/alerts/a1").status_code == 200
        r = client.get("/api/v1/alerts/nope")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "ALERT_NOT_FOUND"


class TestAlertEvidence:
    def test_evidence_expansion(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 3)
        seed_alert(db_session, "a1", evidence=[ids[0], ids[2]])
        db_session.commit()

        data = client.get("/api/v1/alerts/a1/evidence").json()["data"]
        assert data["alertId"] == "a1"
        assert data["evidenceCount"] == 2
        assert [e["id"] for e in data["events"]] == [ids[0], ids[2]], "保持证据顺序"
        assert data["missingEventIds"] == []

    def test_missing_evidence_is_reported_not_hidden(
        self, client: TestClient, db_session: Session
    ) -> None:
        """**证据被清理时必须报告缺失** —— 静默少返回几条会让"可解释"打折。"""
        seed_resources(db_session)
        ids = seed_events(db_session, 2)
        seed_alert(db_session, "a1", evidence=[ids[0], "evt_已删除"])
        db_session.commit()

        data = client.get("/api/v1/alerts/a1/evidence").json()["data"]
        assert data["evidenceCount"] == 2
        assert len(data["events"]) == 1
        assert data["missingEventIds"] == ["evt_已删除"]

    def test_event_related_alerts(self, client: TestClient, db_session: Session) -> None:
        """反向链路：从事件出发看它被哪些告警采纳。"""
        seed_resources(db_session)
        ids = seed_events(db_session, 3)
        seed_alert(db_session, "a1", evidence=[ids[0]])
        seed_alert(db_session, "a2", evidence=[ids[0], ids[1]])
        db_session.commit()

        data = client.get(f"/api/v1/events/{ids[0]}/related-alerts").json()["data"]
        assert {a["id"] for a in data["alerts"]} == {"a1", "a2"}


class TestAlertStateMachine:
    def test_ack_changes_state(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 1)
        seed_alert(db_session, "a1", evidence=[ids[0]])
        db_session.commit()

        body = client.post("/api/v1/alerts/a1/actions", json={"action": "ack"}).json()
        assert body["data"]["changed"] is True
        assert body["data"]["alert"]["state"] == "acked"

    def test_repeat_ack_is_not_an_error(self, client: TestClient, db_session: Session) -> None:
        """重复确认返回 `changed=False`，**不能报错** —— 运维重复点击不应弹错。"""
        seed_resources(db_session)
        ids = seed_events(db_session, 1)
        seed_alert(db_session, "a1", evidence=[ids[0]], state=AlertState.ACKED.value)
        db_session.commit()

        r = client.post("/api/v1/alerts/a1/actions", json={"action": "ack"})
        assert r.status_code == 200
        assert r.json()["data"]["changed"] is False
        assert "无需变更" in r.json()["data"]["message"]

    def test_resolve_sets_resolved_at(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 1)
        seed_alert(db_session, "a1", evidence=[ids[0]])
        db_session.commit()

        alert = client.post("/api/v1/alerts/a1/actions", json={"action": "resolve"}).json()["data"][
            "alert"
        ]
        assert alert["state"] == "resolved"
        assert alert["resolvedAt"] is not None

    def test_refire_clears_resolved_at(self, client: TestClient, db_session: Session) -> None:
        """复发时必须清掉恢复时间，否则详情页会显示"已恢复但仍在触发"。"""
        seed_resources(db_session)
        ids = seed_events(db_session, 1)
        seed_alert(db_session, "a1", evidence=[ids[0]], state=AlertState.RESOLVED.value)
        db_session.commit()

        alert = client.post("/api/v1/alerts/a1/actions", json={"action": "unsilence"}).json()[
            "data"
        ]["alert"]
        assert alert["state"] == "firing"
        assert alert["resolvedAt"] is None

    def test_illegal_transition_returns_409(self, client: TestClient, db_session: Session) -> None:
        """非法迁移是**冲突**（409）而不是参数错误 —— 请求本身合法，是状态不允许。"""
        seed_resources(db_session)
        ids = seed_events(db_session, 1)
        # resolved 不能再被 ack（允许集合里没有 acked）
        seed_alert(db_session, "a1", evidence=[ids[0]], state=AlertState.RESOLVED.value)
        db_session.commit()

        r = client.post("/api/v1/alerts/a1/actions", json={"action": "ack"})
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "CONFLICT"
        assert r.json()["error"]["details"]["currentState"] == "resolved"

    def test_silence_records_until(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 1)
        seed_alert(db_session, "a1", evidence=[ids[0]])
        db_session.commit()

        alert = client.post(
            "/api/v1/alerts/a1/actions",
            json={"action": "silence", "silenceSeconds": 600},
        ).json()["data"]["alert"]
        assert alert["state"] == "silenced"
        assert "silencedUntil" in alert["labels"]

    def test_unsilence_clears_marker(self, client: TestClient, db_session: Session) -> None:
        """取消静默必须清掉标记，避免残留字段误导前端。"""
        seed_resources(db_session)
        ids = seed_events(db_session, 1)
        seed_alert(db_session, "a1", evidence=[ids[0]], state=AlertState.SILENCED.value)
        db_session.commit()

        alert = client.post("/api/v1/alerts/a1/actions", json={"action": "unsilence"}).json()[
            "data"
        ]["alert"]
        assert alert["state"] == "firing"
        assert "silencedUntil" not in alert["labels"]

    def test_invalid_action_returns_400(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 1)
        seed_alert(db_session, "a1", evidence=[ids[0]])
        db_session.commit()
        r = client.post("/api/v1/alerts/a1/actions", json={"action": "explode"})
        assert r.status_code == 400

    def test_action_on_unknown_alert_returns_404(self, client: TestClient) -> None:
        r = client.post("/api/v1/alerts/nope/actions", json={"action": "ack"})
        assert r.status_code == 404


class TestBulkAlertActions:
    def test_bulk_ack(self, client: TestClient, db_session: Session) -> None:
        seed_resources(db_session)
        ids = seed_events(db_session, 3)
        for i in range(3):
            seed_alert(db_session, f"a{i}", evidence=[ids[i]])
        db_session.commit()

        r = client.post(
            "/api/v1/alerts/actions",
            params=[("ids", "a0"), ("ids", "a1"), ("ids", "a2")],
            json={"action": "ack"},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["requested"] == 3
        assert data["changed"] == 3
        assert data["failed"] == 0

    def test_bulk_reports_per_item_results(self, client: TestClient, db_session: Session) -> None:
        """**逐条报告**：一次 20 条里失败 1 条，运维需要知道是哪条。"""
        seed_resources(db_session)
        ids = seed_events(db_session, 2)
        seed_alert(db_session, "a0", evidence=[ids[0]])
        seed_alert(db_session, "a1", evidence=[ids[1]], state=AlertState.RESOLVED.value)
        db_session.commit()

        r = client.post(
            "/api/v1/alerts/actions",
            params=[("ids", "a0"), ("ids", "a1"), ("ids", "nope")],
            json={"action": "ack"},
        )
        data = r.json()["data"]
        assert data["requested"] == 3
        assert data["changed"] == 1, "a0 应被确认"
        assert data["failed"] == 2, "a1 非法迁移 + nope 不存在"
        by_id = {x["alertId"]: x["result"] for x in data["results"]}
        assert by_id["a0"] == "changed"
        assert by_id["a1"] == "illegal_transition"
        assert by_id["nope"] == "not_found"

    def test_bulk_empty_ids_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/alerts/actions", json={"action": "ack"})
        assert r.status_code == 400

    def test_bulk_invalid_action_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/alerts/actions", params=[("ids", "a0")], json={"action": "nope"})
        assert r.status_code == 400
