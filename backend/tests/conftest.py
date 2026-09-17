"""pytest 共享夹具。

**测试库隔离**：涉及数据库的测试使用独立的 `crosslayer_test` 库，
绝不碰开发库 `crosslayer`。库名由 `TEST_DATABASE_URL` 指定，
未设置时按 `DATABASE_URL` 推导（把库名替换为 `<库名>_test`）。

若测试库不可用（例如 CI 上没有 PostgreSQL），相关测试**显式 skip**
并给出可操作的提示，而不是静默通过 —— 一个"假通过"的集成测试
比没有测试更危险。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

DEFAULT_DEV_URL = "postgresql+psycopg://crosslayer:crosslayer_dev_pw@127.0.0.1:5432/crosslayer"


def _test_database_url() -> str:
    """测试库连接串。"""
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit

    base = os.environ.get("DATABASE_URL", DEFAULT_DEV_URL)
    # 把库名替换为 <库名>_test（保留 driver / 用户 / 主机 / 端口）
    if "/" not in base:
        return base
    head, _, tail = base.rpartition("/")
    # tail 可能带查询串，简单处理：只改库名部分
    dbname, sep, query = tail.partition("?")
    if dbname.endswith("_test"):
        return base
    new_tail = f"{dbname}_test{sep}{query}"
    return f"{head}/{new_tail}"


TEST_DATABASE_URL = _test_database_url()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """会话级引擎。测试库不可用时 skip 全部数据库测试。"""
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"测试库不可用，跳过数据库测试。\n"
            f"  目标: {TEST_DATABASE_URL}\n"
            f"  原因: {type(exc).__name__}: {exc}\n"
            f"  处理: 确认 PostgreSQL 在运行，且测试库已创建：\n"
            f"        sudo -u postgres psql -c 'ALTER ROLE crosslayer CREATEDB;'\n"
            f"        然后 psql ... -c 'CREATE DATABASE crosslayer_test OWNER crosslayer;'"
        )
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """每个测试一个 session，测试结束后清空全部表。

    用 TRUNCATE ... CASCADE 而不是 drop/create：保持表结构（省时间），
    只清数据，且能正确处理外键依赖顺序。
    """
    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with db_engine.begin() as conn:
            tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
            if tables:
                conn.execute(text(f"TRUNCATE {tables} CASCADE"))
        # 清空连接池：应用侧与测试侧共用同一引擎，池中连接若带着上一个用例的
        # 事务快照，后续用例会读到陈旧状态（表现为"已提交的行查不到"）。
        db_engine.dispose()


@pytest.fixture(autouse=True)
def _no_rate_limiting_by_default(request, monkeypatch: pytest.MonkeyPatch) -> None:
    """全局关闭限流（除非用例显式覆盖）。

    限流器是进程级单例，若某个用例为验证 429 而调小阈值，
    残留会污染同批其他用例（症状是后续请求被意外 429，
    表现为"指标恒为 0"这类难以定位的失败）。

    这里用 autouse 在**每个**用例前把阈值设到极大，从机制上隔离。
    需要验证限流的用例加 `@pytest.mark.real_rate_limit` 让本夹具跳过 ——
    注意**不能用**"用例里设小阈值"的方式：pytest 的 autouse 夹具在显式请求的
    夹具**之后**实例化，反而会覆盖用例的设置（这个坑真实踩过）。
    """
    if request.node.get_closest_marker("real_rate_limit"):
        # 该用例要验证真实限流，不覆盖它的设置
        return

    from app.ingest import limits

    monkeypatch.setattr(limits.rate_limiter, "max_batches", 1_000_000)
