"""健康端点契约测试（docs/API_CONTRACT.md §4.8）。"""

from fastapi.testclient import TestClient

from app.api.health import probe_database
from app.main import app

client = TestClient(app)


def test_health_shape():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "degraded", "down"}
    assert set(body["components"]) >= {"database", "zsvirt", "ingest", "gpuProvider"}
    assert body["generatedAt"]


def test_health_degrades_when_zsvirt_unconfigured():
    """/api/health 必须说明具体哪个上游坏了，而不是只给整体状态。"""
    r = client.get("/api/health")
    zsvirt = r.json()["components"]["zsvirt"]
    assert zsvirt["status"] == "degraded"
    assert zsvirt["detail"]["error"] == "ZSVIRT_ENDPOINT_NOT_CONFIGURED"


def test_health_reports_gpu_provider_mode():
    """诚实性要求：必须暴露 GPU 数据渠道。"""
    r = client.get("/api/health")
    assert "mode" in r.json()["components"]["gpuProvider"]["detail"]


def test_root_endpoint():
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["name"] == "Crosslayer"


def test_database_probe_is_honest_when_unconfigured():
    """未配置连接串时必须 degraded，不得伪造 ok。"""
    status = probe_database("")
    assert status.status == "degraded"
    assert status.detail is not None
    assert status.detail["error"] == "DATABASE_URL_NOT_CONFIGURED"


def test_database_probe_reports_failure_not_ok():
    """连不上时必须 degraded 并给出错误类型，不得静默返回 ok。"""
    status = probe_database("postgresql+psycopg://nobody:nopass@127.0.0.1:1/nodb")
    assert status.status == "degraded"
    assert status.detail is not None
    assert "error" in status.detail


def test_database_probe_ok_against_local_postgres():
    """本机 PostgreSQL 可用时（CI 无库则跳过），应返回 ok 且带延迟。"""
    import os

    url = os.environ.get("DATABASE_URL")
    if not url:
        import pytest

        pytest.skip("DATABASE_URL 未设置，跳过真实数据库探测")
    status = probe_database(url)
    assert status.status == "ok", status.detail
    assert "latencyMs" in (status.detail or {})
