"""拓扑端点测试（`GET /api/v1/topology`）。

契约：`docs/API_CONTRACT.md` §4.2；成员 C 的 Q12 要求上限 200 节点 / 400 边
且超限必须返回 `truncated: true`。

重点验证：

- 节点与边是**扁平分离**的形态（ECharts graph 需要）
- `status` 与 `observability` 是**两个独立字段**（D-032，C 的质疑）
- `staleness` 只在非 active 时给出
- 裁剪是**确定性**的，且不产生悬空边
- 参数非法返回 400，而不是静默忽略
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.enums import ResourceKind, ResourceStatus
from app.graph.repository import (
    MAX_TOPOLOGY_EDGES,
    MAX_TOPOLOGY_NODES,
    ensure_edge,
    mark_gone,
    mark_stale,
    trim_topology,
    upsert_resource,
)

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)

HOST = "host:zsvirt:h1"
GPU = "gpu:zsvirt:g0"
VGPU = "vgpu:zsvirt:v0"
VM = "vm:zsvirt:vm0"
CTR = "container:probe:probe-x:web-0"
AIS = "ai_service:probe:probe-x:vllm"


@pytest.fixture
def client(db_engine: Engine):
    """指向测试库的 TestClient。"""
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


def build_chain(session: Session, *, seen_at: datetime = NOW) -> None:
    """建立 HOST → GPU → VGPU → VM → CTR → AIS 链路。"""
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
        attributes={"memory": 24 * 1024**3, "model": "A10"},
    )
    upsert_resource(
        session,
        resource_id=VGPU,
        kind=ResourceKind.VGPU.value,
        parent_id=GPU,
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
    )
    upsert_resource(
        session,
        resource_id=VM,
        kind=ResourceKind.VM.value,
        parent_id=VGPU,
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
        attributes={"image": "vllm/vllm-openai"},
    )
    upsert_resource(
        session,
        resource_id=AIS,
        kind=ResourceKind.AI_SERVICE.value,
        parent_id=CTR,
        status=ResourceStatus.RUNNING.value,
        seen_at=seen_at,
    )
    for parent, child in ((HOST, GPU), (GPU, VGPU), (VGPU, VM), (VM, CTR), (CTR, AIS)):
        ensure_edge(session, parent, child)
    session.flush()


class TestTopologyShape:
    def test_returns_nodes_and_edges_separately(
        self, db_engine: Engine, db_session: Session, client: TestClient
    ) -> None:
        """扁平 nodes + edges（ECharts graph 形态），而不是嵌套树。"""
        build_chain(db_session)
        db_session.commit()

        r = client.get("/api/v1/topology", params={"rootId": HOST, "depth": 8})
        assert r.status_code == 200, r.text
        body = r.json()

        assert set(body.keys()) == {"data", "meta"}
        data = body["data"]
        assert {n["id"] for n in data["nodes"]} == {HOST, GPU, VGPU, VM, CTR, AIS}
        assert len(data["edges"]) == 5
        assert data["truncated"] is False
        assert data["droppedNodes"] == 0

    def test_meta_has_trace_id_and_timestamp(self, client: TestClient, db_session: Session) -> None:
        build_chain(db_session)
        db_session.commit()
        meta = client.get("/api/v1/topology").json()["meta"]
        assert meta["traceId"].startswith("req_")
        assert meta["generatedAt"]

    def test_edge_shape(self, client: TestClient, db_session: Session) -> None:
        build_chain(db_session)
        db_session.commit()
        edges = client.get("/api/v1/topology", params={"rootId": HOST}).json()["data"]["edges"]
        sample = next(e for e in edges if e["childId"] == GPU)
        assert sample == {"parentId": HOST, "childId": GPU, "relation": "hosts"}

    def test_node_carries_attributes(self, client: TestClient, db_session: Session) -> None:
        build_chain(db_session)
        db_session.commit()
        nodes = client.get("/api/v1/topology", params={"rootId": HOST}).json()["data"]["nodes"]
        gpu = next(n for n in nodes if n["id"] == GPU)
        assert gpu["name"] == "node-01" or gpu["attributes"]["model"] == "A10"
        assert gpu["kind"] == "gpu"
        assert gpu["parentId"] == HOST
        assert gpu["isPlaceholder"] is False


class TestStatusVsObservability:
    """D-032：两个字段独立，**`stale` 不得混进 `status`**（C 提出的问题）。"""

    def test_running_and_stale_coexist(self, client: TestClient, db_session: Session) -> None:
        build_chain(db_session)
        db_session.commit()
        # 让资源变陈旧
        mark_stale(db_session, older_than_seconds=60, now=NOW + timedelta(minutes=10))
        db_session.commit()

        nodes = client.get("/api/v1/topology", params={"rootId": HOST}).json()["data"]["nodes"]
        vm = next(n for n in nodes if n["id"] == VM)
        assert vm["status"] == "running", "业务状态不受观测状态影响"
        assert vm["observability"] == "stale", "观测状态独立变化"
        assert vm["status"] != "stale", "stale 绝不能出现在 status 里"

    def test_staleness_only_present_when_not_active(
        self, client: TestClient, db_session: Session
    ) -> None:
        """正常资源不显示 staleness —— 否则界面全是"0s 前"的噪声。"""
        build_chain(db_session)
        db_session.commit()
        nodes = client.get("/api/v1/topology", params={"rootId": HOST}).json()["data"]["nodes"]
        assert all(n["staleness"] is None for n in nodes)

        mark_stale(db_session, older_than_seconds=60, now=NOW + timedelta(minutes=10))
        db_session.commit()
        nodes = client.get("/api/v1/topology", params={"rootId": HOST}).json()["data"]["nodes"]
        assert all(n["staleness"] is not None for n in nodes)
        assert nodes[0]["staleness"].endswith(("s", "m", "h", "d"))

    def test_gone_excluded_by_default(self, client: TestClient, db_session: Session) -> None:
        build_chain(db_session)
        db_session.commit()
        mark_stale(db_session, older_than_seconds=1, now=NOW + timedelta(hours=1))
        mark_gone(db_session, older_than_seconds=1, now=NOW + timedelta(hours=2))
        db_session.commit()

        assert client.get("/api/v1/topology").json()["data"]["nodes"] == []
        with_stale = client.get("/api/v1/topology", params={"includeStale": True}).json()["data"][
            "nodes"
        ]
        assert len(with_stale) == 6


class TestFiltering:
    def test_kinds_filter(self, client: TestClient, db_session: Session) -> None:
        build_chain(db_session)
        db_session.commit()
        data = client.get("/api/v1/topology", params={"rootId": HOST, "kinds": "gpu,vgpu"}).json()[
            "data"
        ]
        assert {n["id"] for n in data["nodes"]} == {GPU, VGPU}

    def test_invalid_kind_returns_400(self, client: TestClient, db_session: Session) -> None:
        """非法枚举**不静默忽略** —— 否则前端会以为"没有这种资源"。"""
        r = client.get("/api/v1/topology", params={"kinds": "database"})
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "INVALID_ARGUMENT"
        assert (
            "database" not in r.json()["error"]["message"]
            or "允许值" in r.json()["error"]["message"]
        )

    def test_depth_limits_traversal(self, client: TestClient, db_session: Session) -> None:
        build_chain(db_session)
        db_session.commit()
        nodes = client.get("/api/v1/topology", params={"rootId": HOST, "depth": 1}).json()["data"][
            "nodes"
        ]
        assert {n["id"] for n in nodes} == {HOST, GPU}

    def test_depth_over_max_returns_422(self, client: TestClient) -> None:
        r = client.get("/api/v1/topology", params={"depth": 99})
        assert r.status_code == 422

    def test_unknown_root_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/topology", params={"rootId": "vm:zsvirt:nope"})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert r.json()["error"]["traceId"]

    def test_rootless_returns_all(self, client: TestClient, db_session: Session) -> None:
        build_chain(db_session)
        db_session.commit()
        nodes = client.get("/api/v1/topology").json()["data"]["nodes"]
        assert len(nodes) == 6


class TestTrimming:
    def test_trim_is_deterministic(self) -> None:
        """同样输入两次裁剪结果必须一致 —— 否则前端会看到拓扑跳变。"""
        from app.models import Resource

        nodes = [
            Resource(
                id=f"task:probe:p:t{i:03d}",
                kind="task",
                observability="active",
                status="running",
                last_seen_at=NOW,
                first_seen_at=NOW,
            )
            for i in range(30)
        ]
        nodes += [
            Resource(
                id="host:zsvirt:h1",
                kind="host",
                observability="active",
                status="running",
                last_seen_at=NOW,
                first_seen_at=NOW,
            )
        ]
        a = trim_topology(nodes, [], max_nodes=5)
        b = trim_topology(list(reversed(nodes)), [], max_nodes=5)
        assert [n.id for n in a[0]] == [n.id for n in b[0]], "裁剪必须确定性"

    def test_priority_keeps_backbone(self) -> None:
        """裁剪优先保留主干层级，而不是随机丢一半。"""
        from app.models import Resource

        nodes = [
            Resource(
                id=f"task:probe:p:t{i:03d}",
                kind="task",
                observability="active",
                status="running",
                last_seen_at=NOW,
                first_seen_at=NOW,
            )
            for i in range(10)
        ] + [
            Resource(
                id=HOST,
                kind="host",
                observability="active",
                status="running",
                last_seen_at=NOW,
                first_seen_at=NOW,
            ),
            Resource(
                id=GPU,
                kind="gpu",
                observability="active",
                status="running",
                last_seen_at=NOW,
                first_seen_at=NOW,
            ),
        ]
        kept, _, dropped, reason = trim_topology(nodes, [], max_nodes=2)
        assert {n.id for n in kept} == {HOST, GPU}, "宿主机与 GPU 应优先保留"
        assert dropped == 10
        assert reason is not None

    def test_no_dangling_edges(self) -> None:
        """裁剪后**不得残留悬空边** —— 前端会因找不到端点而布局异常。"""
        from app.graph.algorithms import Edge
        from app.models import Resource

        nodes = [
            Resource(
                id=f"container:probe:p:c{i:03d}",
                kind="container",
                observability="active",
                status="running",
                last_seen_at=NOW,
                first_seen_at=NOW,
            )
            for i in range(10)
        ]
        nodes.append(
            Resource(
                id=HOST,
                kind="host",
                observability="active",
                status="running",
                last_seen_at=NOW,
                first_seen_at=NOW,
            )
        )
        edges = [Edge(HOST, f"container:probe:p:c{i:03d}") for i in range(10)]

        kept, kept_edges, _, _ = trim_topology(nodes, edges, max_nodes=3)
        kept_ids = {n.id for n in kept}
        assert all(e.parent_id in kept_ids and e.child_id in kept_ids for e in kept_edges), (
            "不得残留端点已被裁掉的边"
        )

    def test_returns_truncated_flag_via_http(
        self, client: TestClient, db_session: Session, db_engine: Engine
    ) -> None:
        """超限必须显式告知（C 的 Q12：不能静默显示不完整拓扑）。"""
        for i in range(MAX_TOPOLOGY_NODES + 25):
            upsert_resource(
                db_session,
                resource_id=f"container:probe:p:c{i:04d}",
                kind=ResourceKind.CONTAINER.value,
                status=ResourceStatus.RUNNING.value,
                seen_at=NOW,
            )
        db_session.commit()

        data = client.get("/api/v1/topology").json()["data"]
        assert data["truncated"] is True
        assert data["droppedNodes"] == 25
        assert data["truncationReason"]
        assert len(data["nodes"]) == MAX_TOPOLOGY_NODES
        assert len(data["edges"]) <= MAX_TOPOLOGY_EDGES
