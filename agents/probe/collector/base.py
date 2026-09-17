"""采集器基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..model import Event, Resource


class Collector(ABC):
    """采集器基类：`collect()` 返回一轮采集到的资源与事件。"""

    name: str = "base"
    interval: float = 5.0  # 轮询间隔（秒）

    @abstractmethod
    def collect(self) -> tuple[list[Resource], list[Event]]:
        """采集一轮，返回 `(resources, events)`。"""
        raise NotImplementedError
