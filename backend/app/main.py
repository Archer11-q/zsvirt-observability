"""Crosslayer 后端应用入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8080
（须在 backend/ 目录下执行）
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.router import api_router, build_v1_router
from app.config import get_settings
from app.scheduler import PeriodicTasks, build_periodic_tasks

log = logging.getLogger("crosslayer.main")

#: 当前运行的周期任务集合。`/api/health` 会读它的状态。
#: 模块级而不是挂在 `app.state` 上，是为了让 `health.py` 不必依赖 request 对象
#: （健康检查可能在极早期被调用）。
runtime_tasks: PeriodicTasks | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """同进程周期任务的启动与关闭（`ARCHITECTURE.md` §4.4）。

    在这里做而不是在 `@app.on_event("startup")`：后者已废弃，且 lifespan 能在
    **同一个作用域**里表达"启动了什么就要关闭什么"，避免漏掉 shutdown。
    """
    global runtime_tasks
    runtime_tasks = build_periodic_tasks()
    if runtime_tasks is not None:
        await runtime_tasks.start()
    try:
        yield
    finally:
        if runtime_tasks is not None:
            # 协作式取消并等待收尾：uvicorn 重启时不留半写状态的 session
            await runtime_tasks.stop()
        runtime_tasks = None


app = FastAPI(
    title="Crosslayer API",
    description=(
        "面向 ZSvirt 虚拟机内部 AI 工作负载的跨层可观测与根因诊断平台。"
        "契约见 docs/API_CONTRACT.md。"
    ),
    version=__version__,
    lifespan=lifespan,
)

app.include_router(api_router)
app.include_router(build_v1_router())


@app.get("/", include_in_schema=False)
def root() -> dict:
    settings = get_settings()
    return {
        "name": "Crosslayer",
        "version": __version__,
        "docs": "/docs",
        "health": "/api/health",
        "gpuProvider": settings.gpu_provider_mode,
    }
