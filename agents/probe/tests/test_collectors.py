"""采集器单测（零第三方依赖，纯标准库 unittest + mock）。

覆盖：
- `procutil.container_source_id`：cgroup 解析容器 ID（docker v1 / v2）。
- `ProcessCollector`：`parentSourceId` 挂容器；`process.io_wait.high` 窗口化差分
  （连续超阈才报、带 metrics）。
- `NetworkCollector`：`container.network.unreachable` 的 up→down 状态机（只报一次）。

运行（在仓库根）：`python agents/probe/tests/test_collectors.py`
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))  # agents/

from probe.collector import gpu as gpu_mod  # noqa: E402
from probe.collector.network import NetworkCollector  # noqa: E402
from probe.collector.process import ProcessCollector  # noqa: E402
from probe.collector.procutil import container_source_id  # noqa: E402

_CID64 = "ab" * 32  # 正好 64 位 hex（Docker 容器 ID 长度）
DOCKER_V1 = f"12:memory:/docker/{_CID64}\n"
DOCKER_V2 = f"0::/docker/{_CID64}\n"
EXPECTED_SID = _CID64[:12]  # 64 位容器 ID 前 12 位
HOST_CGROUP = "0::/user.slice/user-1000.slice/session-3.scope\n"


class TestContainerSourceId(unittest.TestCase):
    def test_docker_v1(self):
        with mock.patch("probe.collector.procutil.read_text", return_value=DOCKER_V1):
            self.assertEqual(container_source_id(123), EXPECTED_SID)

    def test_docker_v2(self):
        with mock.patch("probe.collector.procutil.read_text", return_value=DOCKER_V2):
            self.assertEqual(container_source_id(123), EXPECTED_SID)

    def test_host_process(self):
        with mock.patch("probe.collector.procutil.read_text", return_value=HOST_CGROUP):
            self.assertIsNone(container_source_id(123))

    def test_unreadable(self):
        with mock.patch("probe.collector.procutil.read_text", return_value=None):
            self.assertIsNone(container_source_id(123))


def _stat(utime: int, stime: int, blkio: int) -> dict[str, str]:
    return {
        "pid": "10", "comm": "python", "state": "S", "ppid": "1",
        "starttime": "1847263",
        "utime": str(utime), "stime": str(stime), "blkio_ticks": str(blkio),
    }


class TestProcessCollector(unittest.TestCase):
    def _env(self, collector: ProcessCollector, stat: dict[str, str]):
        """打桩 /proc 读取，返回当轮事件。"""
        with mock.patch("probe.collector.process.list_pids", return_value=[10]), \
             mock.patch("probe.collector.process.read_stat", return_value=stat), \
             mock.patch("probe.collector.process.read_cmdline",
                        return_value=["python", "serve.py"]), \
             mock.patch("probe.collector.process.in_container", return_value=True), \
             mock.patch("probe.collector.process.is_framework", return_value=False), \
             mock.patch("probe.collector.process.container_source_id",
                        return_value="abcdef123456"), \
             mock.patch("probe.collector.process.rss_bytes", return_value=1024):
            resources, events = collector.collect()
        return resources, events

    def test_parent_source_id_attached(self):
        c = ProcessCollector()
        resources, _ = self._env(c, _stat(100, 100, 0))
        self.assertEqual(resources[0].parent_source_id, "abcdef123456")
        # firstSeenAt / lastSeenAt 已填充
        self.assertIsNotNone(resources[0].first_seen_at)
        self.assertIsNotNone(resources[0].last_seen_at)

    def test_first_seen_at_stable_across_rounds(self):
        c = ProcessCollector()
        resources1, _ = self._env(c, _stat(100, 100, 0))
        resources2, _ = self._env(c, _stat(150, 150, 0))
        # firstSeenAt 沿用首次观察时间（sourceId 不变则不变），lastSeenAt 每次刷新
        self.assertEqual(resources2[0].first_seen_at, resources1[0].first_seen_at)

    def test_io_wait_requires_consecutive_rounds(self):
        c = ProcessCollector()
        # 第 1 轮：建立基线
        _, ev1 = self._env(c, _stat(100, 100, 0))
        self.assertEqual(ev1, [])
        # 第 2 轮：io_wait 占比 50%（blkio 差分 200 / total 400）→ 超阈但未连续
        _, ev2 = self._env(c, _stat(200, 200, 200))
        self.assertEqual(ev2, [])
        # 第 3 轮：继续高 io_wait → 连续 2 轮，触发
        _, ev3 = self._env(c, _stat(300, 300, 400))
        self.assertEqual(len(ev3), 1)
        self.assertEqual(ev3[0].type, "process.io_wait.high")
        self.assertAlmostEqual(ev3[0].metrics["io_wait_pct"], 50.0, places=1)

    def test_io_wait_resets_after_recovery(self):
        c = ProcessCollector()
        self._env(c, _stat(100, 100, 0))
        self._env(c, _stat(200, 200, 200))  # 高 io_wait 1 轮
        # 第 3 轮回落：blkio 差分 0 → 连续计数清零
        _, ev3 = self._env(c, _stat(300, 300, 200))
        self.assertEqual(ev3, [])
        # 第 4 轮再高：重新从 0 计，未到 2 轮，不报
        _, ev4 = self._env(c, _stat(400, 400, 400))
        self.assertEqual(ev4, [])


class TestNetworkCollector(unittest.TestCase):
    def test_up_to_down_reports_once(self):
        c = NetworkCollector()
        targets = {("127.0.0.1", 8000): "abcdef123456"}
        with mock.patch.object(c, "_discover", return_value=targets):
            with mock.patch.object(c, "_probe", return_value=True):
                _, ev = c.collect()
                self.assertEqual(ev, [])  # 首次可达，不报
            with mock.patch.object(c, "_probe", return_value=False):
                _, ev = c.collect()
                self.assertEqual(len(ev), 1)  # up→down，报一次
                self.assertEqual(ev[0].type, "container.network.unreachable")
                self.assertEqual(ev[0].resource_kind, "container")
                self.assertEqual(ev[0].resource_source_id, "abcdef123456")
            with mock.patch.object(c, "_probe", return_value=False):
                _, ev = c.collect()
                self.assertEqual(ev, [])  # 持续不可达，不重复报
            with mock.patch.object(c, "_probe", return_value=True):
                _, ev = c.collect()
                self.assertEqual(ev, [])  # 恢复可达，不报


class TestGPUCollector(unittest.TestCase):
    def test_unavailable_returns_empty(self):
        c = gpu_mod.GPUCollector()
        with mock.patch.object(gpu_mod, "nvidia_smi_available", return_value=False):
            resources, events = c.collect()
        self.assertEqual(resources, [])
        self.assertEqual(events, [])

    def test_collect_emits_gpu_resource(self):
        c = gpu_mod.GPUCollector()
        gpu = {
            "uuid": "GPU-3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44",
            "model": "Tesla V100-SXM2-32GB",
            "pciAddress": "00000000:00:08.0",
            "memTotalBytes": 34089742336,
            "memUsedBytes": 10737418240,
        }
        procs = [{"pid": 1827, "usedMemBytes": 10737418240}]
        with mock.patch.object(gpu_mod, "nvidia_smi_available", return_value=True), \
             mock.patch.object(gpu_mod, "query_gpus", return_value=[gpu]), \
             mock.patch.object(gpu_mod, "query_compute_processes", return_value=procs):
            resources, _ = c.collect()
        self.assertEqual(len(resources), 1)
        r = resources[0]
        self.assertEqual(r.kind, "gpu")
        self.assertEqual(r.source_id, gpu["uuid"])
        self.assertNotIn(":", r.source_id)  # sourceId 不得含冒号
        self.assertEqual(r.attributes["memUsedBytes"], 10737418240)
        self.assertEqual(r.attributes["processes"], procs)
        self.assertIsNotNone(r.first_seen_at)

    def test_csv_mib_to_bytes(self):
        # 直接测 CSV 解析：MiB 数值 → 字节
        rows = [["GPU-abc", "Tesla V100", "32768", "512", "00000000:00:08.0"]]
        with mock.patch.object(gpu_mod, "nvidia_smi_available", return_value=True), \
             mock.patch.object(gpu_mod, "_run_smi", return_value=rows):
            gpus = gpu_mod.query_gpus()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["memTotalBytes"], 32768 * 1024 * 1024)
        self.assertEqual(gpus[0]["memUsedBytes"], 512 * 1024 * 1024)
        self.assertEqual(gpus[0]["pciAddress"], "00000000:00:08.0")

    def test_bad_rows_are_skipped(self):
        # 坏行（缺列 / 非数值）应被跳过，不抛异常
        rows = [["GPU-x", "Model", "not-a-number", "512", "pci"]]
        with mock.patch.object(gpu_mod, "nvidia_smi_available", return_value=True), \
             mock.patch.object(gpu_mod, "_run_smi", return_value=rows):
            self.assertEqual(gpu_mod.query_gpus(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
