"""GPU 采集器：VM 内直通 GPU 的访客层指标（nvidia-smi）。

定位（`DATA_MODEL.md` §2.2 三层分工 + `DECISIONS.md` D-028）：

- **L1 资产层**（序列号 / 显存容量 / 型号）权威在 ZSvirt `QueryGpuDevice`；
- **L2 性能层**（利用率 / 温度）权威在 ZWatch（命题方第一轮答复已确认启用，X-07 关闭）；
- **L3 访客层**（本模块）：VM 内 `nvidia-smi` 可见的**显存占用 `memUsedBytes`** 与
  **占用进程 `processes[]`**，用于「自身超配 vs 邻居干扰」的**交叉验证**。

命题方答复确认 GPU 为**直通**（非 MDEV），故 VM 内 `nvidia-smi` 可用、可读访客侧显存。

上报形态：`gpu` kind 资源，`sourceId` 用 GPU UUID（nvidia-smi 原生唯一标识，无冒号），
`attributes` 携带访客层字段。B 侧 `GuestSmiProvider` 在 ingest 侧消费（`origin=probe`）。

容错：无 `nvidia-smi` / 调用失败 / 解析失败一律返回空，**不影响其它采集器**。
"""

from __future__ import annotations

import csv
import io
import logging
import shutil
import subprocess
from typing import Any

from ..model import Resource, utc_now_ms
from .base import Collector

log = logging.getLogger("probe.collector.gpu")

_MIB = 1024 * 1024  # nvidia-smi 默认单位 MiB → 字节

# nvidia-smi 查询字段（顺序即 CSV 列序）
_QUERY_GPU = "uuid,name,memory.total,memory.used,pci.bus_id"
_QUERY_PROCS = "pid,used_memory"


def nvidia_smi_available() -> bool:
    """判断 VM 内是否有 GPU 直通（nvidia-smi 可用）。"""
    return shutil.which("nvidia-smi") is not None


def _run_smi(args: list[str]) -> list[list[str]] | None:
    """执行 nvidia-smi 查询，返回 CSV 行列表；任何失败返回 None。"""
    if not nvidia_smi_available():
        return None
    try:
        proc = subprocess.run(
            ["nvidia-smi", *args, "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return list(csv.reader(io.StringIO(proc.stdout)))
    except csv.Error:
        return None


def query_gpus() -> list[dict[str, Any]]:
    """查询每张 GPU 的访客层字段（uuid / 型号 / PCI 地址 / 显存 total+used）。"""
    rows = _run_smi(["--query-gpu=" + _QUERY_GPU])
    gpus: list[dict[str, Any]] = []
    if not rows:
        return gpus
    for r in rows:
        if len(r) < 5:
            continue
        try:
            total = int(float(r[2])) * _MIB
            used = int(float(r[3])) * _MIB
        except ValueError:
            continue
        gpus.append(
            {
                "uuid": r[0],
                "model": r[1],
                "pciAddress": r[4],
                "memTotalBytes": total,
                "memUsedBytes": used,
            }
        )
    return gpus


def query_compute_processes() -> list[dict[str, int]]:
    """查询占用显存的 compute 进程（pid + 已用显存字节）。"""
    rows = _run_smi(["--query-compute-apps=" + _QUERY_PROCS])
    procs: list[dict[str, int]] = []
    if not rows:
        return procs
    for r in rows:
        if len(r) < 2:
            continue
        try:
            pid = int(r[0])
            used = int(float(r[1])) * _MIB
        except ValueError:
            continue
        procs.append({"pid": pid, "usedMemBytes": used})
    return procs


class GPUCollector(Collector):
    name = "gpu"
    interval = 10.0

    def __init__(self) -> None:
        # sourceId -> 首次观察时间（firstSeenAt）
        self._first_seen: dict[str, str] = {}

    def collect(self) -> tuple[list[Resource], list[Event]]:
        if not nvidia_smi_available():
            return [], []

        now = utc_now_ms()
        procs = query_compute_processes()
        resources: list[Resource] = []

        for gpu in query_gpus():
            source_id = gpu["uuid"]
            first_seen = self._first_seen.setdefault(source_id, now)
            # `--query-compute-apps` 按进程维度返回全卡聚合；赛题为单 VM 单 GPU
            # 直通，故等价于该卡进程。多卡直通时此处会过度归属，属已知近似。
            resources.append(
                Resource(
                    kind="gpu",
                    source_id=source_id,
                    name=gpu["model"],
                    attributes={
                        "uuid": gpu["uuid"],
                        "model": gpu["model"],
                        "pciAddress": gpu["pciAddress"],
                        "memTotalBytes": gpu["memTotalBytes"],
                        "memUsedBytes": gpu["memUsedBytes"],
                        "processes": procs,
                    },
                    first_seen_at=first_seen,
                    last_seen_at=now,
                )
            )
        return resources, []
