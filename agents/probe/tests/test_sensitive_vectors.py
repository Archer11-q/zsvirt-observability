"""探针侧脱敏一致性校验（零第三方依赖）。

只用标准库 `json` + `unittest`，解析共享向量 `shared/sensitive_vectors.json`
（与 B 侧 `backend/tests/test_sensitive_vectors.py` 使用**同一份**向量），
断言探针的 `sensitive.py` 与 B 侧 `normalize/sensitive.py` 逐条行为一致。

这份测试的意义：两侧脱敏规则若靠口头对齐必然漂移，向量把"行为一致"
变成可执行断言——任何一侧改动导致不一致，测试立刻失败。

运行方式（在仓库根，任选其一）：

    python agents/probe/tests/test_sensitive_vectors.py
    python -m unittest discover -s agents/probe/tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# 把 agents/probe 加到 sys.path，便于 import sensitive / idgen
_HERE = Path(__file__).resolve().parent      # agents/probe/tests
_PROBE = _HERE.parent                         # agents/probe
_REPO = _HERE.parents[2]                      # 仓库根
sys.path.insert(0, str(_PROBE))

from idgen import ai_service_source_id, process_source_id  # noqa: E402
from sensitive import filter_sensitive, is_sensitive_key, mask_cmdline  # noqa: E402

VECTORS_PATH = _REPO / "shared" / "sensitive_vectors.json"


def _load_vectors() -> dict:
    with open(VECTORS_PATH, encoding="utf-8") as f:
        return json.load(f)


class TestSensitiveVectors(unittest.TestCase):
    """探针侧与共享向量逐条对齐。"""

    @classmethod
    def setUpClass(cls):
        cls.vectors = _load_vectors()

    def test_key_rules(self):
        for case in self.vectors["keys"]:
            key = next(iter(case["input"]))
            with self.subTest(id=case["id"], key=key):
                self.assertEqual(is_sensitive_key(key), case["expected"])

    def test_cmdline_rules(self):
        for case in self.vectors["cmdline"]:
            args = case["input"]["args"]
            with self.subTest(id=case["id"]):
                self.assertEqual(mask_cmdline(args), case["expected"])

    def test_filter_rules(self):
        for case in self.vectors["filter"]:
            raw = case["input"]["raw"]
            with self.subTest(id=case["id"]):
                self.assertEqual(filter_sensitive(raw), case["expected"])

    def test_source_id_has_no_colon(self):
        """B 侧 `probe_resource_id` 拒收含冒号的 sourceId（SENSITIVE_DATA §8.4）。

        process 的 `starttime+pid`、ai_service 的 `服务名+端口` 都必须用 `.`
        连接，否则全局 ID `{kind}:probe:{agentId}:{sourceId}` 会多出一段无法解析。
        """
        pid = process_source_id("12345", 678)
        svc = ai_service_source_id("vllm", 8000)
        self.assertNotIn(":", pid)
        self.assertNotIn(":", svc)
        self.assertEqual(pid, "12345.678")
        self.assertEqual(svc, "vllm.8000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
