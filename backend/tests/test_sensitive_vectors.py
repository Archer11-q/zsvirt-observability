"""共享测试向量驱动的一致性测试。

**为什么要这个文件**：成员 A 的探针（`agents/probe/sensitive.py`）与
B 侧的 `app/normalize/sensitive.py` 必须**逐条行为一致**。否则会出现
"探针掩码了、B 漏了"（缝隙）或"B 过度掩码、丢了诊断价值"（信息损失）。

做法：两侧实现都接受同一份向量文件
（`shared/sensitive_vectors.json`）的校验。本文件是 B 侧的校验器；
成员 A 可用 `agents/probe/` 内的脚本跑同一份向量。

向量文件刻意是**纯 ASCII**，且格式简单到不需要 JSON 库也能解析
（形如 `CASE <id> <expected> <JSON input>`），
这样零依赖的探针也能读取。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.normalize.sensitive import (
    filter_sensitive,
    is_sensitive_key,
    mask_cmdline,
)

VECTORS_PATH = Path(__file__).resolve().parents[2] / "shared" / "sensitive_vectors.json"


def _load() -> dict[str, Any]:
    assert VECTORS_PATH.exists(), (
        f"共享测试向量缺失: {VECTORS_PATH}\n该文件是 A/B 两侧脱敏规则一致性的唯一依据，不应被删除。"
    )
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


VECTORS = _load()


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in items]


# ---------------------------------------------------------------- 键名规则


@pytest.mark.parametrize("case", VECTORS["keys"], ids=_ids(VECTORS["keys"]))
def test_is_sensitive_key_vectors(case: dict[str, Any]) -> None:
    """键名命中规则必须与向量一致。

    正例（k01–k15）与反例（k20–k29）**同等重要**：反例防止误杀。
    唯一的已知折中是 k26（`cookie_count` 因含子串 `cookie` 被判敏感），
    见 `docs/SENSITIVE_DATA.md` §9。
    """
    (key,) = case["input"].keys()
    assert is_sensitive_key(key) is case["expected"], (
        f"{case['id']}: is_sensitive_key({key!r}) "
        f"期望 {case['expected']}，实际 {is_sensitive_key(key)}"
    )


# ---------------------------------------------------------------- 命令行掩码


@pytest.mark.parametrize("case", VECTORS["cmdline"], ids=_ids(VECTORS["cmdline"]))
def test_mask_cmdline_vectors(case: dict[str, Any]) -> None:
    """cmdline 掩码必须与向量一致。

    关键用例：

    - c10：`--token --port 8000` → **不得**把 `--port` 当值吞掉
    - c11：`--token` 后面没值 → 原样保留，不添加掩码
    - c13：`--tokenizer bpe` → 按当前规则**会被误命中**（已知折中，见 §9）
    """
    got = mask_cmdline(case["input"]["args"])
    assert got == case["expected"], (
        f"{case['id']}: mask_cmdline({case['input']['args']}) 期望 {case['expected']}，实际 {got}"
    )


# ---------------------------------------------------------------- 浅层过滤


@pytest.mark.parametrize("case", VECTORS["filter"], ids=_ids(VECTORS["filter"]))
def test_filter_sensitive_vectors(case: dict[str, Any]) -> None:
    """**递归**过滤语义必须与向量一致。

    向量校验的是 `filter_sensitive` 的完整行为，**对 dict 与 list 都递归**：

    - f03 / f05：嵌套 dict 中的敏感键必须清掉
    - f04 / f07 / f09：**list 元素中的敏感键也必须清掉**
      —— Docker inspect 返回的 `Env` 数组就是典型形态，
      凭据藏在数组元素里是真实泄漏路径，不能漏。

    注意这与成员 A 的探针实现有**已知差异**：探针的
    `filter_sensitive` 只处理 dict 第一层且不递归。B 侧更严的原因是
    B 是兜底关口 —— 不能因为探针漏了就跟着漏。见 `docs/SENSITIVE_DATA.md` §9。
    """
    got = filter_sensitive(case["input"]["raw"])
    assert got == case["expected"], (
        f"{case['id']}: filter_sensitive({case['input']['raw']}) "
        f"期望 {case['expected']}，实际 {got}"
    )


@pytest.mark.parametrize("case", VECTORS["filter"], ids=_ids(VECTORS["filter"]))
def test_filter_sensitive_leaves_no_sensitive_key_anywhere(case: dict[str, Any]) -> None:
    """不变量：过滤后**任何深度**都不得残留敏感键。

    这是比逐例比对更强的断言 —— 即使向量漏写了某个用例，
    这条也能抓住"深处还留着凭据"的情况。
    """

    def collect_keys(obj: Any) -> list[str]:
        if isinstance(obj, dict):
            out: list[str] = []
            for k, v in obj.items():
                out.append(str(k))
                out.extend(collect_keys(v))
            return out
        if isinstance(obj, (list, tuple)):
            out = []
            for item in obj:
                out.extend(collect_keys(item))
            return out
        return []

    got = filter_sensitive(case["input"]["raw"])
    leaked = [k for k in collect_keys(got) if is_sensitive_key(k)]
    assert not leaked, f"{case['id']}: 过滤后仍残留敏感键 {leaked}"


# ---------------------------------------------------------------- 全局断言


def test_all_vector_ids_are_unique() -> None:
    """向量 id 必须唯一，否则失败信息无法定位到具体用例。"""
    all_ids = _ids(VECTORS["keys"]) + _ids(VECTORS["cmdline"]) + _ids(VECTORS["filter"])
    assert len(all_ids) == len(set(all_ids)), "向量 id 存在重复"


def test_vector_file_is_ascii_only() -> None:
    """向量文件必须是纯 ASCII —— 零依赖的探针要能不靠 JSON 库读它。"""
    raw = VECTORS_PATH.read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:  # pragma: no cover - 失败时给出定位
        raise AssertionError(
            f"向量文件含非 ASCII 字符（字节偏移 {exc.start}）：请保持纯 ASCII，以便探针侧零依赖解析"
        ) from exc


def test_vectors_cover_both_positive_and_negative_keys() -> None:
    """必须同时包含正例与反例，否则"一致"可能只是"都错"。"""
    keys = VECTORS["keys"]
    positives = [c for c in keys if c["expected"] is True]
    negatives = [c for c in keys if c["expected"] is False]
    assert len(positives) >= 5, "正例太少"
    assert len(negatives) >= 5, "反例太少（无反例则无法发现误杀）"


# ---------------------------------------------------------------- 幂等与不变量


def test_mask_cmdline_is_idempotent() -> None:
    """已掩码的 cmdline 再次处理结果不变（掩码值不含敏感参数名）。"""
    once = mask_cmdline(["--password=secret", "--token", "abc"])
    twice = mask_cmdline(once)
    assert once == twice


def test_filter_sensitive_is_idempotent() -> None:
    raw = {"image": "vllm", "API_KEY": "leak", "nested": {"token": "t", "keep": 1}}
    once = filter_sensitive(raw)
    assert filter_sensitive(once) == once


def test_deep_filter_sensitive_is_idempotent() -> None:
    raw = {"image": "vllm", "nested": {"token": "t", "keep": 1}}
    once = filter_sensitive(raw)
    assert filter_sensitive(once) == once


def test_filter_sensitive_does_not_mutate_input() -> None:
    """不得修改入参 —— 原始载荷在拒收/隔离路径上可能还要用到。"""
    raw = {"image": "vllm", "API_KEY": "leak"}
    snapshot = json.loads(json.dumps(raw))
    filter_sensitive(raw)
    assert raw == snapshot


def test_filter_sensitive_does_not_mutate_nested_input() -> None:
    raw = {"a": {"API_KEY": "leak", "keep": 1}, "items": [{"token": "t", "keep": 2}]}
    snapshot = json.loads(json.dumps(raw))
    filter_sensitive(raw)
    assert raw == snapshot


def test_filter_sensitive_handles_non_dict_input() -> None:
    assert filter_sensitive(None) is None
    assert filter_sensitive("text") == "text"
    assert filter_sensitive(42) == 42
    assert filter_sensitive([]) == []


def test_mask_cmdline_handles_non_list_input() -> None:
    assert mask_cmdline(None) == []
    assert mask_cmdline("--password x") == []
    assert mask_cmdline(123) == []


def test_mask_cmdline_preserves_non_string_items() -> None:
    assert mask_cmdline(["python", 42, None]) == ["python", "42", "None"]


# ---------------------------------------------------------------- redact_event


def test_redact_event_removes_nested_secrets_recursively() -> None:
    """`redact_event` 是 B 侧实际使用的兜底入口，必须递归清理。"""
    from app.normalize.sensitive import redact_event

    payload = {
        "type": "container.oom_killed",
        "severity": "critical",
        "attributes": {
            "image": "vllm/vllm-openai",
            "cmdline": ["python", "serve.py", "--token", "abc", "--port", "8000"],
            "nested": {"API_KEY": "leak", "keep": 1},
        },
        "raw": {"docker": {"env": {"AWS_SECRET_ACCESS_KEY": "x"}, "image": "vllm"}},
        "message": "connect failed to postgresql://user:hunter2@10.0.0.5:5432/db",
    }
    out = redact_event(payload)

    # 嵌套凭据被清掉
    assert "nested" in out["attributes"]
    assert "API_KEY" not in out["attributes"]["nested"]
    assert out["attributes"]["nested"]["keep"] == 1
    assert "AWS_SECRET_ACCESS_KEY" not in out["raw"]["docker"]["env"]
    assert out["raw"]["docker"]["image"] == "vllm"

    # cmdline 保留结构但掩码凭据值
    assert out["attributes"]["cmdline"] == [
        "python",
        "serve.py",
        "--token",
        "***",
        "--port",
        "8000",
    ]

    # 连接串密码被掩码，但主机与库名保留（诊断需要）
    assert "hunter2" not in out["message"]
    assert "10.0.0.5" in out["message"]


def test_redact_event_does_not_mutate_input() -> None:
    from app.normalize.sensitive import redact_event

    payload = {"raw": {"API_KEY": "leak"}, "attributes": {"cmdline": ["--token", "x"]}}
    snapshot = json.loads(json.dumps(payload))
    redact_event(payload)
    assert payload == snapshot


def test_redact_event_preserves_required_fields() -> None:
    """脱敏不得破坏契约必填字段（C 要求每个 Event 带 severity）。"""
    from app.normalize.sensitive import redact_event

    payload = {
        "type": "gpu.memory.exhausted",
        "severity": "critical",
        "occurredAt": "2026-09-17T12:00:00Z",
        "resourceRef": {"kind": "vgpu", "sourceId": "v0"},
    }
    out = redact_event(payload)
    assert out == payload


def test_mask_secrets_in_text_masks_known_key_shapes() -> None:
    from app.normalize.sensitive import mask_secrets_in_text

    assert "sk-" not in mask_secrets_in_text("key=sk-abcdefghijklmnop")
    assert "ghp_" not in mask_secrets_in_text("token ghp_abcdefghijkl")
    assert "AKIA" not in mask_secrets_in_text("aws AKIAIOSFODNN7EXAMPLE")
    # 无关文本不受影响
    assert mask_secrets_in_text("all good") == "all good"
