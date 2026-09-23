"""平台层命名空间桥接测试（关闭 D-101）。

这一层的价值全在"能把平台命名空间真的写出来"，所以测试围绕三件事：

1. **ID 拼装正确** —— `{kind}:zsvirt:{uuid}`，且 GPU 挂在宿主下（否则图在
   平台层就断了）。
2. **落库不改 `status`** —— 那条铁律：`status` 由来源系统拥有，指标采集不是
   它的来源。
3. **建不出来的东西如实记在 `unresolved` 里** —— VM UUID 未知（X-09）就
   不产出 vm 资源，而不是编一个 ID 把图连起来。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.enums import ResourceKind
from app.zsvirt import GpuMetricReading
from app.zsvirt.harvest import (
    HarvestPlan,
    ResourceUpdate,
    harvest_once,
    map_reading,
    map_readings,
    persist_plan,
    zsvirt_host_resource_id,
    zsvirt_vm_resource_id,
)
from tests.test_zwatch import SERIAL, healthy_reading

HOST_UUID = "host-uuid-1"
VM_UUID = "3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"
NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


# ================================================================ ID 拼装


class TestPlatformNamespaceIds:
    def test_host_id_uses_the_zsvirt_namespace(self) -> None:
        assert zsvirt_host_resource_id(HOST_UUID) == f"host:zsvirt:{HOST_UUID}"

    def test_vm_id_uses_the_zsvirt_namespace(self) -> None:
        assert zsvirt_vm_resource_id(VM_UUID) == f"vm:zsvirt:{VM_UUID}"

    def test_gpu_id_uses_the_serial_not_the_pci_address(self) -> None:
        """PCI 地址形如 `0000:01:00.0` **含冒号**，而 ID 拆不回四段。

        用序列号做 `sourceId` 是有依据的：答复说标签里"部分序列含
        `GpuSerialNumber`"，且它是设备的稳定标识。
        """
        plan = map_reading(healthy_reading())
        gpu = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value)
        assert gpu.resource_id == f"gpu:zsvirt:{SERIAL}"
        assert ":" not in gpu.resource_id.split(":zsvirt:", 1)[1], "sourceId 含冒号会让 ID 无法解析"


# ================================================================ 映射


class TestMapReading:
    def test_host_and_gpu_are_both_emitted(self) -> None:
        plan = map_reading(healthy_reading())
        kinds = [u.kind for u in plan.updates]
        assert ResourceKind.HOST.value in kinds
        assert ResourceKind.GPU.value in kinds

    def test_gpu_is_parented_to_the_platform_host(self) -> None:
        """直通拓扑是 host → gpu → vm；GPU 的父必须是平台宿主。"""
        plan = map_reading(healthy_reading())
        gpu = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value)
        assert gpu.parent_id == f"host:zsvirt:{HOST_UUID}"

    def test_attributes_match_the_existing_consumers(self) -> None:
        """属性名必须与 `/api/v1/workloads` 的读取键一致。

        否则"接入了真实 GPU 指标"在界面上依然是一片 —：数据写进去了，
        但没人读得出来。
        """
        plan = map_reading(healthy_reading())
        gpu = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value)
        assert gpu.attributes["utilizationPct"] == 37.5
        assert gpu.attributes["memUsagePct"] == 41.2
        assert gpu.attributes["origin"] == "zsvirt-zwatch"
        assert gpu.attributes["serialNumber"] == SERIAL

    def test_attribute_names_are_readable_by_the_workloads_endpoint(self) -> None:
        """用端点真正的读取函数核对，而不是核对字符串字面量。

        这是"两处各写一遍、慢慢漂移"最容易发生的地方。
        """
        from app.api.workloads import _first_float, _first_int

        plan = map_reading(healthy_reading())
        attrs = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value).attributes
        assert _first_int(attrs, "memTotalBytes", "memory", "mem_total_bytes") == (
            24576 * 1024 * 1024
        )
        assert _first_float(attrs, "utilizationPct", "utilization_pct") == 37.5

    def test_missing_byte_count_is_not_written(self) -> None:
        """`memUsedBytes` 缺失时**不写这个键**。

        写 `None` 会让"没采到"在库里看起来像一个值；而写一个反推的字节数
        更糟 —— 那是把推算值当观测值（命题方答复第 07 条）。
        """
        plan = map_reading(healthy_reading())
        attrs = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value).attributes
        assert "memUsedBytes" not in attrs

    def test_passthrough_is_labelled_as_card_level(self) -> None:
        """直通模式必须标注 `memUsageScope=card`。

        不标的话，界面上一个 41% 会被读成"这个虚拟机占了 41%"，而这在直通下
        是**证明不了**的结论。
        """
        plan = map_reading(healthy_reading())
        attrs = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value).attributes
        assert attrs["attribution"] == "passthrough"
        assert attrs["memUsageScope"] == "card"

    def test_asset_fields_are_merged_in(self) -> None:
        plan = map_reading(
            healthy_reading(),
            asset={
                "model": "NVIDIA Quadro RTX 6000",
                "power": 260,
                "isDriverLoaded": True,
            },
        )
        attrs = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value).attributes
        assert attrs["model"] == "NVIDIA Quadro RTX 6000"
        assert attrs["power"] == 260

    def test_asset_does_not_override_a_measured_value(self) -> None:
        """资产接口的容量不能覆盖指标侧已有的值（两者同义时以观测为准）。"""
        plan = map_reading(healthy_reading(), asset={"memTotalBytes": 1})
        attrs = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value).attributes
        assert attrs["memTotalBytes"] == 24576 * 1024 * 1024

    def test_vm_resource_is_emitted_when_uuid_is_known(self) -> None:
        plan = map_reading(healthy_reading(), vm_uuid=VM_UUID)
        vm = next(u for u in plan.updates if u.kind == ResourceKind.VM.value)
        assert vm.resource_id == f"vm:zsvirt:{VM_UUID}"
        assert vm.parent_id == f"gpu:zsvirt:{SERIAL}"

    def test_vm_resource_is_not_invented_and_reason_is_recorded(self) -> None:
        """X-09 未解决 ⇒ 不产出 vm，并把原因写进 `unresolved`。"""
        plan = map_reading(healthy_reading())
        assert not any(u.kind == ResourceKind.VM.value for u in plan.updates)
        assert any("X-09" in r for r in plan.unresolved), plan.unresolved

    def test_missing_host_uuid_is_reported(self) -> None:
        plan = map_reading(healthy_reading(labels={"GpuSerialNumber": SERIAL}))
        assert any("HostUuid" in r for r in plan.unresolved)
        gpu = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value)
        assert gpu.parent_id is None, "宿主建不出来时 GPU 挂顶层，但不能因此不写"

    def test_pci_only_reading_falls_back_and_says_so(self) -> None:
        """只有 PCI 地址时兜底，但要**点明** ID 与平台侧不一致。"""
        plan = map_reading(
            healthy_reading(
                gpu_serial="",
                labels={"HostUuid": HOST_UUID, "PciDeviceAddress": "0000:01:00.0"},
            )
        )
        gpu = next(u for u in plan.updates if u.kind == ResourceKind.GPU.value)
        assert gpu.resource_id == "gpu:zsvirt:0000-01-00.0"
        assert any("兜底" in r for r in plan.unresolved), plan.unresolved

    def test_reading_without_any_gpu_identity_yields_nothing(self) -> None:
        plan = map_reading(healthy_reading(gpu_serial="", labels={"HostUuid": HOST_UUID}))
        assert not any(u.kind == ResourceKind.GPU.value for u in plan.updates)
        assert any("无法建立" in r for r in plan.unresolved)


class TestMapReadings:
    def test_same_host_from_two_readings_is_merged(self) -> None:
        """两块卡在同一次采集里会各产出一条宿主 —— 必须去重。"""
        readings = [
            healthy_reading(),
            healthy_reading(
                gpu_serial="GPU-B",
                labels={"HostUuid": HOST_UUID, "GpuSerialNumber": "GPU-B"},
            ),
        ]
        plan = map_readings(readings)
        ids = [u.resource_id for u in plan.updates]
        assert len(ids) == len(set(ids)), f"同一资源被写多次：{ids}"
        assert ids.count(f"host:zsvirt:{HOST_UUID}") == 1
        assert len(plan.updates) == 3, "1 个宿主 + 2 块卡"

    def test_empty_input_yields_empty_plan(self) -> None:
        plan = map_readings([])
        assert plan.is_empty
        assert plan.sampled_at is None


class TestMergeAttributes:
    def test_existing_attributes_are_preserved(self) -> None:
        """合并而不是覆盖：资产接口写的型号不能被指标采集抹掉。"""
        update = ResourceUpdate(
            resource_id="gpu:zsvirt:x", kind="gpu", attributes={"utilizationPct": 10.0}
        )
        merged = update.merge_attributes({"model": "A10", "memTotalBytes": 100})
        assert merged == {"model": "A10", "memTotalBytes": 100, "utilizationPct": 10.0}

    def test_new_values_win_on_conflict(self) -> None:
        update = ResourceUpdate(
            resource_id="gpu:zsvirt:x", kind="gpu", attributes={"utilizationPct": 10.0}
        )
        assert update.merge_attributes({"utilizationPct": 1.0})["utilizationPct"] == 10.0


# ================================================================ 落库


class TestPersistPlan:
    def test_creates_platform_resources(self, db_session: Session) -> None:
        plan = map_reading(healthy_reading())
        result = persist_plan(db_session, plan, seen_at=NOW)
        db_session.commit()

        assert result.resources == 2
        assert f"gpu:zsvirt:{SERIAL}" in result.created
        assert f"host:zsvirt:{HOST_UUID}" in result.created

    def test_second_harvest_updates_instead_of_creating(self, db_session: Session) -> None:
        persist_plan(db_session, map_reading(healthy_reading()), seen_at=NOW)
        db_session.commit()

        result = persist_plan(db_session, map_reading(healthy_reading()), seen_at=NOW)
        db_session.commit()
        assert result.created == []
        assert len(result.updated) == 2

    def test_status_is_never_written_by_the_harvester(self, db_session: Session) -> None:
        """**铁律**：`status` 由来源系统拥有。

        平台资源的 status 来自 ZSvirt 资产接口；指标采集不是它的来源。
        若这里顺手写一个 status，就会出现"一次指标采集把 running 改成 unknown"
        这类静默数据损坏。
        """
        from app.models import Resource

        persist_plan(db_session, map_reading(healthy_reading()), seen_at=NOW)
        db_session.commit()
        before = db_session.get(Resource, f"gpu:zsvirt:{SERIAL}")
        assert before is not None
        original_status = before.status

        persist_plan(db_session, map_reading(healthy_reading()), seen_at=NOW)
        db_session.commit()
        db_session.expire_all()
        after = db_session.get(Resource, f"gpu:zsvirt:{SERIAL}")
        assert after is not None
        assert after.status == original_status

    def test_attributes_are_merged_across_harvests(self, db_session: Session) -> None:
        """第一次采到容量，第二次只有利用率 —— 容量不能被抹掉。"""
        from app.models import Resource

        persist_plan(
            db_session,
            map_reading(healthy_reading(), asset={"model": "RTX 6000"}),
            seen_at=NOW,
        )
        db_session.commit()

        second = map_reading(healthy_reading())
        # 第二次不带资产信息
        second.updates = [
            ResourceUpdate(
                resource_id=u.resource_id,
                kind=u.kind,
                attributes={"utilizationPct": 88.0},
                parent_id=u.parent_id,
            )
            if u.kind == ResourceKind.GPU.value
            else u
            for u in second.updates
        ]
        persist_plan(db_session, second, seen_at=NOW)
        db_session.commit()
        db_session.expire_all()

        row = db_session.get(Resource, f"gpu:zsvirt:{SERIAL}")
        assert row is not None
        assert row.attributes["model"] == "RTX 6000", "第一次采到的型号被覆盖了"
        assert row.attributes["utilizationPct"] == 88.0, "新观测值没写进去"

    def test_unresolved_reasons_are_carried_into_the_result(self, db_session: Session) -> None:
        result = persist_plan(db_session, map_reading(healthy_reading()), seen_at=NOW)
        assert any("X-09" in r for r in result.unresolved)

    def test_empty_plan_writes_nothing(self, db_session: Session) -> None:
        result = persist_plan(db_session, HarvestPlan(), seen_at=NOW)
        assert result.resources == 0
        assert result.created == []

    def test_parent_is_written_before_child(self, db_session: Session) -> None:
        """外键要求父先存在。顺序错了会直接违约。"""
        plan = map_reading(healthy_reading(), vm_uuid=VM_UUID)
        result = persist_plan(db_session, plan, seen_at=NOW)
        db_session.commit()
        assert result.created[0] == f"host:zsvirt:{HOST_UUID}"
        assert f"gpu:zsvirt:{SERIAL}" in result.created
        assert f"vm:zsvirt:{VM_UUID}" in result.created


class TestHarvestOnce:
    def test_harvest_writes_real_readings_into_the_graph(self, db_session: Session) -> None:
        """端到端：一次采集 → 平台命名空间的资源出现在库里。"""

        class FakeProvider:
            def read_all(self) -> list[GpuMetricReading]:
                return [healthy_reading()]

            def assets(self) -> list[object]:
                class Asset:
                    serial_number = SERIAL
                    mem_total_bytes = 24576 * 1024 * 1024
                    power_watts = 260
                    is_driver_loaded = True
                    pci_address = "0000:01:00.0"
                    model = "NVIDIA Quadro RTX 6000"

                return [Asset()]

        result = harvest_once(db_session, provider=FakeProvider(), seen_at=NOW)
        db_session.commit()

        assert result.resources == 2
        from app.models import Resource

        gpu = db_session.get(Resource, f"gpu:zsvirt:{SERIAL}")
        assert gpu is not None
        assert gpu.kind == "gpu"
        assert gpu.attributes["model"] == "NVIDIA Quadro RTX 6000"
        assert gpu.attributes["utilizationPct"] == 37.5

    def test_harvest_of_an_unconfigured_provider_surfaces_the_error(
        self, db_session: Session
    ) -> None:
        """未配置的渠道要把 `GpuMetricsUnavailable` 抛上来，而不是静默写 0 条。"""
        from app.zsvirt import GpuMetricsUnavailable, ZWatchProvider

        with pytest.raises(GpuMetricsUnavailable):
            harvest_once(db_session, provider=ZWatchProvider())
