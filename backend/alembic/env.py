"""Alembic 环境配置。

要点：

1. **连接串不写在 alembic.ini**，而是从 `app.config` 读取（它来自环境变量
   `DATABASE_URL`）。原因：连接串含密码，绝不能进仓库。
2. `compare_type=True` 与 `compare_server_default=True` 让 autogenerate 能
   检测列类型与默认值变化，而不只是增删列。
3. 迁移脚本在执行前会先导入 `app.models`，确保 `target_metadata` 完整 ——
   否则 autogenerate 会误判"表被删除"。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: 供 autogenerate 比对的目标 schema
target_metadata = Base.metadata


def _database_url() -> str:
    """迁移目标连接串。

    优先取环境变量（`DATABASE_URL`），这样 CI 与测试可以指向独立库。
    """
    settings = get_settings()
    url = settings.database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未配置，无法执行迁移。见 docs/DEPLOYMENT.md §4。"
        )
    return url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
