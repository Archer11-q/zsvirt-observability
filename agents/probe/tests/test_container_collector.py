"""容器采集器回归测试（针对 2026-09-30 成员 B 缺陷同步单的四条缺陷）。

夹具按 Docker Engine API 的**真实响应形状**构造（字段名与嵌套结构与真实一致，
尤其是「事件无顶层 `id`、ID 在 `Actor.ID`」「`/containers/json` 无 `RestartCount`」），
而不是按实现假设 mock —— 这正是原缺陷的教训：测试自造夹具就抓不到「夹具与真实
响应形状不一致」这类缺陷。

覆盖：
- 缺陷 1：`events()` 的 filters 必须 URL 编码（URL 里不得有空格）。
- 缺陷 2：`/events` 是 NDJSON，必须逐行解析，单行坏掉不丢整批。
- 缺陷 3：事件容器 ID 在 `Actor.ID`，`resource_source_id` 必须非空。
- 缺陷 4：`restartCount` 取自 inspect，缺失时不报「0」的错值。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))  # agents/

from probe.collector.container import ContainerCollector, DockerClient  # noqa: E402

CID64 = "ef193b87b224d12e45b8b85957b006fb87f3d7225aae6192dd0996cd987ab34b"
EXPECTED_SID = CID64[:12]

# 真实形状：/events 的 NDJSON（注意顶层**没有** `id`，ID 在 Actor.ID）
EVENT_OOM = {
    "Type": "container",
    "Action": "oom",
    "Actor": {
        "ID": CID64,
        "Attributes": {"name": "oom-demo", "image": "python:3.12-slim"},
    },
    "time": 1690000000,
    "timeNano": 1690000000000000000,
}
EVENT_DIE = {
    "Type": "container",
    "Action": "die",
    "Actor": {
        "ID": CID64,
        "Attributes": {"name": "oom-demo", "exitCode": "137"},
    },
    "time": 1690000001,
    "timeNano": 1690000001000000000,
}
EVENT_RESTART = {
    "Type": "container",
    "Action": "restart",
    "Actor": {
        "ID": CID64,
        "Attributes": {"name": "oom-demo"},
    },
    "time": 1690000002,
    "timeNano": 1690000002000000000,
}

# 真实形状：/containers/json 条目（**没有** RestartCount 字段）
CONTAINER_JSON = {
    "Id": CID64,
    "Names": ["/oom-demo"],
    "Image": "python:3.12-slim",
    "State": "running",
    "Status": "Up 3 seconds",
}

# 真实形状：/containers/<id>/json（inspect，有 RestartCount）
INSPECT_JSON = {
    "Id": CID64,
    "RestartCount": 14,
    "State": {"OOMKilled": True, "ExitCode": 137, "Running": True},
}


class TestDockerClientStream(unittest.TestCase):
    """缺陷 1 + 2：events 的 URL 编码 与 NDJSON 逐行解析。"""

    def test_get_stream_parses_ndjson(self):
        body = "\n".join(json.dumps(e) for e in [EVENT_OOM, EVENT_DIE])
        client = DockerClient()
        with mock.patch.object(client, "_request", return_value=body):
            events = client._get_stream("/events?x=1")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["Action"], "oom")
        self.assertEqual(events[1]["Action"], "die")

    def test_get_stream_skips_bad_line(self):
        body = json.dumps(EVENT_OOM) + "\n{not valid json}\n" + json.dumps(EVENT_DIE)
        client = DockerClient()
        with mock.patch.object(client, "_request", return_value=body):
            events = client._get_stream("/events?x=1")
        self.assertEqual(len(events), 2)  # 坏行跳过，不丢整批

    def test_events_url_has_no_space(self):
        """缺陷 1：filters 编码后 URL 不得含空格（原 json.dumps 默认带空格）。"""
        client = DockerClient()
        with mock.patch.object(client, "_get_stream", return_value=[]) as m:
            client.events(1690000000, 1690000100)
        path = m.call_args[0][0]
        self.assertNotIn(" ", path)
        self.assertIn("since=1690000000", path)
        self.assertIn("until=1690000100", path)
        self.assertIn("filters=", path)


class TestContainerCollectorEvents(unittest.TestCase):
    """缺陷 3：事件容器 ID 在 Actor.ID，resource_source_id 必须非空。"""

    def _collect(self, events_list):
        c = ContainerCollector()
        c._client = mock.Mock()
        c._client.list_containers.return_value = []
        c._client.events.return_value = events_list
        c._client.inspect.return_value = None
        return c.collect()

    def test_oom_event_targets_actor_id(self):
        _, events = self._collect([EVENT_OOM])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.type, "container.oom_killed")
        self.assertNotEqual(e.resource_source_id, "")  # 关键：非空
        self.assertEqual(e.resource_source_id, EXPECTED_SID)

    def test_restart_event_targets_actor_id(self):
        _, events = self._collect([EVENT_RESTART])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "container.restart")
        self.assertEqual(events[0].resource_source_id, EXPECTED_SID)

    def test_die_event_is_ignored(self):
        # `die` 在 filters 里用于窗口内 OOM 的关联，但本身不产出事件
        _, events = self._collect([EVENT_DIE])
        self.assertEqual(events, [])


class TestContainerCollectorRestartCount(unittest.TestCase):
    """缺陷 4：restartCount 取自 inspect，缺失时不报 0 错值。"""

    def _collect(self, containers, inspect):
        c = ContainerCollector()
        c._client = mock.Mock()
        c._client.list_containers.return_value = containers
        c._client.events.return_value = []
        c._client.inspect.return_value = inspect
        return c.collect()[0]

    def test_restart_count_from_inspect(self):
        resources = self._collect([CONTAINER_JSON], INSPECT_JSON)
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].attributes["restartCount"], 14)

    def test_restart_count_omitted_when_inspect_unavailable(self):
        resources = self._collect([CONTAINER_JSON], None)
        self.assertNotIn("restartCount", resources[0].attributes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
