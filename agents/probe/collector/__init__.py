"""采集器集合。"""

from __future__ import annotations

from .ai_service import AIServiceCollector
from .base import Collector
from .container import ContainerCollector
from .gpu import GPUCollector
from .network import NetworkCollector
from .process import ProcessCollector

# 默认启用的采集器。GPU 采集器内部容错（无 nvidia-smi 时返回空、不影响其它），
# 命题方已确认 GPU 直通（X-04 部分关闭），故默认启用访客层采集。
DEFAULT_COLLECTORS: list[type[Collector]] = [
    ProcessCollector,
    ContainerCollector,
    AIServiceCollector,
    NetworkCollector,
    GPUCollector,
]

__all__ = [
    "Collector",
    "ProcessCollector",
    "ContainerCollector",
    "AIServiceCollector",
    "NetworkCollector",
    "GPUCollector",
    "DEFAULT_COLLECTORS",
]
