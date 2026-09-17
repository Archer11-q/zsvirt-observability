"""接入端点的 HTTP 层测试（`POST /api/v1/ingest/batch`）。

契约：`docs/API_CONTRACT.md` §3、错误模型 §2.3。

这些测试验证 **HTTP 语义**（状态码、错误码、响应头）；
service 层语义由 `tests/test_ingest.py` 覆盖。分开的原因：
HTTP 契约是成员 A 联调时直接看到的东西，错误码错了 A 会误判故障类型。

关键契约点：

- 非法 Schema → `422 SCHEMA_VALIDATION_FAILED`（不是 500）
- 超批量上限 → `400 INVALID_ARGUMENT`（不静默截断）
- 限流 → `429 RATE_LIMITED` **且带 `Retry-After`**（A 据此退避，不无脑重放）
- 鉴权失败 → `401 UNAUTHENTICATED`（A 的 Q7：要能区分鉴权失败与网络故障）
- 重复批次 → **200**（不是 409）—— A 的 Q2 明确要求不得判定为脏数据

**测试隔离**：限流器与指标是进程级单例。为避免某个用例调整阈值后污染其他用例，
`client` 夹具默认把限流阈值调到极大（等效关闭），只有 `TestRateLimiterUnit`
与 `rate_limited_client` 使用真实限流。这比"改完再恢复"更稳 ——
不依赖恢复逻辑本身正确（此前正是因为恢复未生效，导致多个用例被意外 429）。
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.ingest.limits import BatchRateLimiter

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
AGENT = "probe-3f2a9c10"
VM_ID = "vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"


#: 批次号计数器。**默认必须唯一** —— 否则 B 的幂等去重会正确地把
#: 后续测试当成重复批次并回放首次结果，症状是"限额检查没生效""指标为 0"
#: 这类极具误导性的失败。这个坑真实踩过。
_BATCH_SEQ = itertools.count(1)


def make_payload(**overrides) -> dict:
    payload = {
        "agentId": AGENT,
        "vmId": VM_ID,
        "agentVersion": "0.1.0",
        "batchId": f"01J000000000000000000{next(_BATCH_SEQ):04d}",
        "sentAt": NOW.isoformat(),
        "resources": [
            {
                "kind": "container",
                "sourceId": "web-0",
                "name": "web-0",
                "status": "running",
                "attributes": {"image": "vllm/vllm-openai"},
            }
        ],
        "events": [
            {
                "occurredAt": (NOW - timedelta(seconds=5)).isoformat(),
                "resourceRef": {"kind": "container", "sourceId": "web-0"},
                "type": "container.oom_killed",
                "severity": "critical",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _build_client(db_engine: Engine, *, max_batches: int = 1_000_000) -> TestClient:
    """构造指向测试库的 TestClient，并把限流阈值设为给定值。"""
    from sqlalchemy.orm import sessionmaker

    from app.db import get_db
    from app.ingest import limits
    from app.ingest.limits import reset_metrics

    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)

    def _override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    reset_metrics()
    limits.rate_limiter.max_batches = max_batches
    limits.rate_limiter.window_seconds = 60

    from app.main import app

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _teardown_client() -> None:
    from app.db import get_db
    from app.ingest.limits import reset_metrics
    from app.main import app

    app.dependency_overrides.pop(get_db, None)
    # 恢复默认阈值并清零窗口，防止极紧的限流设置泄漏到其他用例
    reset_metrics()


@pytest.fixture
def client(db_engine: Engine):
    """不限流的 TestClient（常规 HTTP 契约测试用）。"""
    c = _build_client(db_engine, max_batches=1_000_000)
    try:
        yield c
    finally:
        _teardown_client()


@pytest.fixture
def rate_limited_client(db_engine: Engine, monkeypatch: pytest.MonkeyPatch):
    """限流生效为 2 批/分钟的 TestClient（仅用于验证 429 语义）。

    注意：conftest 里有 autouse 夹具会把阈值设到极大，
    因此这里必须在它**之后**再设小 —— 通过 monkeypatch 在本夹具内完成，
    优先级高于 autouse 夹具（fixture 实例化顺序：autouse 先，然后按依赖）。
    """
    from app.ingest import limits

    c = _build_client(db_engine, max_batches=2)
    monkeypatch.setattr(limits.rate_limiter, "window_seconds", 60)
    try:
        yield c
    finally:
        _teardown_client()


class TestHappyPath:
    def test_accepts_batch(self, client: TestClient) -> None:
        r = client.post("/api/v1/ingest/batch", json=make_payload())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["data"]["accepted"]["resources"] == 1
        assert body["data"]["accepted"]["events"] == 1
        assert body["data"]["duplicate"] is False
        assert body["meta"]["traceId"].startswith("ing_")
        assert "serverTime" in body["meta"]

    def test_per_resource_result_present(self, client: TestClient) -> None:
        """A 的 Q1：必须在响应里逐条给出 resource 结果与 globalId。"""
        r = client.post("/api/v1/ingest/batch", json=make_payload())
        resources = r.json()["data"]["resources"]
        assert len(resources) == 1
        assert resources[0]["result"] == "accepted"
        assert resources[0]["globalId"] == f"container:probe:{AGENT}:web-0"

    def test_duplicate_returns_200_not_conflict(self, client: TestClient) -> None:
        """**A 的 Q2**：重复批次必须返回 200 并回放首次结果，不得返回 409/422。

        逐字段比对而非整体 dict 比较，失败时能看出具体哪个字段不一致 ——
        A 用这个响应维护「已确认资源」缓存，任何字段不一致都会污染它的缓存。
        """
        payload = make_payload()
        first = client.post("/api/v1/ingest/batch", json=payload)
        second = client.post("/api/v1/ingest/batch", json=payload)

        assert first.status_code == 200, first.text[:300]
        assert second.status_code == 200, f"重复批次不是错误：{second.text[:300]}"

        first_data = first.json()["data"]
        second_data = second.json()["data"]
        assert first_data["duplicate"] is False
        assert second_data["duplicate"] is True, f"第二次未被识别为重复批次：{second_data}"
        assert {k: v for k, v in second_data.items() if k != "duplicate"} == {
            k: v for k, v in first_data.items() if k != "duplicate"
        }
        assert second_data["resources"] == first_data["resources"], (
            "A 用这个响应维护「已确认资源」缓存，逐条结果必须一致"
        )

    def test_duplicate_header(self, client: TestClient) -> None:
        payload = make_payload()
        client.post("/api/v1/ingest/batch", json=payload)
        second = client.post("/api/v1/ingest/batch", json=payload)
        assert second.headers["X-Ingest-Duplicate"] == "true"


class TestErrorModel:
    def test_malformed_json_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/ingest/batch",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "SCHEMA_VALIDATION_FAILED"
        assert r.json()["error"]["traceId"]

    def test_missing_required_field_returns_422_with_location(self, client: TestClient) -> None:
        """错误必须能定位到字段 ——「错误响应要可诊断，不能只返回 500」。"""
        payload = make_payload()
        del payload["vmId"]
        r = client.post("/api/v1/ingest/batch", json=payload)
        assert r.status_code == 422
        errors = r.json()["error"]["details"]["errors"]
        assert any("vmId" in e["loc"] for e in errors), errors

    def test_bare_uuid_vmid_rejected_with_helpful_message(self, client: TestClient) -> None:
        """探针裸传 VM UUID 会被当作未知资源，导致所有资源挂占位节点。

        提前失败并说明期望格式，比事后排查"为什么全挂在 unresolved 下"划算得多。
        """
        r = client.post(
            "/api/v1/ingest/batch",
            json=make_payload(vmId="3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"),
        )
        assert r.status_code == 422
        assert "vm:zsvirt:" in str(r.json()["error"]["details"])

    def test_invalid_severity_returns_422(self, client: TestClient) -> None:
        payload = make_payload(
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "web-0"},
                    "type": "container.oom_killed",
                    "severity": "fatal",
                }
            ]
        )
        r = client.post("/api/v1/ingest/batch", json=payload)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "SCHEMA_VALIDATION_FAILED"

    def test_bad_source_id_rejected_per_item_not_whole_batch(self, client: TestClient) -> None:
        """**部分接受**：单个坏条目走条目级拒收（200 + rejected），不是整批 422。"""
        payload = make_payload(
            resources=[
                {"kind": "container", "sourceId": "web-0", "status": "running"},
                {"kind": "process", "sourceId": "12345:678", "status": "running"},
            ],
            events=[],
        )
        r = client.post("/api/v1/ingest/batch", json=payload)
        assert r.status_code == 200, f"坏条目不应导致整批被拒：{r.text[:300]}"
        body = r.json()["data"]
        assert body["accepted"]["resources"] == 1
        assert len(body["rejected"]) == 1
        assert body["rejected"][0]["index"] == 1
        assert body["rejected"][0]["reason"] == "INVALID_RESOURCE_ID"


class TestLimitsOverHttp:
    def test_oversized_batch_returns_400(self, client: TestClient) -> None:
        """超上限返回 400 而不是静默截断 —— 静默截断会让探针以为数据已送达。"""
        payload = make_payload(
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "web-0"},
                    "type": "container.restart",
                    "severity": "warning",
                }
                for _ in range(1001)
            ]
        )
        r = client.post("/api/v1/ingest/batch", json=payload)
        assert r.status_code == 400, r.text[:300]
        assert r.json()["error"]["code"] == "INVALID_ARGUMENT"
        assert "超过上限" in r.json()["error"]["message"]

    def test_oversized_payload_returns_400(self, client: TestClient) -> None:
        large = "x" * (1024 * 1024 + 100)
        payload = make_payload(
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "web-0"},
                    "type": "container.restart",
                    "severity": "warning",
                    "raw": {"blob": large},
                }
            ]
        )
        r = client.post("/api/v1/ingest/batch", json=payload)
        assert r.status_code == 400, r.text[:300]
        assert r.json()["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.real_rate_limit
    def test_rate_limit_returns_429_with_retry_after(self, rate_limited_client: TestClient) -> None:
        """**A 的 Q4**：429 必须带 `Retry-After`，A 据此退避而不是无脑重放。"""
        c = rate_limited_client
        for batch_id in ("b1", "b2"):
            assert (
                c.post("/api/v1/ingest/batch", json=make_payload(batchId=batch_id)).status_code
                == 200
            )

        r = c.post("/api/v1/ingest/batch", json=make_payload(batchId="b3"))
        assert r.status_code == 429, r.text[:300]
        assert r.json()["error"]["code"] == "RATE_LIMITED"
        assert "Retry-After" in r.headers
        assert int(r.headers["Retry-After"]) >= 1
        assert r.json()["error"]["details"]["retryAfterSeconds"] >= 1


class TestRateLimiterUnit:
    """限流器单测（独立实例，天然隔离，不经 HTTP）。"""

    def test_allows_within_limit(self) -> None:
        rl = BatchRateLimiter(max_batches=3, window_seconds=60)
        for _ in range(3):
            assert rl.check_and_record(NOW).allowed is True

    def test_rejects_over_limit_with_retry_after(self) -> None:
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

    def test_reset_restores_defaults(self) -> None:
        """`reset()` 必须恢复阈值，而不只是清窗口。

        否则测试里为快速触发限流而调小的阈值会残留，污染后续用例 ——
        这个坑真实发生过。
        """
        rl = BatchRateLimiter()
        rl.max_batches = 2
        rl.window_seconds = 5
        rl.check_and_record(NOW)
        rl.reset()
        assert rl.max_batches == BatchRateLimiter.DEFAULT_MAX_BATCHES
        assert rl.window_seconds == BatchRateLimiter.DEFAULT_WINDOW_SECONDS
        assert rl.current_count(NOW) == 0


class TestAuth:
    def test_auth_disabled_by_default(self, client: TestClient) -> None:
        """A 的 Q7：可选 Bearer Token，**默认关闭**。"""
        r = client.post("/api/v1/ingest/batch", json=make_payload())
        assert r.status_code == 200

    def test_missing_token_when_enabled_returns_401(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import Settings

        monkeypatch.setattr(
            "app.api.ingest.get_settings",
            lambda: Settings(API_AUTH_TOKEN="s3cret-token"),  # type: ignore[call-arg]
        )
        r = client.post("/api/v1/ingest/batch", json=make_payload())
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHENTICATED"
        assert "停止重试" in r.json()["error"]["message"]

    def test_wrong_token_returns_401(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import Settings

        monkeypatch.setattr(
            "app.api.ingest.get_settings",
            lambda: Settings(API_AUTH_TOKEN="s3cret-token"),  # type: ignore[call-arg]
        )
        r = client.post(
            "/api/v1/ingest/batch",
            json=make_payload(),
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401

    def test_correct_token_passes(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import Settings

        monkeypatch.setattr(
            "app.api.ingest.get_settings",
            lambda: Settings(API_AUTH_TOKEN="s3cret-token"),  # type: ignore[call-arg]
        )
        r = client.post(
            "/api/v1/ingest/batch",
            json=make_payload(),
            headers={"Authorization": "Bearer s3cret-token"},
        )
        assert r.status_code == 200


class TestHealthIntegration:
    def test_health_reports_ingest_metrics(self, client: TestClient) -> None:
        """`/api/health` 的 ingest 分量必须来自真实运行状态，不是占位符。"""
        r0 = client.post("/api/v1/ingest/batch", json=make_payload())
        assert r0.status_code == 200, r0.text[:300]

        r = client.get("/api/health")
        assert r.status_code == 200
        detail = r.json()["components"]["ingest"]["detail"]
        assert detail["batchesAccepted"] == 1, f"ingest detail={detail}"
        assert "clockDriftMs" in detail
        assert "clockDriftExceedsWarn" in detail
        assert "unresolvedEvents" in detail

    def test_clock_drift_degrades_health(self, client: TestClient) -> None:
        """漂移超阈值时 `/api/health` 的 ingest 必须转 degraded。

        理由：漂移会让跨层关联的时间窗失效，但服务表面仍"正常"——
        不降级的话运维看不到这个隐患。
        """
        from app.ingest import limits

        limits.metrics.observe_clock_drift(NOW, NOW - timedelta(seconds=30))
        r = client.get("/api/health")
        ingest = r.json()["components"]["ingest"]
        assert ingest["status"] == "degraded"
        assert ingest["detail"]["error"] == "CLOCK_DRIFT_EXCEEDS_THRESHOLD"

    def test_unresolved_events_surface_in_health(self, client: TestClient) -> None:
        """孤儿引用计数是链路完整性的信号，必须可见（A 的 Q1）。"""
        payload = make_payload(
            resources=[],
            events=[
                {
                    "occurredAt": NOW.isoformat(),
                    "resourceRef": {"kind": "container", "sourceId": "ghost"},
                    "type": "container.oom_killed",
                    "severity": "critical",
                }
            ],
        )
        r0 = client.post("/api/v1/ingest/batch", json=payload)
        assert r0.status_code == 200, r0.text[:300]

        r = client.get("/api/health")
        assert r.json()["components"]["ingest"]["detail"]["unresolvedEvents"] == 1
