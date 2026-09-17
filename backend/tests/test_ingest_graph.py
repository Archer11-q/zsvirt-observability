"""A public-API test for graph edges built by **ingest**, not by hand.

Every existing topology test seeds its chain with `ensure_edge`, so the whole
suite passed while `POST /api/v1/ingest/batch` -- the only way real data arrives
-- produced resources with a `parent_id` and no edges. Topology answered with
disconnected nodes and zero edges, and nothing caught it.

A test that builds its own fixture cannot detect "the fixture is the only thing
that builds this". So this one ingests a batch and then asserts that the public
topology endpoint reports the relationships. It is deliberately small: its job is
to make that regression impossible to reintroduce, not to re-test topology.
"""

from __future__ import annotations

import _scenarios as sc
from fastapi.testclient import TestClient


def ingest(client: TestClient, payload: dict) -> dict:
    r = client.post("/api/v1/ingest/batch", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["data"]


class TestIngestBuildsTheGraph:
    def test_edges_exist_after_a_single_batch(self, client: TestClient) -> None:
        """一次上报后，拓扑必须返回真实连线。"""
        ingest(client, sc.scenario_container_oom())

        body = client.get("/api/v1/topology", params={"depth": 8}).json()["data"]
        assert body["edges"], (
            "上报后拓扑没有边 —— `upsert_resource` 写了 parent_id 但没维护 "
            "resource_edge，而 load_graph 只遍历边表"
        )
        assert not body["truncated"]

    def test_edge_chain_matches_the_reported_hierarchy(self, client: TestClient) -> None:
        ingest(client, sc.scenario_gpu_memory_exhausted())
        body = client.get("/api/v1/topology", params={"depth": 8}).json()["data"]

        by_child = {e["childId"]: e["parentId"] for e in body["edges"]}
        kinds = {n["id"]: n["kind"] for n in body["nodes"]}

        # 整条链必须连起来：HOST → gpu → vgpu → vm → container → ai_service → agent
        for node_id, kind in kinds.items():
            if kind in ("host",):
                continue
            assert node_id in by_child, f"{kind} 节点 {node_id} 没有父边"

        ids = set(kinds)
        assert len(body["edges"]) == len([i for i in ids if by_child.get(i) is not None])

    def test_workload_chain_resolution_works_on_ingested_data(
        self, client: TestClient
    ) -> None:
        """工作负载的链路归并依赖同一份边表。

        边表为空时它只能看到工作负载自己：计数恒为 0、GPU 永远解析不到。
        """
        ingest(client, sc.scenario_container_oom())

        r = client.get(
            "/api/v1/workloads",
            params={"since": "2026-09-16T00:00:00+00:00", "to": "2026-09-18T00:00:00+00:00"},
        )
        assert r.status_code == 200, r.text
        item = r.json()["data"]["items"][0]

        assert item["eventCount"] >= 2, (
            "容器与 AI 服务上的事件没有归并到工作负载 —— 链路解析没生效"
        )
        assert item["alertCount"] >= 1

    def test_diagnosis_sees_beyond_the_anchor(self, client: TestClient) -> None:
        """诊断的影响范围依赖图遍历：边表为空时只有锚点自己。"""
        ingest(client, sc.scenario_container_oom())
        diag = client.get("/api/v1/diagnoses").json()["data"]["items"][0]

        assert diag["affectedResources"] or diag["onChain"] or diag["potentiallyAffected"], (
            "三集全空说明图遍历没看到任何邻居"
        )
