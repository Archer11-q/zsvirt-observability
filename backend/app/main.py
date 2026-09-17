"""Crosslayer 后端应用入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8080
（须在 backend/ 目录下执行）
"""

from fastapi import FastAPI

from app import __version__
from app.api.router import api_router, build_v1_router
from app.config import get_settings

app = FastAPI(
    title="Crosslayer API",
    description=(
        "面向 ZSvirt 虚拟机内部 AI 工作负载的跨层可观测与根因诊断平台。"
        "契约见 docs/API_CONTRACT.md。"
    ),
    version=__version__,
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
