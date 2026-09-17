"""数据库会话与引擎管理。

配置来自 `DATABASE_URL`（`docs/DEPLOYMENT.md` §4）。

**为什么不用 async**：当前规模（单机原型、三类故障场景）下同步 SQLAlchemy
足够，且分析型查询（时间窗聚合、资源图遍历）写起来更直白。引入 async
会带来 session 生命周期管理与驱动选择（`psycopg` async）的额外复杂度，
而收益在当前规模下不可测。若将来接入压力上来，改 async 只影响本文件与
repository 层（`app.graph` / `app.events` 等），不影响业务逻辑。
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """进程内共享的引擎（惰性创建）。

    `pool_pre_ping` 让连接在借出前探活 —— 数据库重启或空闲连接被回收后
    不会把坏连接交给业务代码。
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务边界上下文：正常提交，异常回滚，始终关闭。

    ```python
    with session_scope() as session:
        session.add(obj)
    ```
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入入口（每个请求一个 session）。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def session_scope_or_none() -> Iterator[Session]:
    """兼容旧调用点的别名，行为同 `session_scope`。"""
    return session_scope()


def create_all() -> None:
    """按模型建表（开发/测试用）。

    **生产请用 Alembic 迁移** —— `create_all` 不做版本管理，无法演进。
    本函数仅用于测试与本地快速起步。
    """
    Base.metadata.create_all(bind=get_engine())


def drop_all() -> None:
    """删表（仅测试用）。"""
    Base.metadata.drop_all(bind=get_engine())


def check_connection() -> tuple[bool, str]:
    """探测连通性。返回 `(是否可用, 描述)`。

    供 `/api/health` 使用 —— 健康检查必须给出**具体**结果，
    不能只返回一个布尔值（`API_CONTRACT.md` §4.8 的可诊断性要求）。
    """
    try:
        with get_engine().connect() as conn:
            version = conn.execute(text("select version()")).scalar()
        return True, str(version).split(" on ")[0]
    except Exception as exc:  # noqa: BLE001 - 健康检查必须吞异常并降级
        return False, f"{type(exc).__name__}: {exc}"


def reset_engine() -> None:
    """丢弃缓存的引擎与会话工厂（测试之间切换数据库时使用）。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
