"""资源 ID 契约测试。

契约见 `docs/DATA_MODEL.md` §3 与 `ADR-0002`（Accepted）。三条不可违背的约束：

1. ID **不透明** —— 消费方不得解析结构
2. ID **不可变** —— 资源重建视为新资源
3. ID **不承载语义** —— 不得从中推断状态/时间/健康度

另覆盖成员 A 确认的探针侧 `sourceId` 规则（`API_CONTRACT.md` §3.3.1）。
"""

from __future__ import annotations

import pytest

from app.normalize.ids import (
    AUTHORITATIVE_SOURCE,
    InvalidResourceId,
    ResourceRef,
    build_resource_id,
    check_authoritative,
    parse_resource_id,
    probe_resource_id,
    zsvirt_resource_id,
)


class TestBuild:
    def test_zsvirt_vm_id(self):
        uuid = "3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"
        assert zsvirt_resource_id("vm", uuid) == f"vm:zsvirt:{uuid}"

    def test_probe_resource_id_has_agent_segment(self):
        """探针 ID 形如 {kind}:probe:{agentId}:{sourceId} —— 共四段。"""
        rid = probe_resource_id("probe-3f2a9c10", ResourceRef("container", "web-0"))
        assert rid == "container:probe:probe-3f2a9c10:web-0"
        assert rid.count(":") == 3, "探针 ID 必须是四段"

    def test_parse_round_trip_for_probe_id(self):
        """用 maxsplit=2 解析，agentId 与 sourceId 一起留在第三段。"""
        rid = probe_resource_id("probe-3f2a9c10", ResourceRef("process", "12345.678"))
        kind, source, remainder = parse_resource_id(rid)
        assert kind == "process"
        assert source == "probe"
        assert remainder == "probe-3f2a9c10:12345.678"

    def test_probe_rejects_colon_in_source_id(self):
        """`process` 的 starttime+pid 若用 ':' 连接会产生五段 ID，必须拒绝。

        约定用 '.' 连接：`starttime.pid`。
        """
        with pytest.raises(InvalidResourceId, match="must not contain"):
            probe_resource_id("probe-1", ResourceRef("process", "12345:678"))

    def test_process_source_id_with_dot_is_accepted(self):
        rid = probe_resource_id("probe-1", ResourceRef("process", "12345.678"))
        assert rid == "process:probe:probe-1:12345.678"

    def test_rejects_unknown_kind(self):
        with pytest.raises(InvalidResourceId, match="unknown resource kind"):
            build_resource_id("database", "probe", "x")

    def test_rejects_unknown_source(self):
        with pytest.raises(InvalidResourceId, match="unknown source"):
            build_resource_id("vm", "kafka", "x")

    def test_rejects_empty_source_id(self):
        with pytest.raises(InvalidResourceId, match="must not be empty"):
            build_resource_id("vm", "zsvirt", "")

    def test_rejects_colon_in_source_id(self):
        """含冒号会导致 ID 无法拆回三段，必须拒绝。"""
        with pytest.raises(InvalidResourceId, match="must not contain"):
            build_resource_id("vm", "zsvirt", "a:b")

    def test_probe_rejects_colon_in_agent_id(self):
        with pytest.raises(InvalidResourceId, match="agent_id must not contain"):
            probe_resource_id("probe:x", ResourceRef("container", "web-0"))

    def test_probe_rejects_empty_agent_id(self):
        with pytest.raises(InvalidResourceId, match="agent_id must not be empty"):
            probe_resource_id("", ResourceRef("container", "web-0"))


class TestParse:
    @pytest.mark.parametrize(
        "bad",
        [
            "vm-zsvirt-uuid",  # 分隔符错误
            "vm:zsvirt",  # 缺一段
            "database:zsvirt:uuid",  # kind 非法
            "vm:kafka:uuid",  # source 非法
        ],
    )
    def test_rejects_malformed(self, bad):
        with pytest.raises(InvalidResourceId):
            parse_resource_id(bad)

    def test_all_frozen_kinds_are_accepted(self):
        """F-04 已冻结的 9 种 kind 都要能拼装与解析。"""
        for kind in AUTHORITATIVE_SOURCE:
            rid = build_resource_id(kind, "manual", "x1")
            assert parse_resource_id(rid) == (kind, "manual", "x1")


class TestAuthoritativeSource:
    def test_platform_kinds_are_zsvirt_authoritative(self):
        for kind in ("host", "gpu", "vgpu", "vm"):
            assert check_authoritative(kind, "zsvirt") is True
            assert check_authoritative(kind, "probe") is False

    def test_vm_internal_kinds_are_probe_authoritative(self):
        for kind in ("container", "process", "ai_service", "agent", "task"):
            assert check_authoritative(kind, "probe") is True
            assert check_authoritative(kind, "zsvirt") is False

    def test_manual_always_allowed(self):
        """manual 是演示期兜底通道，任何类型都放行。"""
        for kind in AUTHORITATIVE_SOURCE:
            assert check_authoritative(kind, "manual") is True


class TestIdContractInvariants:
    def test_id_is_stable_for_same_inputs(self):
        """幂等：同样输入必得同样 ID（不可变约束的基础）。"""
        a = probe_resource_id("probe-1", ResourceRef("container", "web-0"))
        b = probe_resource_id("probe-1", ResourceRef("container", "web-0"))
        assert a == b

    def test_different_kinds_do_not_collide(self):
        """同一 sourceId 在不同 kind 下必须得到不同 ID。"""
        ids = {
            build_resource_id(kind, "probe", "x1")
            for kind in ("container", "process", "ai_service")
        }
        assert len(ids) == 3

    def test_different_sources_do_not_collide(self):
        """同一 sourceId 在不同来源下必须得到不同 ID。"""
        assert build_resource_id("vm", "zsvirt", "x") != build_resource_id("vm", "manual", "x")

    def test_kinds_in_id_match_frozen_enum(self):
        """ID 中出现的 kind 必须来自已冻结枚举，不得是自由字符串。"""
        from app.graph.algorithms import RESOURCE_KINDS

        assert set(AUTHORITATIVE_SOURCE) == set(RESOURCE_KINDS), (
            "AUTHORITATIVE_SOURCE 与冻结的 RESOURCE_KINDS 不一致 —— "
            "说明枚举在某处被改动了，需要同步"
        )
