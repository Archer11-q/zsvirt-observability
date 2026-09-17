"""ORM 模型与存储层测试（真实 PostgreSQL）。

这些测试验证的是**已冻结的契约语义在持久化层的落地**，
每一条都对应一个明确的决策编号或契约条款：

| 测试 | 依据 |
|---|---|
| `status` 与 `observability` 独立、不互相覆盖 | D-032 |
| `Alert` 证据必填（数据库层强制） | `DATA_MODEL.md` §5.2 |
| upsert 不覆盖来源系统的 `status` | §4.1 + D-032 |
| `first_seen_at` 不被后续 upsert 推后 | §4.1 |
| 资源不物理删除，只标 `stale`/`gone` | §7 |
| 占位资源用于孤儿事件 | 成员 A 的 Q1 |
| `Diagnosis.confidence` 值域约束 | §5.3 |
| 邻接表与 `parent_id` 同步 | §6 |

需要真实数据库；不可用时由 `conftest` 显式 skip。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import (
    AlertState,
    EventSource,
    Observability,
    ResourceKind,
    ResourceStatus,
    Severity,
)
from app.graph.algorithms import build_child_index, build_parent_index
from app.graph.repository import (
    count_unresolved,
    ensure_edge,
    list_roots,
    load_graph,
    mark_gone,
    mark_stale,
    placeholder_resource_id,
    set_status,
    upsert_resource,
)
from app.models import Alert, Diagnosis, Event, IngestBatch, Resource

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


def as_utc(moment: datetime) -> datetime:
    """同一时刻的 UTC 表示。

    PostgreSQL 会按**会话时区**返回带时区的 datetime（本机是 Asia/Shanghai），
    而 20:00+08:00 与 12:00+00:00 是**同一时刻**。直接断言 tzinfo 不同的
    aware datetime 容易写出"看起来失败、其实数据正确"的用例，
    因此统一转成 UTC 再比。
    """
    return moment.astimezone(UTC)


HOST = "host:zsvirt:h1"
GPU = "gpu:zsvirt:g0"
VGPU = "vgpu:zsvirt:v0"
VM = "vm:zsvirt:vm0"
CTR = "container:probe:probe-x:web-0"
AIS = "ai_service:probe:probe-x:vllm"


def make_chain(session: Session) -> None:
    """建立 HOST → GPU → VGPU → VM → CTR → AIS 的完整资源链。"""
    upsert_resource(
        session,
        resource_id=HOST,
        kind=ResourceKind.HOST.value,
        name="node-01",
        status=ResourceStatus.RUNNING.value,
        seen_at=NOW,
    )
    upsert_resource(
        session,
        resource_id=GPU,
        kind=ResourceKind.GPU.value,
        parent_id=HOST,
        status=ResourceStatus.RUNNING.value,
        seen_at=NOW,
        attributes={"memory": 24 * 1024**3},
    )
    upsert_resource(
        session,
        resource_id=VGPU,
        kind=ResourceKind.VGPU.value,
        parent_id=GPU,
        status=ResourceStatus.RUNNING.value,
        seen_at=NOW,
    )
    upsert_resource(
        session,
        resource_id=VM,
        kind=ResourceKind.VM.value,
        parent_id=VGPU,
        status=ResourceStatus.RUNNING.value,
        seen_at=NOW,
    )
    upsert_resource(
        session,
        resource_id=CTR,
        kind=ResourceKind.CONTAINER.value,
        parent_id=VM,
        status=ResourceStatus.RUNNING.value,
        seen_at=NOW,
        attributes={"image": "vllm/vllm-openai"},
    )
    upsert_resource(
        session,
        resource_id=AIS,
        kind=ResourceKind.AI_SERVICE.value,
        parent_id=CTR,
        status=ResourceStatus.RUNNING.value,
        seen_at=NOW,
    )
    ensure_edge(session, HOST, GPU)
    ensure_edge(session, GPU, VGPU)
    ensure_edge(session, VGPU, VM)
    ensure_edge(session, VM, CTR)
    ensure_edge(session, CTR, AIS)
    session.flush()


# ---------------------------------------------------------------- 资源基础


class TestResourceBasics:
    def test_upsert_inserts_then_updates(self, db_session: Session) -> None:
        r1 = upsert_resource(
            db_session,
            resource_id=VM,
            kind=ResourceKind.VM.value,
            name="vm0",
            status=ResourceStatus.RUNNING.value,
            seen_at=NOW,
        )
        assert as_utc(r1.first_seen_at) == NOW
        assert r1.status == ResourceStatus.RUNNING.value

        later = NOW + timedelta(minutes=5)
        r2 = upsert_resource(
            db_session,
            resource_id=VM,
            kind=ResourceKind.VM.value,
            name="vm0-renamed",
            seen_at=later,
        )
        assert as_utc(r2.first_seen_at) == NOW, "first_seen_at 不得被后续 upsert 推后"
        assert as_utc(r2.last_seen_at) == later, "last_seen_at 应推进到本次观测时刻"
        assert r2.name == "vm0-renamed"

    def test_upsert_does_not_clobber_source_status(self, db_session: Session) -> None:
        """**核心不变量**：未显式传 status 时不得覆盖既有业务状态。

        若这里失败，说明 B 会用默认值把来源系统的状态冲掉
        （例如"ZSvirt 报 running 被探针 upsert 写成 unknown"），
        这是静默数据损坏。
        """
        upsert_resource(
            db_session,
            resource_id=VM,
            kind=ResourceKind.VM.value,
            status=ResourceStatus.RUNNING.value,
            seen_at=NOW,
        )
        # 探针后续上报，不带 status
        r = upsert_resource(
            db_session,
            resource_id=VM,
            kind=ResourceKind.VM.value,
            seen_at=NOW + timedelta(seconds=30),
        )
        assert r.status == ResourceStatus.RUNNING.value, (
            "status 是来源系统的字段，B 不得在未显式给出时覆盖它"
        )

    def test_set_status_is_explicit_path(self, db_session: Session) -> None:
        upsert_resource(db_session, resource_id=VM, kind=ResourceKind.VM.value, seen_at=NOW)
        r = set_status(db_session, VM, ResourceStatus.STOPPED.value)
        assert r is not None
        assert r.status == ResourceStatus.STOPPED.value

    def test_status_and_observability_are_independent(self, db_session: Session) -> None:
        """D-032：VM 可同时是 `status=running` 与 `observability=stale`。"""
        upsert_resource(
            db_session,
            resource_id=VM,
            kind=ResourceKind.VM.value,
            status=ResourceStatus.RUNNING.value,
            seen_at=NOW,
        )
        # 让它变陈旧
        affected = mark_stale(db_session, older_than_seconds=60, now=NOW + timedelta(minutes=10))
        assert affected == 1

        r = db_session.get(Resource, VM)
        assert r is not None
        assert r.status == ResourceStatus.RUNNING.value, "业务状态不受观测状态影响"
        assert r.observability == Observability.STALE.value, "观测状态独立变化"

    def test_kind_check_constraint(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError):
            db_session.execute(
                text(
                    "INSERT INTO resource (id, kind, status, observability, "
                    "first_seen_at, last_seen_at, attributes, labels, is_placeholder) "
                    "VALUES ('x:y:z', 'database', 'running', 'active', now(), now(), "
                    "'{}'::jsonb, '{}'::jsonb, false)"
                )
            )


# ---------------------------------------------------------------- 资源图


class TestGraphPersistence:
    def test_chain_round_trip(self, db_session: Session) -> None:
        make_chain(db_session)
        nodes, edges = load_graph(db_session, root_id=HOST, max_depth=10)
        ids = {n.id for n in nodes}
        assert ids == {HOST, GPU, VGPU, VM, CTR, AIS}

        parents = build_parent_index(edges)
        children = build_child_index(edges)
        assert parents[GPU] == HOST
        assert parents[AIS] == CTR
        assert children[VM] == [CTR]

    def test_load_graph_from_leaf_reaches_upstream(self, db_session: Session) -> None:
        """从叶子加载应能向上追溯到宿主机（诊断锚点多为服务层）。"""
        make_chain(db_session)
        nodes, _ = load_graph(db_session, root_id=AIS, max_depth=10)
        assert {n.id for n in nodes} == {HOST, GPU, VGPU, VM, CTR, AIS}

    def test_max_depth_limits_expansion(self, db_session: Session) -> None:
        make_chain(db_session)
        nodes, _ = load_graph(db_session, root_id=HOST, max_depth=1)
        assert {n.id for n in nodes} == {HOST, GPU}

    def test_list_roots_returns_host(self, db_session: Session) -> None:
        make_chain(db_session)
        roots = list_roots(db_session)
        assert [r.id for r in roots] == [HOST]

    def test_ensure_edge_syncs_parent_id(self, db_session: Session) -> None:
        upsert_resource(db_session, resource_id=HOST, kind=ResourceKind.HOST.value, seen_at=NOW)
        upsert_resource(db_session, resource_id=GPU, kind=ResourceKind.GPU.value, seen_at=NOW)
        ensure_edge(db_session, HOST, GPU)
        assert db_session.get(Resource, GPU).parent_id == HOST

    def test_ensure_edge_is_idempotent(self, db_session: Session) -> None:
        upsert_resource(db_session, resource_id=HOST, kind=ResourceKind.HOST.value, seen_at=NOW)
        upsert_resource(db_session, resource_id=GPU, kind=ResourceKind.GPU.value, seen_at=NOW)
        ensure_edge(db_session, HOST, GPU)
        ensure_edge(db_session, HOST, GPU)  # 不应报错或产生重复
        rows = db_session.execute(
            text("SELECT count(*) FROM resource_edge WHERE parent_id=:p AND child_id=:c"),
            {"p": HOST, "c": GPU},
        ).scalar()
        assert rows == 1

    def test_self_loop_rejected(self, db_session: Session) -> None:
        upsert_resource(db_session, resource_id=VM, kind=ResourceKind.VM.value, seen_at=NOW)
        from app.graph.algorithms import GraphError

        with pytest.raises(GraphError, match="self loop"):
            ensure_edge(db_session, VM, VM)

    def test_missing_endpoint_rejected(self, db_session: Session) -> None:
        from app.graph.algorithms import GraphError

        upsert_resource(db_session, resource_id=VM, kind=ResourceKind.VM.value, seen_at=NOW)
        with pytest.raises(GraphError, match="not found"):
            ensure_edge(db_session, VM, "gpu:zsvirt:nonexistent")

    def test_kinds_filter_applies_to_result_not_traversal(self, db_session: Session) -> None:
        """**回归测试**：`kinds` 必须过滤返回的节点，而不是先剪掉遍历所需的节点。

        早期实现把 kind 过滤放在 SQL 层，于是从 HOST 出发时图里只剩 HOST
        自己（HOST 不是 GPU），遍历找不到邻居 → 返回空集。
        """
        make_chain(db_session)
        nodes, edges = load_graph(
            db_session, root_id=HOST, max_depth=10, kinds=[ResourceKind.GPU.value]
        )
        assert {n.id for n in nodes} == {GPU}
        assert edges == [], "只剩一个节点时不应残留指向被过滤节点的边"

    def test_kinds_filter_can_return_several(self, db_session: Session) -> None:
        make_chain(db_session)
        nodes, _ = load_graph(
            db_session,
            root_id=HOST,
            max_depth=10,
            kinds=[ResourceKind.GPU.value, ResourceKind.VGPU.value],
        )
        assert {n.id for n in nodes} == {GPU, VGPU}

    def test_kinds_filter_without_root(self, db_session: Session) -> None:
        make_chain(db_session)
        nodes, _ = load_graph(db_session, kinds=[ResourceKind.CONTAINER.value])
        assert {n.id for n in nodes} == {CTR}


# ---------------------------------------------------------------- 生命周期


class TestLifecycle:
    def test_mark_stale_then_gone(self, db_session: Session) -> None:
        make_chain(db_session)
        later = NOW + timedelta(minutes=30)
        assert mark_stale(db_session, older_than_seconds=600, now=later) == 6
        assert db_session.get(Resource, VM).observability == Observability.STALE.value

        much_later = NOW + timedelta(hours=3)
        assert mark_gone(db_session, older_than_seconds=3600, now=much_later) == 6
        assert db_session.get(Resource, VM).observability == Observability.GONE.value

    def test_no_physical_delete_after_gone(self, db_session: Session) -> None:
        """资源被标 gone 后**仍在表中** —— 删除会让历史事件变孤儿。"""
        make_chain(db_session)
        mark_stale(db_session, older_than_seconds=1, now=NOW + timedelta(hours=1))
        mark_gone(db_session, older_than_seconds=1, now=NOW + timedelta(hours=2))
        remaining = db_session.execute(text("SELECT count(*) FROM resource")).scalar()
        assert remaining == 6, "资源不得被物理删除"

    def test_reobservation_reactivates(self, db_session: Session) -> None:
        make_chain(db_session)
        mark_stale(db_session, older_than_seconds=1, now=NOW + timedelta(hours=1))
        assert db_session.get(Resource, VM).observability == Observability.STALE.value

        upsert_resource(
            db_session,
            resource_id=VM,
            kind=ResourceKind.VM.value,
            seen_at=NOW + timedelta(hours=2),
        )
        assert db_session.get(Resource, VM).observability == Observability.ACTIVE.value

    def test_gone_excluded_from_graph_by_default(self, db_session: Session) -> None:
        make_chain(db_session)
        mark_stale(db_session, older_than_seconds=1, now=NOW + timedelta(hours=1))
        mark_gone(db_session, older_than_seconds=1, now=NOW + timedelta(hours=2))
        nodes, _ = load_graph(db_session, root_id=HOST, max_depth=10)
        assert nodes == [], "默认不返回 gone 资源"
        nodes2, _ = load_graph(db_session, root_id=HOST, max_depth=10, include_stale=True)
        assert len(nodes2) == 6, "include_stale=True 时应返回（gone 也包含，供排障）"


# ---------------------------------------------------------------- 占位资源


class TestPlaceholderResource:
    def test_placeholder_for_orphan_event(self, db_session: Session) -> None:
        """成员 A 的 Q1：孤儿引用挂占位节点，不丢数据、不报错。"""
        pid = placeholder_resource_id(ResourceKind.CONTAINER.value, "probe", "probe-x:web-9")
        upsert_resource(
            db_session,
            resource_id=pid,
            kind=ResourceKind.UNRESOLVED.value,
            name="web-9",
            seen_at=NOW,
            is_placeholder=True,
        )
        assert db_session.get(Resource, pid) is not None
        assert count_unresolved(db_session) == 1

    def test_unresolved_count_is_health_signal(self, db_session: Session) -> None:
        assert count_unresolved(db_session) == 0
        upsert_resource(
            db_session,
            resource_id=placeholder_resource_id("container", "probe", "p:web-1"),
            kind=ResourceKind.UNRESOLVED.value,
            seen_at=NOW,
            is_placeholder=True,
        )
        upsert_resource(
            db_session,
            resource_id=placeholder_resource_id("container", "probe", "p:web-2"),
            kind=ResourceKind.UNRESOLVED.value,
            seen_at=NOW,
            is_placeholder=True,
        )
        assert count_unresolved(db_session) == 2


# ---------------------------------------------------------------- 事件


class TestEventPersistence:
    def test_insert_event(self, db_session: Session) -> None:
        make_chain(db_session)
        ev = Event(
            id="evt_01J0000000000000000000001",
            occurred_at=NOW - timedelta(seconds=30),
            received_at=NOW,
            source=EventSource.PROBE.value,
            resource_id=CTR,
            type="container.oom_killed",
            severity=Severity.CRITICAL.value,
            message="container killed by OOM",
            metrics={"exit_code": 137},
            raw={"docker": {"image": "vllm"}},
        )
        db_session.add(ev)
        db_session.flush()
        loaded = db_session.get(Event, ev.id)
        assert loaded is not None
        assert loaded.type == "container.oom_killed"
        assert loaded.metrics["exit_code"] == 137

    def test_event_severity_check_constraint(self, db_session: Session) -> None:
        make_chain(db_session)
        db_session.add(
            Event(
                id="evt_bad",
                occurred_at=NOW,
                received_at=NOW,
                source=EventSource.PROBE.value,
                resource_id=CTR,
                type="x.y",
                severity="fatal",  # 非法级别
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_event_resource_fk_restricts_delete(self, db_session: Session) -> None:
        """有事件引用时不得删除资源（RESTRICT）—— 保护可追溯性。"""
        make_chain(db_session)
        db_session.add(
            Event(
                id="evt_01J0000000000000000000002",
                occurred_at=NOW,
                received_at=NOW,
                source=EventSource.PROBE.value,
                resource_id=CTR,
                type="container.restart",
                severity=Severity.WARNING.value,
            )
        )
        db_session.flush()
        with pytest.raises(IntegrityError):
            db_session.execute(text("DELETE FROM resource WHERE id=:r"), {"r": CTR})


# ---------------------------------------------------------------- 告警


class TestAlertPersistence:
    def _alert(self, **overrides) -> Alert:
        params = {
            "id": "alert_01J0000000000000000000001",
            "rule_id": "R-CONTAINER-OOM-001",
            "resource_id": CTR,
            "severity": Severity.CRITICAL.value,
            "state": AlertState.FIRING.value,
            "first_fired_at": NOW,
            "last_fired_at": NOW,
            "count": 1,
            "evidence_event_ids": ["evt_01J0000000000000000000001"],
            "aggregation_key": "container.oom_killed:ctr:web-0",
        }
        params.update(overrides)
        return Alert(**params)

    def test_alert_with_evidence_ok(self, db_session: Session) -> None:
        make_chain(db_session)
        db_session.add(self._alert())
        db_session.flush()
        loaded = db_session.get(Alert, "alert_01J0000000000000000000001")
        assert loaded is not None
        assert loaded.evidence_event_ids == ["evt_01J0000000000000000000001"]

    def test_alert_without_evidence_is_rejected(self, db_session: Session) -> None:
        """**红线**：没有证据的告警不允许存在（`DATA_MODEL.md` §5.2）。

        数据库层强制 —— 即使应用层漏了校验，也无法写入无证据告警。
        """
        make_chain(db_session)
        db_session.add(self._alert(evidence_event_ids=[]))
        with pytest.raises(IntegrityError, match="ck_alert_evidence_required"):
            db_session.flush()

    def test_alert_count_must_be_positive(self, db_session: Session) -> None:
        make_chain(db_session)
        db_session.add(self._alert(count=0))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_alert_state_check_constraint(self, db_session: Session) -> None:
        make_chain(db_session)
        db_session.add(self._alert(state="pending"))
        with pytest.raises(IntegrityError):
            db_session.flush()


# ---------------------------------------------------------------- 诊断


class TestDiagnosisPersistence:
    def _diag(self, **overrides) -> Diagnosis:
        params = {
            "id": "diag_01J0000000000000000000001",
            "created_at": NOW,
            "trigger": {"alertId": "alert_01J0000000000000000000001"},
            "root_cause": "GPU_MEMORY_EXHAUSTED",
            "confidence": 0.80,
            "confidence_breakdown": [
                {"ruleId": "R-GPU-MEM-001", "contribution": 0.55, "observed": "98%"},
                {"ruleId": "R-INFER-TIMEOUT-002", "contribution": 0.25, "observed": "x12"},
            ],
            "affected_resources": [GPU, VGPU, VM, CTR],
            "potentially_affected": [HOST],
            "on_chain": [],
            "evidence": [{"type": "metric", "name": "gpu_memory_usage", "value": "98%"}],
            "recommendation": [{"code": "REDUCE_CONCURRENCY", "text": "降低并发"}],
            "rule_set_version": "rs-0.1.0",
            "notes": [],
        }
        params.update(overrides)
        return Diagnosis(**params)

    def test_diagnosis_round_trip(self, db_session: Session) -> None:
        make_chain(db_session)
        db_session.add(self._diag())
        db_session.flush()
        loaded = db_session.get(Diagnosis, "diag_01J0000000000000000000001")
        assert loaded is not None
        assert loaded.root_cause == "GPU_MEMORY_EXHAUSTED"
        assert loaded.rule_set_version == "rs-0.1.0"
        # confidence 必须可由 breakdown 复算
        total = sum(h["contribution"] for h in loaded.confidence_breakdown)
        assert round(total, 4) == loaded.confidence

    def test_potentially_affected_stays_separate(self, db_session: Session) -> None:
        """C 要求 D-075：两个字段必须分开存储，不得混入。"""
        make_chain(db_session)
        db_session.add(self._diag())
        db_session.flush()
        loaded = db_session.get(Diagnosis, "diag_01J0000000000000000000001")
        assert HOST not in loaded.affected_resources
        assert HOST in loaded.potentially_affected

    def test_confidence_range_enforced(self, db_session: Session) -> None:
        make_chain(db_session)
        db_session.add(self._diag(confidence=1.5))
        with pytest.raises(IntegrityError):
            db_session.flush()


# ---------------------------------------------------------------- 接入幂等


class TestIngestBatch:
    def test_batch_round_trip(self, db_session: Session) -> None:
        make_chain(db_session)
        b = IngestBatch(
            batch_id="01J0000000000000000000001",
            agent_id="probe-3f2a9c10",
            vm_id=VM,
            agent_version="0.1.0",
            sent_at=NOW,
            received_at=NOW,
            accepted_resources=3,
            accepted_events=12,
            result={"accepted": {"resources": 3, "events": 12}},
        )
        db_session.add(b)
        db_session.flush()
        loaded = db_session.get(IngestBatch, "01J0000000000000000000001")
        assert loaded is not None
        assert loaded.result["accepted"]["events"] == 12

    def test_duplicate_batch_id_rejected_by_pk(self, db_session: Session) -> None:
        """主键冲突即幂等去重的第一道防线（应用层负责回放首次结果）。"""
        make_chain(db_session)
        db_session.add(IngestBatch(batch_id="dup-1", agent_id="a", received_at=NOW))
        db_session.flush()
        db_session.add(IngestBatch(batch_id="dup-1", agent_id="a", received_at=NOW))
        with pytest.raises(IntegrityError):
            db_session.flush()
