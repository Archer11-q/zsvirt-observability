"""A→B 契约测试：拿成员 A 的**真实样例载荷**跑后端全链路（任务 6.1）。

数据来源：`agents/probe/fixtures/`（成员 A 在 `b48aac6` 交付，见其 `README.md`）。
这些载荷由探针**同一套管道**生成，时间戳与 `batchId` 固定、重复运行 byte 级一致，
因此可以直接当作契约基线 —— 这正是"用真实载荷做 fixture"的意义：
自造的 payload 只能测到"我以为的契约"。

对应 A 在 `agents/docs/EVENT_COVERAGE.md` 里写的五条集成测试建议，逐条落实：

| 建议 | 用例 |
|---|---|
| 1 `normal.json` → `accepted.resources=7, events=0`、无 `rejected` | `TestNormalBatch` |
| 2 `container_oom_killed.json` → 3 事件 → 容器 OOM 告警 → 诊断 `CONTAINER_MEMORY_LIMIT` | `TestContainerOom` |
| 3 重复提交 → Q2 幂等 | `TestIdempotency` |
| 4 `agent_network_failure.json` → 场景三规则链 + `agent`/`task` 图边 | `TestNetworkFailure` |
| 5 脱敏与 `sourceId` 规则 | `TestRedactionAndIds` |

**不含**场景一的跨层结论：A 的 `gpu_memory_exhausted.json` 只含 VM 内症状
（`process.io_wait.high`），根因事件 `gpu.memory.exhausted` 由 B 的 GPU 渠道产生
（D-028 三层分工）。跨层组合在 `test_cross_layer.py` 里验证。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

#: 成员 A 的样例载荷目录。**只读**：这些文件由 A 生成，B 不得改动 ——
#: 改了就不再是"对方的真实载荷"，契约测试也就失去意义。
FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "agents/probe/fixtures"

ALL_FIXTURES = (
    "normal",
    "container_oom_killed",
    "gpu_memory_exhausted",
    "agent_network_failure",
)


def load(name: str) -> dict[str, Any]:
    path = FIXTURES / f"{name}.json"
    if not path.exists():  # pragma: no cover - 只有样例被删时触发
        pytest.skip(f"缺少成员 A 的样例载荷：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def client(db_engine: Engine):
    """指向测试库的 TestClient（与 conftest 的等价，此处显式声明以便本文件独立可读）。

    注意：本文件**不使用** conftest 的 `client`，因为它需要同时读回数据库以断言
    资源与图结构；两套夹具混用会让"哪份数据可见"变得难以推理。
    """
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


def post_batch(client: TestClient, payload: dict) -> dict:
    response = client.post("/api/v1/ingest/batch", json=payload)
    assert response.status_code == 200, response.text[:400]
    return response.json()["data"]


def rows(engine: Engine, sql: str) -> list[tuple]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql)).all())


def truncate_all(engine: Engine) -> None:
    """清空全部表（保留表结构）。

    本文件自建 `client` 夹具，因此**没有**继承 conftest 里那个负责事后 TRUNCATE
    的 `client`；少了清理，用例就会在上一个用例提交的数据之上运行 ——
    实测表现为"OOM 样例的告警泄漏到网络样例"。
    """
    from app.models import Base

    with engine.begin() as conn:
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        if names:
            conn.execute(text(f"TRUNCATE {names} CASCADE"))


@pytest.fixture(autouse=True)
def _clean_database(db_engine: Engine) -> None:
    """**每个**用例开始前清空数据。

    用 autouse 而不是在每个用例里手写一行：手写的那行很容易在新增用例时被忘掉，
    而失败表现是"读到别的用例的数据"，看起来像产品缺陷而不是测试缺陷。
    """
    truncate_all(db_engine)


class TestFixtureIntegrity:
    """先证明样例本身是自洽的，再谈后端如何接它。

    若样例内部就不自洽（事件引用未声明的资源、`sourceId` 含冒号），
    后端拒绝它反而是正确行为 —— 那类失败不该被算成后端缺陷。
    """

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_fixture_exists_and_parses(self, name: str) -> None:
        data = load(name)
        for field in ("agentId", "vmId", "agentVersion", "batchId", "sentAt"):
            assert field in data, f"{name} 缺少 {field}"

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_event_refs_point_at_declared_resources(self, name: str) -> None:
        """`resourceRef` 必须指向本批已声明的资源。

        A 的生成器已内置该校验；这里再断言一次，因为一旦漂移，
        后端会静默建占位节点，而占位节点会让影响范围失真。
        """
        data = load(name)
        declared = {(r["kind"], r["sourceId"]) for r in data["resources"]}
        for event in data["events"]:
            ref = event["resourceRef"]
            assert (ref["kind"], ref["sourceId"]) in declared, (
                f"{name}: 事件 {event['type']} 引用了未声明的资源 {ref}"
            )

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_source_ids_have_no_colon(self, name: str) -> None:
        """`sourceId` 含冒号会拼出五段 ID，后端应拒绝（A 已对齐，这里守住）。"""
        data = load(name)
        for resource in data["resources"]:
            assert ":" not in resource["sourceId"], resource
            assert ":" not in (resource.get("parentSourceId") or ""), resource

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_agent_id_has_no_colon(self, name: str) -> None:
        assert ":" not in load(name)["agentId"]

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_vm_id_has_the_global_shape(self, name: str) -> None:
        parts = load(name)["vmId"].split(":")
        assert parts[0] == "vm" and parts[1] == "zsvirt" and len(parts) >= 3


class TestNormalBatch:
    """A 的建议 1：`normal.json` → 7 资源 / 0 事件 / 无 rejected。"""

    def test_accepts_all_resources_and_no_events(self, client: TestClient) -> None:
        data = post_batch(client, load("normal"))
        assert data["accepted"]["resources"] == 7
        assert data["accepted"]["events"] == 0
        assert data["rejected"] == []
        assert data["duplicate"] is False

    def test_empty_event_list_is_not_an_error(self, client: TestClient) -> None:
        """空事件批必须正常返回，而不是 422 —— 探针在无事发生时也会心跳上报。"""
        data = post_batch(client, load("normal"))
        assert data["alerts"]["eventsEvaluated"] == 0
        assert data["alerts"]["created"] == 0

    def test_no_alerts_from_a_healthy_batch(self, client: TestClient) -> None:
        post_batch(client, load("normal"))
        assert client.get("/api/v1/alerts").json()["data"]["items"] == []

    def test_all_five_probe_kinds_are_accepted(self, client: TestClient, db_engine: Engine) -> None:
        post_batch(client, load("normal"))
        kinds = dict(rows(db_engine, "select kind, count(*) from resource group by kind"))
        assert set(kinds) <= {"container", "process", "ai_service", "agent", "task"}
        assert kinds["container"] == 2
        assert kinds["process"] == 3
        assert kinds["ai_service"] == 2

    def test_hierarchy_comes_from_parent_source_id(
        self, client: TestClient, db_engine: Engine
    ) -> None:
        """**回归测试**：`process` 的父是 `container`，不是 `vm`。

        此前 `_infer_parent_kind("process")` 猜成 `vm`，拼出不存在的父 id，
        而 `upsert_resource` 在同一条 INSERT 里写 `parent_id` —— 外键违约让
        **整批**上报 503。A 的样例一入库就暴露了这个缺陷。

        现在父类型是从**本批声明的资源**里查出来的，不是猜的。
        """
        post_batch(client, load("normal"))
        pairs = dict(
            rows(
                db_engine,
                "select child_id, parent_id from resource_edge",
            )
        )
        process_parents = {p for c, p in pairs.items() if c.startswith("process:")}
        assert process_parents, "进程没有父边"
        assert all(p.startswith("container:") for p in process_parents), (
            f"进程的父应当是容器，实际：{process_parents}"
        )

    def test_no_dangling_parent_id(self, client: TestClient, db_engine: Engine) -> None:
        """`parent_id` 不得指向不存在的资源（外键已强制，这里断言它没被绕过）。"""
        post_batch(client, load("normal"))
        dangling = rows(
            db_engine,
            "select r.id, r.parent_id from resource r "
            "left join resource p on p.id = r.parent_id "
            "where r.parent_id is not null and p.id is null",
        )
        assert dangling == []


class TestContainerOom:
    """A 的建议 2：3 个事件 → 容器 OOM 告警 → 诊断 `CONTAINER_MEMORY_LIMIT`。"""

    def test_accepts_all_three_events(self, client: TestClient) -> None:
        data = post_batch(client, load("container_oom_killed"))
        assert data["accepted"]["resources"] == 5
        assert data["accepted"]["events"] == 3
        assert data["rejected"] == []

    def test_produces_the_expected_alerts(self, client: TestClient) -> None:
        post_batch(client, load("container_oom_killed"))
        alerts = client.get("/api/v1/alerts", params={"limit": 50}).json()["data"]["items"]
        by_rule = {a["ruleId"]: a for a in alerts}

        assert set(by_rule) == {"R-CTR-OOM-010", "R-CTR-RESTART-011", "R-PROC-CRASH-012"}
        assert by_rule["R-CTR-OOM-010"]["severity"] == "critical"
        assert by_rule["R-CTR-RESTART-011"]["severity"] == "warning"
        assert by_rule["R-PROC-CRASH-012"]["severity"] == "error"

    def test_auto_diagnosis_concludes_container_memory_limit(self, client: TestClient) -> None:
        """**场景二的端到端结论**，用 A 的真实载荷而非自造 payload。"""
        post_batch(client, load("container_oom_killed"))

        diagnoses = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        assert len(diagnoses) == 1, [d["rootCause"] for d in diagnoses]
        diag = diagnoses[0]

        assert diag["rootCause"] == "CONTAINER_MEMORY_LIMIT"
        assert diag["confidence"] >= 0.30
        assert diag["ruleSetVersion"], "结论必须带规则集版本"
        assert diag["evidence"], "有根因就必须有证据"
        assert "INCREASE_CONTAINER_MEMORY" in {r["code"] for r in diag["recommendation"]}

    def test_alert_links_back_to_the_diagnosis(self, client: TestClient) -> None:
        post_batch(client, load("container_oom_killed"))
        oom = next(
            a
            for a in client.get("/api/v1/alerts").json()["data"]["items"]
            if a["ruleId"] == "R-CTR-OOM-010"
        )
        assert oom["diagnosisId"], "严重告警应被自动诊断并回写"

        diag = client.get(f"/api/v1/diagnosis/{oom['diagnosisId']}").json()["data"]
        assert diag["rootCause"] == "CONTAINER_MEMORY_LIMIT"

    def test_evidence_traces_back_to_real_events(self, client: TestClient) -> None:
        post_batch(client, load("container_oom_killed"))
        for alert in client.get("/api/v1/alerts").json()["data"]["items"]:
            assert alert["evidenceEventIds"], "红线：告警必须有证据"
            for event_id in alert["evidenceEventIds"]:
                assert client.get(f"/api/v1/events/{event_id}").status_code == 200


class TestGpuProbeSymptomOnly:
    """A 的 `gpu_memory_exhausted.json` 只含 VM 内症状。

    按 D-028 的三层分工，`gpu.memory.exhausted` 由 B 的 GPU 渠道产生，
    探针只给 `process.io_wait.high`。因此这里断言的是**诚实的行为**：
    单靠探针症状得不出 GPU 根因（也不该硬套），跨层结论在组合测试里验证。
    """

    def test_accepts_the_batch(self, client: TestClient) -> None:
        data = post_batch(client, load("gpu_memory_exhausted"))
        assert data["accepted"]["resources"] == 4
        assert data["accepted"]["events"] == 2
        assert data["rejected"] == []

    def test_probe_symptom_raises_a_warning_alert(self, client: TestClient) -> None:
        """`process.io_wait.high` 现在有告警规则（`R-PROC-IOWAIT-013`）。

        **这条断言的变化本身就是一次发现**：任务 6.3 做事件覆盖对账时发现
        A 的收集器**已经在采集**这个类型，而 B 的规则集里没有任何规则引用它 ——
        数据进来了却没人看。补上规则后，原先"断言没有告警"的用例如期失败，
        提示更新，于是它变成现在这条。

        保留它的价值：若规则被删掉，这里会失败，而不是让"数据白采"重新悄悄发生。
        """
        data = post_batch(client, load("gpu_memory_exhausted"))
        # 样例里两条事件落在**两个不同的进程**上，而聚合键是
        # `{ruleId}:{resourceId}`，因此正确地产生**两条**告警而不是一条。
        assert data["alerts"]["created"] == 2
        # 自动诊断的计数在 `autoDiagnosis` 子对象里（告警引擎摘要只报自己的计数）
        auto = data["alerts"]["autoDiagnosis"]
        assert auto["skippedBelowSeverity"] == 2, "warning 级不自动诊断"
        assert auto["linked"] == {}

        alerts = client.get("/api/v1/alerts", params={"limit": 50}).json()["data"]["items"]
        assert {a["ruleId"] for a in alerts} == {"R-PROC-IOWAIT-013"}
        assert len({a["resourceId"] for a in alerts}) == 2, "两个进程各自成案"
        for alert in alerts:
            # warning 级：不自动诊断（避免灌一堆 UNKNOWN），但列表里看得到
            assert alert["severity"] == "warning"
            assert alert["diagnosisId"] is None
            assert alert["evidenceEventIds"], "红线：告警必须有证据"

    def test_symptom_alone_does_not_invent_a_gpu_cause(self, client: TestClient) -> None:
        """不得凭 VM 内 I/O 等待就断言 GPU 显存耗尽 —— 那不叫关联，叫猜。"""
        post_batch(client, load("gpu_memory_exhausted"))
        diagnoses = client.get("/api/v1/diagnoses").json()["data"]["items"]
        causes = {d["rootCause"] for d in diagnoses}
        assert "GPU_MEMORY_EXHAUSTED" not in causes, (
            "仅凭探针症状得出了 GPU 根因，说明证据不足以支撑该结论"
        )


class TestNetworkFailure:
    """A 的建议 4：场景三规则链 + `agent`/`task` 资源与图边。"""

    def test_accepts_all_five_events(self, client: TestClient) -> None:
        data = post_batch(client, load("agent_network_failure"))
        assert data["accepted"]["resources"] == 4
        assert data["accepted"]["events"] == 5
        assert data["rejected"] == []
        assert data["acceptedUnknownTypes"] == 0, "全部事件类型都应是已冻结枚举"

    def test_agent_and_task_resources_are_accepted(
        self, client: TestClient, db_engine: Engine
    ) -> None:
        post_batch(client, load("agent_network_failure"))
        kinds = dict(rows(db_engine, "select kind, count(*) from resource group by kind"))
        assert kinds.get("agent") == 1
        assert kinds.get("task") == 1

    def test_agent_task_edge_exists(self, client: TestClient, db_engine: Engine) -> None:
        """`agent → task` 的父子边必须落到邻接表（否则诊断看不到 task）。"""
        post_batch(client, load("agent_network_failure"))
        pairs = rows(db_engine, "select parent_id, child_id from resource_edge")
        assert any(p.startswith("agent:") and c.startswith("task:") for p, c in pairs), (
            f"缺少 agent→task 边：{pairs}"
        )

    def test_produces_the_scenario_three_alert_chain(self, client: TestClient) -> None:
        post_batch(client, load("agent_network_failure"))
        alerts = client.get("/api/v1/alerts", params={"limit": 50}).json()["data"]["items"]
        rules = {a["ruleId"] for a in alerts}
        assert rules == {
            "R-NET-UNREACH-023",
            "R-AGENT-NET-024",
            "R-AGENT-TASK-022",
            "R-AIS-TIMEOUT-020",
            "R-AIS-ERROR-021",
        }, rules

    def test_diagnosis_concludes_network_unreachable(self, client: TestClient) -> None:
        """场景三的端到端结论，同样用 A 的真实载荷。"""
        post_batch(client, load("agent_network_failure"))
        diagnoses = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        assert diagnoses, "错误级告警应触发自动诊断"
        causes = {d["rootCause"] for d in diagnoses}
        assert "NETWORK_UNREACHABLE" in causes, causes

    def test_diagnosis_reaches_beyond_the_anchor(self, client: TestClient) -> None:
        """影响范围必须跨层：证据落在容器/智能体，结论要覆盖到 task。

        注意断言的是**网络结论**而不是 `items[0]`：这个样例会产出两条诊断
        （网络链 0.85、`ai_service` 链因证据不足为 UNKNOWN），而 UNKNOWN 的三集
        本来就该是空的 —— 它没有证据可界定范围。挑错对象会让断言看起来像
        "影响范围没算出来"。
        """
        post_batch(client, load("agent_network_failure"))
        items = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        diag = next(d for d in items if d["rootCause"] == "NETWORK_UNREACHABLE")

        affected = set(diag["affectedResources"])
        assert any(a.startswith("agent:") for a in affected), affected
        assert any(a.startswith("task:") for a in affected), affected
        assert diag["onChain"] or diag["potentiallyAffected"] or affected, "三集全空"

    def test_ai_service_chain_stays_unknown_with_weak_evidence(self, client: TestClient) -> None:
        """`inference.timeout` / `inference.error` 的证据不足以定根因 → 诚实返回 UNKNOWN。

        这是置信下限（0.30）在起作用：有症状、有证据，但没有任何一条规则能给出
        足够强的解释。此时返回一个"看起来像"的根因才是最坏的失败方式 ——
        运维会照着它去改配置。断言 UNKNOWN 就是断言这条红线还在。
        """
        post_batch(client, load("agent_network_failure"))
        items = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]

        unknown = [d for d in items if d["rootCause"] == "UNKNOWN"]
        assert unknown, "证据不足时应有一条 UNKNOWN 结论"
        for diag in unknown:
            assert diag["confidence"] < 0.30
            assert [r["code"] for r in diag["recommendation"]] == ["COLLECT_MORE_EVIDENCE"]

    def test_the_two_chains_are_separate_incidents(self, client: TestClient) -> None:
        """网络链与 `ai_service` 链是两个事故：资源不同，结论也各自独立。

        这个样例特别有价值 —— A 把它作为**一个**批次上报，而 B 按资源判定为两起
        事故。若将来有人把聚合判据放宽到"同批即同事故"，这条会失败。
        """
        post_batch(client, load("agent_network_failure"))
        items = client.get("/api/v1/diagnoses", params={"limit": 50}).json()["data"]["items"]
        assert len(items) == 2, [(d["rootCause"], d["trigger"].get("clusterSize")) for d in items]

        sizes = sorted(d["trigger"].get("clusterSize", 0) for d in items)
        assert sizes == [2, 3], "两个事故分别覆盖 2 条与 3 条告警"

        for diag in items:
            assert diag["trigger"].get("alertIds"), "聚合痕迹必须留在结论里"


class TestIdempotency:
    """A 的建议 5：同一批次重复提交 → Q2 幂等。"""

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_replay_is_flagged_and_consistent(self, client: TestClient, name: str) -> None:
        payload = load(name)
        first = post_batch(client, payload)
        again = post_batch(client, payload)

        assert again["duplicate"] is True, "重复批次必须被识别（A 据此维护已确认缓存）"
        assert again["accepted"] == first["accepted"]
        assert again["resources"] == first["resources"], "逐条结果必须与首次一致"

    def test_replay_does_not_duplicate_events_or_alerts(
        self, client: TestClient, db_engine: Engine
    ) -> None:
        payload = load("container_oom_killed")
        post_batch(client, payload)
        before = (
            rows(db_engine, "select count(*) from event")[0][0],
            rows(db_engine, "select count(*) from alert")[0][0],
        )
        post_batch(client, payload)
        after = (
            rows(db_engine, "select count(*) from event")[0][0],
            rows(db_engine, "select count(*) from alert")[0][0],
        )
        assert before == after, "重放不得再写事件或告警"

    def test_replay_does_not_inflate_alert_count(
        self, client: TestClient, db_engine: Engine
    ) -> None:
        payload = load("container_oom_killed")
        post_batch(client, payload)
        post_batch(client, payload)
        counts = rows(db_engine, "select distinct count from alert")
        assert counts == [(1,)], f"重放让 count 灌水：{counts}"


class TestRedactionAndIds:
    """A 的建议 5（续）：脱敏与 `sourceId` 规则在真实载荷上生效。"""

    def test_cmdline_is_masked_but_structure_kept(
        self, client: TestClient, db_engine: Engine
    ) -> None:
        """A 的样例里 `--api-key sk-demo` 必须被掩码，但命令结构要保留。

        保留结构是有意的：诊断要能看出"这是 vllm 起的服务"，
        而密钥本身对诊断没有价值、泄漏代价却很高。
        """
        post_batch(client, load("normal"))
        rows_ = rows(
            db_engine,
            "select attributes from resource where kind = 'process'",
        )
        dumped = json.dumps([r[0] for r in rows_], ensure_ascii=False)
        assert "sk-demo" not in dumped, "cmdline 里的密钥未被掩码"
        assert "vllm.entrypoints" in dumped, "掩码不应破坏命令结构"

    #: 敏感**键名**（不是任意文本里出现这些词）。与 `app.normalize.sensitive` 的
    #: 规则一致 —— 脱敏针对的是 key/value，不是自由文本。
    SENSITIVE_KEYS = ("api_key", "apikey", "password", "passwd", "secret", "token")

    def _collect_keys(self, value, prefix: str = "") -> list[str]:
        """递归收集 JSON 结构里出现的所有键名。"""
        out: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                out.append(str(key))
                out.extend(self._collect_keys(item, f"{prefix}.{key}"))
        elif isinstance(value, list):
            for item in value:
                out.extend(self._collect_keys(item, prefix))
        return out

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_no_sensitive_key_survives_ingest(
        self, client: TestClient, db_engine: Engine, name: str
    ) -> None:
        """入库后**不得留下敏感键**。

        只查键名，不查自由文本：`container_oom_killed.json` 的 kernel log 里出现
        "password" 这类词是正常的诊断信息，把它一起掩掉会损害可诊断性
        （`SENSITIVE_DATA.md` 的分工就是"掩码键值、保留结构"）。

        另外单独断言**密钥值**（`sk-demo`）不残留 —— 那才是真正会泄漏的东西。
        """
        post_batch(client, load(name))

        keys: list[str] = []
        values: list[str] = []
        for table, column in (
            ("resource", "attributes"),
            ("resource", "labels"),
            ("event", "raw"),
            ("event", "metrics"),
        ):
            for (payload,) in rows(db_engine, f"select {column} from {table}"):
                keys.extend(self._collect_keys(payload))
                values.append(json.dumps(payload, ensure_ascii=False))

        lowered = [k.lower() for k in keys]
        for sensitive in self.SENSITIVE_KEYS:
            assert not any(
                k == sensitive or k.endswith(f"_{sensitive}") or k.startswith(f"{sensitive}_")
                for k in lowered
            ), f"{name}: 入库后仍能见到敏感键 {sensitive}（现有键：{sorted(set(lowered))}）"

        joined = " ".join(values)
        assert "sk-demo" not in joined, f"{name}: 密钥值泄漏到库里"

    def test_resource_ids_are_four_segment_probe_ids(
        self, client: TestClient, db_engine: Engine
    ) -> None:
        for name in ALL_FIXTURES:
            with db_engine.begin() as conn:
                for table in ("event", "resource", "alert", "diagnosis", "ingest_batch"):
                    conn.execute(text(f'TRUNCATE "{table}" CASCADE'))
            post_batch(client, load(name))
            ids = [r[0] for r in rows(db_engine, "select id from resource")]
            assert ids
            for resource_id in ids:
                parts = resource_id.split(":")
                assert len(parts) == 4, f"{name}: 探针资源 ID 应为四段：{resource_id}"
                assert parts[1] == "probe"
                assert parts[2] == load(name)["agentId"]

    def test_no_placeholder_resources_created(self, client: TestClient, db_engine: Engine) -> None:
        """样例的 `resourceRef` 自洽，因此不应产生任何占位节点。"""
        for name in ALL_FIXTURES:
            with db_engine.begin() as conn:
                for table in ("event", "resource", "alert", "diagnosis", "ingest_batch"):
                    conn.execute(text(f'TRUNCATE "{table}" CASCADE'))
            post_batch(client, load(name))
            placeholders = rows(
                db_engine, "select count(*) from resource where is_placeholder or kind='unresolved'"
            )[0][0]
            assert placeholders == 0, f"{name}: 产生了 {placeholders} 个占位节点"


class TestGraphIsIncompleteByDesign:
    """记录一个**契约层面的已知事实**，而不是把它当成缺陷。

    探针只上报它自己权威的层（container / process / ai_service / agent / task）。
    `host` / `gpu` / `vgpu` / `vm` 由 ZSvirt 资产 API 侧权威（`DATA_MODEL.md` §4.2.1），
    因此样例里没有它们，容器也没有父。

    后果：直接灌入 A 的样例后资源图是**孤立的分支**，拓扑看不到 VM 及以上。
    这是设计如此，不是 bug —— 但必须被断言下来，否则将来有人会误以为
    "探针漏报了 VM"而去改 A 的样例。
    """

    def test_container_has_no_parent_when_vm_is_not_reported(
        self, client: TestClient, db_engine: Engine
    ) -> None:
        post_batch(client, load("normal"))
        parents = rows(db_engine, "select parent_id from resource where kind = 'container'")
        assert parents, "应有容器资源"
        assert all(p[0] is None for p in parents), (
            "容器不应有父 —— 样例里没有上报 VM，而探针不拥有 VM 的权威"
        )

    def test_topology_returns_only_probe_owned_kinds(self, client: TestClient) -> None:
        post_batch(client, load("normal"))
        nodes = client.get("/api/v1/topology", params={"depth": 8}).json()["data"]["nodes"]
        kinds = {n["kind"] for n in nodes}
        assert kinds == {"container", "process", "ai_service"}
        for infra in ("host", "gpu", "vgpu", "vm"):
            assert infra not in kinds, f"探针样例不该产出 {infra} 层资源"

    def test_workload_view_is_empty_without_infra_or_service_chain(
        self, client: TestClient
    ) -> None:
        """`ai_service` 已上报，因此工作负载视图应有条目 —— 但计数为 0（无事件）。"""
        post_batch(client, load("normal"))
        data = client.get(
            "/api/v1/workloads",
            params={"since": "2026-09-17T00:00:00+00:00", "to": "2026-09-18T00:00:00+00:00"},
        ).json()["data"]
        assert len(data["items"]) == 2, [i["id"] for i in data["items"]]
        assert all(item["eventCount"] == 0 for item in data["items"])
