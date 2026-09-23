"""跨层关联端到端测试（任务 6.2）+ 事件类型覆盖对账（任务 6.3）。

来源：成员 A 在 `agents/docs/EVENT_COVERAGE.md` 里给 B 的建议 3 与 4，以及
`agents/probe/fixtures/README.md` 里那句关键说明：

> **场景一**探针只含 VM 内症状（`process.io_wait.high`）；根因事件
> `gpu.memory.exhausted` 由 B 的 zsvirt-adapter 产生（D-028 GPU 三层分工）。

所以场景一的跨层结论**不能**靠探针载荷单独得出，必须把 B 的 GPU 渠道数据与探针
症状叠在同一条链上。本文件验证这件事，并把两个**尚未打通的环节**断言成事实，
而不是假装它们已经工作。
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.enums import EventType, ResourceKind
from app.models import Base

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "agents/probe/fixtures"

#: 探针样例里的固定基准（见 A 的 fixtures/README.md）
VM_ID = "vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"
AGENT_ID = "probe-3f2a9c10"
SELF = "self"  # 探针给 VM 的 sourceId（见 API_CONTRACT §3.3.1）

BASE_TIME = datetime(2026, 9, 17, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def client(db_engine: Engine):
    from sqlalchemy.orm import sessionmaker

    from app.db import get_db
    from app.ingest.limits import reset_metrics
    from app.main import app

    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    reset_metrics()
    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _clean(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        if names:
            conn.execute(text(f"TRUNCATE {names} CASCADE"))


def post(client: TestClient, payload: dict) -> dict:
    response = client.post("/api/v1/ingest/batch", json=payload)
    assert response.status_code == 200, response.text[:400]
    return response.json()["data"]


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def platform_layer_batch(*, batch_id: str = "01M2ZPLATFORM0000000000001") -> dict:
    """ZSvirt 侧权威层：host → gpu → vgpu → vm。

    **刻意放在探针命名空间里**（`agentId` 与样例相同）：这样 `sourceId` 能互相
    解析，图才是连通的。真实部署里这些资源来自 ZSvirt 资产 API，而两者之间的
    ID 桥接**尚未实现** —— 见 `test_platform_namespace_bridge_is_missing`。
    """
    return {
        "agentId": AGENT_ID,
        "vmId": VM_ID,
        "agentVersion": "0.1.0",
        "batchId": batch_id,
        "sentAt": BASE_TIME.isoformat().replace("+00:00", "Z"),
        "resources": [
            {
                "kind": ResourceKind.HOST.value,
                "sourceId": "h0",
                "name": "node-01",
                "status": "running",
            },
            {
                "kind": ResourceKind.GPU.value,
                "sourceId": "g0",
                "name": "A10-0",
                "parentSourceId": "h0",
                "status": "running",
                "attributes": {"memTotalBytes": 24 * 1024**3, "serialNumber": "SIM-1"},
            },
            {
                "kind": ResourceKind.VGPU.value,
                "sourceId": "v0",
                "name": "A10-0-1g",
                "parentSourceId": "g0",
                "status": "running",
            },
            {
                "kind": ResourceKind.VM.value,
                "sourceId": SELF,
                "name": "vm0",
                "parentSourceId": "v0",
                "status": "running",
            },
        ],
        "events": [],
    }


def gpu_root_cause_batch(
    *, value: float = 98.0, batch_id: str = "01M2ZGPUROOT00000000000001"
) -> dict:
    """B 的 GPU 渠道产生的**根因事件**（D-028 的 L2/L3 层）。

    挂在与探针症状同一个 vGPU 上，因此两者能落在同一条链上。
    """
    return {
        "agentId": AGENT_ID,
        "vmId": VM_ID,
        "agentVersion": "0.1.0",
        "batchId": batch_id,
        "sentAt": "2026-09-17T08:00:12.000Z",
        "resources": [],
        "events": [
            {
                "occurredAt": "2026-09-17T08:00:12.000Z",
                "resourceRef": {"kind": ResourceKind.VGPU.value, "sourceId": "v0"},
                "type": EventType.GPU_MEMORY_EXHAUSTED.value,
                "severity": "critical",
                "message": f"vGPU 显存使用率 {value:.0f}%",
                "metrics": {
                    "value": value,
                    "self_vgpu_memory_usage": value,
                    "host_gpu_memory_usage": value,
                },
                "raw": {},
            }
        ],
    }


class TestCrossLayerCorrelation:
    """A 的症状 + B 的根因 → 一条跨层结论。"""

    def test_probe_symptom_and_gpu_root_cause_share_one_chain(
        self, client: TestClient, db_engine: Engine
    ) -> None:
        """两层数据必须落在**同一张图**上，否则跨层关联无从谈起。"""
        post(client, platform_layer_batch())
        post(client, fixture("gpu_memory_exhausted"))
        post(client, gpu_root_cause_batch())

        with db_engine.connect() as conn:
            pairs = list(conn.execute(text("select parent_id, child_id from resource_edge")).all())

        # host → gpu → vgpu → vm 是平台层内建链
        assert any(p.startswith("host:") and c.startswith("gpu:") for p, c in pairs), pairs
        assert any(p.startswith("gpu:") and c.startswith("vgpu:") for p, c in pairs), pairs
        assert any(p.startswith("vgpu:") and c.startswith("vm:") for p, c in pairs), pairs

    def test_gpu_root_cause_raises_a_critical_alert(self, client: TestClient) -> None:
        """GPU 根因事件必须触发 critical 告警 —— 这是跨层链的起点。"""
        post(client, platform_layer_batch())
        post(client, fixture("gpu_memory_exhausted"))
        post(client, gpu_root_cause_batch())

        alerts = client.get("/api/v1/alerts", params={"limit": 50}).json()["data"]["items"]
        gpu = [a for a in alerts if a["ruleId"] == "R-GPU-MEM-001"]
        assert len(gpu) == 1, [a["ruleId"] for a in alerts]
        assert gpu[0]["severity"] == "critical"
        assert gpu[0]["resourceId"] == f"vgpu:probe:{AGENT_ID}:v0"

    def test_diagnosis_names_the_gpu_cause(self, client: TestClient) -> None:
        """端到端结论：`GPU_MEMORY_EXHAUSTED`，且置信来自 GPU 规则。"""
        post(client, platform_layer_batch())
        post(client, fixture("gpu_memory_exhausted"))
        post(client, gpu_root_cause_batch())

        items = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        assert items, "GPU 根因告警应触发自动诊断"

        gpu = next(d for d in items if d["rootCause"] == "GPU_MEMORY_EXHAUSTED")
        rules = {item["ruleId"] for item in gpu["confidenceBreakdown"]}
        assert "R-GPU-SELF-001" in rules, rules
        assert gpu["confidence"] >= 0.30
        assert gpu["recommendation"], "有根因必须有处置建议"

    def test_evidence_contains_both_layers(self, client: TestClient) -> None:
        """证据里必须同时能看到两层的观测，这才叫"关联"。"""
        post(client, platform_layer_batch())
        post(client, fixture("gpu_memory_exhausted"))
        post(client, gpu_root_cause_batch())

        items = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        assert items
        names = {e["name"] for d in items for e in d["evidence"]}
        assert EventType.GPU_MEMORY_EXHAUSTED.value in names, names

    def test_without_the_gpu_channel_there_is_no_gpu_conclusion(self, client: TestClient) -> None:
        """**对照组**：只灌探针症状时不得得出 GPU 根因。

        这条比正向用例更重要 —— 它证明上面的结论确实来自跨层关联，
        而不是"只要有 I/O 等待就报 GPU 显存耗尽"。
        """
        post(client, platform_layer_batch())
        post(client, fixture("gpu_memory_exhausted"))

        items = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        causes = {d["rootCause"] for d in items}
        assert "GPU_MEMORY_EXHAUSTED" not in causes, causes

    def test_impact_scope_covers_the_upper_layers(self, client: TestClient) -> None:
        """影响范围要向上覆盖到 vGPU（证据所在层），而不是只看容器分支。"""
        post(client, platform_layer_batch())
        post(client, fixture("gpu_memory_exhausted"))
        post(client, gpu_root_cause_batch())

        items = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        gpu = next(d for d in items if d["rootCause"] == "GPU_MEMORY_EXHAUSTED")

        # 锚点是 AI 服务，证据在 vGPU —— 后者是前者的**祖先**，而三集刻意
        # **不含离链祖先**（D-077：它提供证据所在的层，但本身没被影响）。
        # 因此"覆盖上层"要分两件事断言：
        #   1. 证据层（vGPU）必须出现在**证据**里；
        #   2. 影响范围必须沿证据向下覆盖到 VM 及以下（证据的下游后代）。
        evidence_resources = {
            e["resourceId"].split(":")[0] for e in gpu["evidence"] if e.get("resourceId")
        }
        assert "vgpu" in evidence_resources, gpu["evidence"]

        affected = set(gpu["affectedResources"])
        assert any(a.startswith("vm:") for a in affected), affected

        # 离链祖先（宿主机 / GPU）不得混进 affected
        assert not any(a.startswith("host:") for a in affected), affected
        assert not any(a.startswith("gpu:") for a in affected), affected


class TestPlatformNamespaceBridgeIsWired:
    """平台命名空间桥接**已落地**（关闭 D-101）。

    这个测试类此前叫 `...IsMissing`，断言的是"整个代码库没有任何
    `zsvirt_resource_id()` 调用方"—— 一条刻意记录"我们知道它还没通"的用例，
    并注明"适配层落地后它会失败，那正是提醒我们更新它的信号"。

    命题方答复确认 ZWatch 已启用（关闭 X-07），桥接随 `app/zsvirt/harvest.py`
    落地，于是这条信号触发了：现在断言的是**相反**的事实 ——
    平台命名空间真的会被写出。
    """

    def test_platform_layer_is_wired_by_the_harvester(self) -> None:
        """`zsvirt_resource_id()` 必须有真实调用方。

        在此之前，平台层资源只能由探针代为上报（形如 `gpu:probe:<agentId>:...`），
        这在真实部署里是错的命名空间。现在 ZWatch 读数经 `harvest.py` 落图，
        写出的是 `host:zsvirt:{uuid}` / `gpu:zsvirt:{serial}`。
        """
        from app.normalize.ids import zsvirt_resource_id

        assert zsvirt_resource_id("gpu", "abc") == "gpu:zsvirt:abc"

        backend = pathlib.Path(__file__).resolve().parents[1]
        callers: list[tuple[str, int]] = []
        for path in backend.rglob("*.py"):
            if path.name == "ids.py" or "test_" in path.name:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # 找**调用**而不是提及：散文会写函数名，代码会写名字加左括号。
                # 用括号区分，比解析注释与文档字符串可靠得多。
                if "zsvirt_resource_id(" not in line:
                    continue
                if line.strip().startswith("#"):
                    continue
                callers.append((str(path.relative_to(backend)), number))

        assert callers, (
            "平台命名空间桥接没有任何调用方 —— 平台层资源又只能由探针代报了"
            "（见 docs/DECISIONS.md D-101）"
        )
        assert any("harvest" in name for name, _ in callers), callers

    def test_reading_maps_to_platform_namespace_ids(self) -> None:
        """端到端核对：一条 ZWatch 读数 → `host:zsvirt:*` + `gpu:zsvirt:*`。"""
        from app.zsvirt.harvest import map_reading
        from tests.test_zwatch import SERIAL, healthy_reading

        plan = map_reading(healthy_reading())

        by_kind = {u.kind: u for u in plan.updates}
        assert by_kind["host"].resource_id == "host:zsvirt:host-uuid-1"
        assert by_kind["gpu"].resource_id == f"gpu:zsvirt:{SERIAL}"
        assert by_kind["gpu"].parent_id == "host:zsvirt:host-uuid-1", (
            "GPU 必须挂在平台宿主下，否则图在平台层就断了"
        )

    def test_vm_resource_is_not_invented_when_uuid_is_unknown(self) -> None:
        """X-09 未解决 ⇒ **不产出** vm 资源，并如实记录原因。

        编一个 vm ID 会把图"连起来"，但连的是一个不存在的虚拟机 ——
        评测现场一旦被追问 VM 从哪来，整条跨层链的可信度就没了。
        """
        from app.zsvirt.harvest import map_reading
        from tests.test_zwatch import healthy_reading

        plan = map_reading(healthy_reading())

        assert not any(u.kind == "vm" for u in plan.updates)
        assert any("X-09" in reason for reason in plan.unresolved), plan.unresolved

    def test_probe_fixture_declares_no_vm_so_the_chain_has_a_gap(
        self, client: TestClient, db_engine: Engine
    ) -> None:
        """只灌探针样例时，容器**没有父**，因此上图是断开的。

        A 的样例里 `parentSourceId` 全是 `null` 或容器内 ID —— 探针不拥有 VM 的
        权威（`DATA_MODEL.md` §4.2.1），所以它不报 VM。结果是：探针层与平台层
        之间缺一条边，**跨层关联的最后一公里依赖尚未落地的适配层**。
        """
        post(client, fixture("normal"))
        with db_engine.connect() as conn:
            parents = [
                row[0]
                for row in conn.execute(
                    text("select parent_id from resource where kind = 'container'")
                ).all()
            ]
        assert parents and all(p is None for p in parents), parents

    def test_platform_and_probe_namespaces_do_not_resolve_to_each_other(self) -> None:
        """两个命名空间拼出的 ID 不同，因此 ZSvirt 上报的 VM **不会**与探针的 VM 合并。

        这正是需要适配层做映射的原因；断言它，是为了让"为什么跨层关联还差一步"
        在代码里可查，而不是只写在文档里。
        """
        from app.normalize.ids import ResourceRef, probe_resource_id, zsvirt_resource_id

        probe_vm = probe_resource_id(AGENT_ID, ResourceRef("vm", SELF))
        zsvirt_vm = zsvirt_resource_id("vm", "3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44")

        assert probe_vm == f"vm:probe:{AGENT_ID}:self"
        assert zsvirt_vm == "vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"
        assert probe_vm != zsvirt_vm
        # 探针侧上报的 vmId 用的是**平台形态**，说明契约期望两者是同一个对象 ——
        # 但拼装规则不同，因此必须有人在中间做映射
        assert zsvirt_vm == VM_ID, "探针的 vmId 采用平台形态，这正是需要桥接的地方"


class TestEventTypeCoverage:
    """任务 6.3：17 项事件类型逐项归属（A 的 `EVENT_COVERAGE.md` 落地为断言）。

    A 的结论是「无一项被静默放弃」。这条断言把它变成可执行的事实：新增事件类型
    而忘了分配归属时，本用例会失败。
    """

    #: `事件类型 → (归属方, 状态)`。归属方与 A 的确认表逐条对应。
    OWNERSHIP: dict[str, tuple[str, str]] = {
        # 探针已实现
        EventType.CONTAINER_OOM_KILLED.value: ("probe", "implemented"),
        EventType.CONTAINER_RESTART.value: ("probe", "implemented"),
        EventType.PROCESS_CRASH.value: ("probe", "implemented"),
        EventType.PROCESS_IO_WAIT_HIGH.value: ("probe", "implemented"),
        EventType.CONTAINER_NETWORK_UNREACHABLE.value: ("probe", "implemented"),
        # 待 X-08（AI 工作负载日志规范），A 已给目标格式样例
        EventType.INFERENCE_TIMEOUT.value: ("probe", "blocked-x08"),
        EventType.INFERENCE_ERROR.value: ("probe", "blocked-x08"),
        EventType.AGENT_NETWORK_TIMEOUT.value: ("probe", "blocked-x08"),
        EventType.AGENT_TASK_FAILED.value: ("probe", "blocked-x08"),
        # 归 B 的 GPU 渠道（D-028 三层分工 / X-05 / X-07）
        EventType.GPU_MEMORY_EXHAUSTED.value: ("b-gpu-channel", "blocked-x05"),
        EventType.GPU_UTILIZATION_HIGH.value: ("b-gpu-channel", "blocked-x05"),
        EventType.VGPU_QUOTA_EXCEEDED.value: ("b-gpu-channel", "blocked-zsvirt"),
        # 可选场景（D-071 降级，赛题非必需）
        EventType.VM_DISK_IO_SATURATED.value: ("probe", "optional"),
        # B 侧平台自产（source=derived）
        EventType.INGEST_CLOCK_DRIFT_HIGH.value: ("b-platform", "implemented"),
        EventType.INGEST_RESOURCE_UNRESOLVED.value: ("b-platform", "implemented"),
        EventType.ZSVIRT_SYNC_FAILED.value: ("b-platform", "blocked-zsvirt"),
        EventType.GPU_PROVIDER_DEGRADED.value: ("b-platform", "implemented"),
    }

    def test_every_frozen_type_has_an_owner(self) -> None:
        """**核心断言**：17 项每一项都必须有明确归属，不允许"没人管"。"""
        frozen = {m.value for m in EventType}
        assert frozen == set(self.OWNERSHIP), (
            f"事件类型与归属表不一致。只在一侧出现：{sorted(frozen ^ set(self.OWNERSHIP))}"
        )

    def test_ownership_covers_all_four_parties(self) -> None:
        parties = {party for party, _ in self.OWNERSHIP.values()}
        assert parties == {"probe", "b-gpu-channel", "b-platform"}, parties

    def test_probe_implemented_types_all_have_alert_rules(self) -> None:
        """探针**已实现**的类型必须都有告警规则 —— 否则数据进来却没人看。

        这是一条真正的交叉校验：A 说"我已经在采集了"，B 必须回答"我能处置它"。
        """
        from app.alerts.rules import build_default_rule_set

        covered = {
            rule.condition.event_type
            for rule in build_default_rule_set().rules
            if rule.condition.event_type
        }
        implemented = {
            event_type
            for event_type, (party, status) in self.OWNERSHIP.items()
            if party == "probe" and status == "implemented"
        }
        missing = implemented - covered
        assert not missing, (
            f"探针已实现但这些事件没有告警规则：{sorted(missing)}；"
            "A 在采集、B 却不会告警，等于数据白采"
        )

    def test_rule_referenced_types_are_all_frozen(self) -> None:
        """规则里引用的类型必须都是已冻结枚举 —— 防止引用不存在的类型。"""
        from app.alerts.rules import build_default_rule_set
        from app.diagnosis.rules import build_default_rule_set as build_diag_rules

        frozen = {m.value for m in EventType}
        referenced = {
            rule.condition.event_type
            for rule in build_default_rule_set().rules
            if rule.condition.event_type
        }
        for rule in build_diag_rules().rules:
            if rule.evidence_kind.value == "event":
                referenced.add(rule.match_name)

        unknown = {t for t in referenced if t not in frozen and "." in t}
        assert not unknown, f"规则引用了不存在的事件类型：{sorted(unknown)}"

    def test_blocked_types_are_all_attributed_to_a_named_dependency(self) -> None:
        """所有"未实现"的类型都必须挂在**有编号**的外部依赖上。

        这是 A 那句"无一项被静默放弃"的可执行版本：不允许出现
        "状态=未实现但没人知道在等什么"。
        """
        blocked = {
            event_type: status
            for event_type, (_party, status) in self.OWNERSHIP.items()
            if status.startswith("blocked")
        }
        assert blocked, "应当存在被阻塞的类型（否则 X-04/05/07/08 的记录就是多余的）"
        for event_type, status in blocked.items():
            assert status.split("-", 1)[1].startswith(("x0", "x1", "zsvirt")), (
                f"{event_type} 的阻塞原因 {status!r} 没有指向具体的外部依赖编号"
            )

    def test_scenario_one_needs_because_probe_only_reports_symptoms(self) -> None:
        """记录一条设计事实：场景一的根因类型**不在探针职责内**。

        若有人把 `gpu.memory.exhausted` 划给探针，跨层分工（D-028）就被打破了，
        本用例会失败并提醒同步更新 A 的 `EVENT_COVERAGE.md`。
        """
        party, _status = self.OWNERSHIP[EventType.GPU_MEMORY_EXHAUSTED.value]
        assert party == "b-gpu-channel", (
            "`gpu.memory.exhausted` 的归属变了：探针在 VM 内无法观测平台侧显存"
            "（D-028 / DATA_MODEL §4.2.1）"
        )
