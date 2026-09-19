"""采集器集合。"""

from __future__ import annotations

from .ai_service import AIServiceCollector
from .base import Collector
from .container import ContainerCollector
from .gpu import GPUCollector
from .network import NetworkCollector
from .process import ProcessCollector

# 默认启用的采集器（GPU 默认不启用，见 gpu.py 说明）
DEFAULT_COLLECTORS: list[type[Collector]] = [
    ProcessCollector,
    ContainerCollector,
    AIServiceCollector,
    NetworkCollector,
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
