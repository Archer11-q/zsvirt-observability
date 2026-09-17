"""GPU 采集器（预留扩展点，默认不启用）。

定位：GPU/vGPU 指标主责在 B 的 `zsvirt-adapter`（DECISIONS D-028），
且 F-01 中 GPU 相关事件的产生方为 ZSvirt（非探针）。因此探针**默认不产出 GPU 数据**，
仅保留 `nvidia_smi_available()` 探测，待明确「VM 内 GPU 直通」与数据归属后再启用。
"""

from __future__ import annotations

import shutil

from ..model import Event, Resource
from .base import Collector


def nvidia_smi_available() -> bool:
    """判断 VM 内是否有 GPU 直通（nvidia-smi 可用）。"""
    return shutil.which("nvidia-smi") is not None


class GPUCollector(Collector):
    name = "gpu"
    interval = 10.0

    def collect(self) -> tuple[list[Resource], list[Event]]:
        # 预留：待 GPU 数据归属明确后再实现。默认不产出。
        return [], []
