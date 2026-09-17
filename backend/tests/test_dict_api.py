"""字典端点测试（`GET /api/v1/dict`）。

契约：`docs/API_CONTRACT.md` §4.6.1；成员 C 的 Q14 要求
「字典端点须覆盖 F-01/F-02/F-03 全部码 + 中文文案」。

重点验证：

- 覆盖 C 点名的**全部 6 类枚举**（severity / alertState / resourceKind /
  resourceStatus / eventType / rootCause + recommendation）
- **含可读文案**（前端不硬编码中文映射，避免前后端漂移）
- 冻结数量正确（17 事件类型 / 11 根因 / 14 建议）
- ETag 条件请求可用（C 按 10–15s 轮询，304 能省掉重复传输）
- **GPU 归因的两个根因码文案必须不同** —— 否则前端无法区分该找邻居还是降并发
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine


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


class TestDictCoverage:
    def test_covers_all_sections_c_asked_for(self, client: TestClient) -> None:
        """C 的 Q14 列出的 6 类枚举必须全部覆盖。"""
        data = client.get("/api/v1/dict").json()["data"]
        assert set(data) >= {
            "severity",
            "alertState",
            "resourceKind",
            "resourceStatus",
            "eventType",
            "rootCause",
            "recommendation",
        }

    def test_severity_values(self, client: TestClient) -> None:
        data = client.get("/api/v1/dict").json()["data"]
        assert set(data["severity"]) == {"info", "warning", "error", "critical"}

    def test_alert_state_values(self, client: TestClient) -> None:
        data = client.get("/api/v1/dict").json()["data"]
        assert set(data["alertState"]) == {"firing", "acked", "resolved", "silenced"}

    def test_resource_kind_values_include_vgpu_and_task(self, client: TestClient) -> None:
        """F-04.1 定论：`vgpu` 与 `task` 均纳入首版。"""
        data = client.get("/api/v1/dict").json()["data"]
        kinds = set(data["resourceKind"])
        assert "vgpu" in kinds
        assert "task" in kinds

    def test_resource_status_values(self, client: TestClient) -> None:
        data = client.get("/api/v1/dict").json()["data"]
        assert set(data["resourceStatus"]) == {"running", "stopped", "error", "unknown"}

    def test_event_type_count_is_17(self, client: TestClient) -> None:
        """F-01 冻结了 17 项事件类型。"""
        data = client.get("/api/v1/dict").json()["data"]
        assert len(data["eventType"]) == 17

    def test_root_cause_count_is_11(self, client: TestClient) -> None:
        """F-02 冻结了 11 项根因码。"""
        data = client.get("/api/v1/dict").json()["data"]
        assert len(data["rootCause"]) == 11

    def test_recommendation_count_is_14(self, client: TestClient) -> None:
        """F-03 冻结了 14 项建议码。"""
        data = client.get("/api/v1/dict").json()["data"]
        assert len(data["recommendation"]) == 14

    def test_observability_exposed_separately(self, client: TestClient) -> None:
        """D-032：观测状态是独立枚举，前端需要它渲染"数据新鲜度"。"""
        data = client.get("/api/v1/dict").json()["data"]
        assert set(data["observability"]) == {"active", "stale", "gone"}


class TestLabels:
    def test_every_code_has_non_empty_label(self, client: TestClient) -> None:
        """**可读文案是 C 的核心要求** —— 前端不硬编码中文映射。"""
        data = client.get("/api/v1/dict").json()["data"]
        for section, mapping in data.items():
            assert mapping, f"{section} 为空"
            for code, label in mapping.items():
                assert label, f"{section}.{code} 文案为空"

    def test_gpu_attribution_codes_have_distinct_labels(self, client: TestClient) -> None:
        """**GPU 归因能力的关键**：两个根因码的文案必须不同。

        若文案相同，前端与运维无法区分"该找邻居还是该降自己的并发"，
        `GPU_NEIGHBOR_CONTENTION` 的拆分就白做了。
        """
        data = client.get("/api/v1/dict").json()["data"]
        exhausted = data["rootCause"]["GPU_MEMORY_EXHAUSTED"]
        neighbour = data["rootCause"]["GPU_NEIGHBOR_CONTENTION"]
        assert exhausted != neighbour
        assert "争用" in neighbour or "邻居" in neighbour or "其他" in neighbour

    def test_event_type_labels_are_chinese_or_proper_noun(self, client: TestClient) -> None:
        data = client.get("/api/v1/dict").json()["data"]
        for code, label in data["eventType"].items():
            has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in label)
            assert has_cjk, f"{code} 的文案 {label!r} 不含中文"


class TestCaching:
    def test_etag_header_present(self, client: TestClient) -> None:
        r = client.get("/api/v1/dict")
        assert "ETag" in r.headers
        assert r.headers["ETag"].startswith('W/"')

    def test_cache_control_allows_revalidation(self, client: TestClient) -> None:
        """C 按 10–15s 轮询字典，允许缓存但必须能及时看到枚举变化。"""
        cc = client.get("/api/v1/dict").headers["Cache-Control"]
        assert "must-revalidate" in cc

    def test_conditional_request_returns_304(self, client: TestClient) -> None:
        etag = client.get("/api/v1/dict").headers["ETag"]
        r = client.get("/api/v1/dict", headers={"If-None-Match": etag})
        assert r.status_code == 304, "内容未变时应返回 304，省掉重复传输"

    def test_stale_etag_returns_full_body(self, client: TestClient) -> None:
        r = client.get("/api/v1/dict", headers={"If-None-Match": 'W/"deadbeef"'})
        assert r.status_code == 200
        assert "rootCause" in r.json()["data"]

    def test_etag_is_stable_across_calls(self, client: TestClient) -> None:
        """同样内容必须给出同样 ETag，否则前端缓存永远命中不了。"""
        a = client.get("/api/v1/dict").headers["ETag"]
        b = client.get("/api/v1/dict").headers["ETag"]
        assert a == b


class TestEnvelope:
    def test_envelope_shape(self, client: TestClient) -> None:
        body = client.get("/api/v1/dict").json()
        assert set(body) == {"data", "meta"}
        assert body["meta"]["traceId"].startswith("req_")
        assert body["meta"]["generatedAt"]
