"""`GET /api/v1/metrics` —— 事件内指标时间序列测试。

这是我们**唯一**的"趋势"能力，也是最容易被误当成完整 TSDB 的端点，所以测试
集中在两件事：

1. **它正确做到了什么** —— 把事件里的 `{名称: 数值}` 摊成按时间的点序列，
   支持按资源/指标名/时间窗过滤；
2. **它明确不做什么** —— 不做聚合、不编单位、不把布尔当数值、
   被截断时**必须**告诉调用方（一条看起来完整其实被截断的曲线比没有更危险）。

`ADR-0003` 不引入 TSDB，所以这里不该出现任何"新建指标表"的痕迹 ——
点全部来自 `event.metrics`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.events.repository import query_metric_series
from app.graph.repository import upsert_resource
from app.models import Event

RES = "host:probe:probe-metrics:h0"
OTHER = "container:probe:probe-metrics:ctr0"
T0 = datetime(2026, 9, 30, 6, 0, 0, tzinfo=UTC)


def _event(
    session: Session,
    event_id: str,
    *,
    resource_id: str = RES,
    occurred_at: datetime = T0,
    metrics: dict[str, Any] | None = None,
) -> None:
    """插入一条带自定义 `metrics` 的事件。

    不用 `_factories.add_event`：它只支持 `value` / `count` 两个键，而本组测试
    恰恰要验证**任意键名**的数值摊平，以及布尔/字符串被排除。
    """
    session.add(
        Event(
            id=event_id,
            occurred_at=occurred_at,
            received_at=occurred_at,
            source="probe",
            resource_id=resource_id,
            type="process.io_wait.high",
            severity="warning",
            message=f"metric probe on {resource_id}",
            metrics=metrics or {},
            raw={},
        )
    )


def _seed(session: Session, *, values: list[float], name: str = "memUsedPct") -> None:
    upsert_resource(session, resource_id=RES, kind="host", status="running", seen_at=T0)
    for index, value in enumerate(values):
        _event(
            session,
            f"evt_m{index:03d}",
            occurred_at=T0 + timedelta(minutes=index),
            metrics={name: value},
        )
    session.commit()


class TestMetricSeriesBasics:
    def test_points_come_back_in_ascending_time_order(
        self, client: TestClient, db_session: Session
    ) -> None:
        """曲线要从左到右画 —— 升序是契约的一部分。"""
        _seed(db_session, values=[10.0, 20.0, 30.0])

        data = client.get("/api/v1/metrics", params={"resourceId": RES}).json()["data"]
        series = next(s for s in data["series"] if s["name"] == "memUsedPct")

        assert [p["value"] for p in series["points"]] == [10.0, 20.0, 30.0]
        stamps = [p["at"] for p in series["points"]]
        assert stamps == sorted(stamps)

    def test_series_are_split_by_resource_and_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        """两个资源的同名指标**不能**混成一条曲线。

        混起来会画出一条把宿主和容器的显存连在一起的线，看起来像"急剧上升"。
        """
        upsert_resource(db_session, resource_id=RES, kind="host", status="running", seen_at=T0)
        upsert_resource(
            db_session, resource_id=OTHER, kind="container", status="running", seen_at=T0
        )
        _event(db_session, "evt_a", metrics={"memUsedPct": 1.0})
        _event(db_session, "evt_b", resource_id=OTHER, metrics={"memUsedPct": 2.0})
        db_session.commit()

        data = client.get("/api/v1/metrics").json()["data"]
        pairs = {(s["resourceId"], s["name"]) for s in data["series"]}
        assert (RES, "memUsedPct") in pairs
        assert (OTHER, "memUsedPct") in pairs

    def test_names_filter_narrows_the_result(self, client: TestClient, db_session: Session) -> None:
        _seed(db_session, values=[1.0], name="alpha")
        _event(db_session, "evt_beta", metrics={"beta": 2.0})
        db_session.commit()

        data = client.get("/api/v1/metrics", params=[("name", "alpha")]).json()["data"]
        assert data["names"] == ["alpha"]

    def test_time_window_is_honoured(self, client: TestClient, db_session: Session) -> None:
        _seed(db_session, values=[1.0, 2.0, 3.0])

        data = client.get(
            "/api/v1/metrics",
            params={
                "resourceId": RES,
                "from": (T0 + timedelta(seconds=30)).isoformat(),
                "to": (T0 + timedelta(seconds=90)).isoformat(),
            },
        ).json()["data"]
        series = next(s for s in data["series"] if s["name"] == "memUsedPct")
        assert [p["value"] for p in series["points"]] == [2.0]


class TestMetricSeriesHonesty:
    """这些是"不假装"的部分 —— 本项目一贯的红线。"""

    def test_boolean_metrics_are_not_turned_into_a_curve(
        self, client: TestClient, db_session: Session
    ) -> None:
        """`{"oomKilled": true}` 不是指标。

        Python 里 `isinstance(True, int)` 为真，不显式排除的话它会变成一条
        "oomKilled = 1.0" 的曲线 —— 一个看起来像指标、其实只是标志位的东西。
        """
        upsert_resource(db_session, resource_id=RES, kind="host", status="running", seen_at=T0)
        _event(db_session, "evt_bool", metrics={"oomKilled": True, "realMetric": 5.0})
        db_session.commit()

        data = client.get("/api/v1/metrics").json()["data"]
        assert data["names"] == ["realMetric"], data["names"]

    def test_non_numeric_values_are_skipped(self, client: TestClient, db_session: Session) -> None:
        upsert_resource(db_session, resource_id=RES, kind="host", status="running", seen_at=T0)
        _event(db_session, "evt_str", metrics={"status": "Connected", "ok": 1.0})
        db_session.commit()

        assert client.get("/api/v1/metrics").json()["data"]["names"] == ["ok"]

    def test_unit_is_never_invented(self, client: TestClient, db_session: Session) -> None:
        """事件契约不带单位 ⇒ 一律 `null`，不从名称猜。

        从名字猜（`_pct` → `%`）在 `io_wait_pct` 上碰巧对，在 `value` 上
        就是编一个看起来专业的单位。
        """
        _seed(db_session, values=[42.0], name="io_wait_pct")
        data = client.get("/api/v1/metrics").json()["data"]
        assert all(s["unit"] is None for s in data["series"])

    def test_explicit_zero_is_preserved(self, client: TestClient, db_session: Session) -> None:
        """合法的 0 必须留住 —— "显存占用 0%"是好消息，不是缺失。"""
        _seed(db_session, values=[0.0])
        data = client.get("/api/v1/metrics").json()["data"]
        series = next(s for s in data["series"] if s["name"] == "memUsedPct")
        assert series["points"][0]["value"] == 0.0


class TestMetricSeriesTruncationIsVisible:
    """被截断**必须**让调用方看见。

    一条看起来完整、其实被截断的曲线，比一条明确标注"不完整"的危险得多 ——
    前者会让人据此判断"趋势平稳"。
    """

    def test_series_truncation_keeps_the_newest_points(self, db_session: Session) -> None:
        _seed(db_session, values=[float(i) for i in range(10)])

        result = query_metric_series(db_session, scan_limit=100, points_per_series=3)
        series = next(s for s in result.series if s.name == "memUsedPct")
        assert series.truncated is True
        # 保留最新：7,8,9 而不是 0,1,2
        assert [p.value for p in series.points] == [7.0, 8.0, 9.0]

    def test_scan_truncation_is_reported(self, client: TestClient, db_session: Session) -> None:
        _seed(db_session, values=[float(i) for i in range(6)])

        data = client.get("/api/v1/metrics", params={"scanEvents": 2}).json()["data"]
        assert data["truncated"] is True, "扫描被截断时调用方必须知道"
        assert data["eventsScanned"] == 2
        # 序列级也要置位：调用方关心的是"曲线是否完整"，而不是哪一级上限触发了
        assert all(s["truncated"] for s in data["series"])

    def test_no_truncation_reports_false(self, client: TestClient, db_session: Session) -> None:
        _seed(db_session, values=[1.0])
        data = client.get("/api/v1/metrics").json()["data"]
        assert data["truncated"] is False
        assert all(s["truncated"] is False for s in data["series"])


class TestMetricSeriesValidation:
    def test_bad_from_returns_400_not_silent_ignore(self, client: TestClient) -> None:
        """静默忽略非法时间会让调用方拿到全时段曲线却以为它被过滤过。"""
        response = client.get("/api/v1/metrics", params={"from": "not-a-time"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_bad_to_returns_400(self, client: TestClient) -> None:
        assert client.get("/api/v1/metrics", params={"to": "2026-13-45"}).status_code == 400

    def test_inverted_window_returns_400(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/metrics",
            params={"from": "2026-09-18T00:00:00Z", "to": "2026-09-17T00:00:00Z"},
        )
        assert response.status_code == 400

    def test_naive_timestamp_is_interpreted_as_utc(
        self, client: TestClient, db_session: Session
    ) -> None:
        """不带时区按 UTC 解释（契约要求带时区，但不因此拒绝）。"""
        _seed(db_session, values=[1.0])
        data = client.get("/api/v1/metrics", params={"from": "2026-09-30T05:00:00"}).json()["data"]
        assert data["names"] == ["memUsedPct"]

    def test_empty_database_returns_empty_series_not_404(self, client: TestClient) -> None:
        """没有指标是**正常的**（例如尚未接入探针），不是错误。"""
        response = client.get("/api/v1/metrics")
        assert response.status_code == 200
        assert response.json()["data"]["series"] == []
        assert response.json()["data"]["names"] == []


def test_endpoint_is_in_the_openapi_schema() -> None:
    """端点必须在 schema 里 —— 前端与对账脚本据此发现它。"""
    from app.main import app

    assert "/api/v1/metrics" in app.openapi()["paths"]
