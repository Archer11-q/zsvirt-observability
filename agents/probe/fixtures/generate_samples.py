"""稳定样例载荷生成器（成员 A → 成员 B 契约测试用）。

**为什么需要这个脚本**：B 的任务 5/6（A→B 契约测试与集成测试补强）在等
「稳定样例数据」（`docs/DEVELOPMENT_PLAN.md` §5.4.1）。稳定 = 字段、时间戳、
ID 全部确定，测试可复现；真实 = 由探针的**同一套管道**生成
（`model.Resource` / `model.Event` → `to_payload()` → `reporter.build_batch`），
与探针运行时上报的格式一字不差。

运行（在仓库根，零第三方依赖）：

    python agents/probe/fixtures/generate_samples.py

产出 4 个场景文件（与脚本同目录）：

| 文件 | 场景（对齐 D-070） | 覆盖 |
|---|---|---|
| `normal.json` | 正常态 | 全部 5 种资源 kind；空 events（测 B 的空批处理） |
| `container_oom_killed.json` | 场景二 · 容器/进程异常 | container.oom_killed / process.crash / container.restart |
| `gpu_memory_exhausted.json` | 场景一 · GPU 瓶颈（探针访客层 + 症状） | `gpu` 资源（访客层 memUsedBytes≈95% 自身超配）+ `process.io_wait.high`；根因事件 `gpu.*` 归 B（D-028） |
| `agent_network_failure.json` | 场景三 · Agent/网络异常 | inference.* / agent.* / container.network.unreachable（X-08 目标格式） |

所有 batchId / agent 与 task 的 sourceId 由固定随机位的 ULID 构成
（时间位取自固定基准时间），时间戳固定在 2026-09-17T08:00:00Z 附近 ——
重复运行输出 byte 级一致，可作为测试 fixture 提交进仓库。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 复用探针真实管道（以 probe 包方式导入，fixtures/ 上两级即 agents/）
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))

from probe.idgen import _encode  # noqa: E402  ULID 时间位编码（Crockford Base32）
from probe.model import Event, Resource  # noqa: E402
from probe.reporter import build_batch  # noqa: E402

# ---------------------------------------------------------------- 固定常量

VM_ID = "vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44"
AGENT_ID = "probe-3f2a9c10"  # probe-<vm-uuid 前 8 位>（§3.3.1）

T0 = datetime(2026, 9, 17, 8, 0, 0, tzinfo=timezone.utc)
T0_MS = int(T0.timestamp() * 1000)

#: F-01 冻结的事件类型枚举（17 项，docs/CONTRACT_FREEZE.md §F-01）
EVENT_TYPES = {
    "gpu.memory.exhausted", "gpu.utilization.high", "vgpu.quota.exceeded",
    "vm.disk.io_saturated", "container.oom_killed", "container.restart",
    "process.crash", "process.io_wait.high", "inference.timeout",
    "inference.error", "agent.task.failed", "agent.network.timeout",
    "container.network.unreachable", "ingest.clock_drift.high",
    "ingest.resource.unresolved", "zsvirt.sync.failed", "gpu.provider.degraded",
}
SEVERITIES = {"info", "warning", "error", "critical"}
PROBE_KINDS = {"container", "process", "ai_service", "agent", "task", "gpu"}
ULID_RE = re.compile(r"^[0-9ABCDEFGHJKMNPQRSTVWXYZ]{26}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def ts(offset_ms: int = 0) -> str:
    """固定基准时间 + 偏移（毫秒）→ ISO 8601 UTC 毫秒（带 Z）。"""
    t = T0 + timedelta(milliseconds=offset_ms)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def fixed_ulid(rand16: str) -> str:
    """固定随机位的 ULID：时间位取 T0（真实时间线），随机位固定 → 输出确定。"""
    ulid = _encode(T0_MS & ((1 << 48) - 1), 10) + rand16
    assert len(ulid) == 26 and ULID_RE.match(ulid), f"bad fixed ulid: {ulid}"
    return ulid


def res(**kw) -> Resource:
    """构造资源（探针真实 dataclass，由 build_batch 统一转 payload）。"""
    return Resource(
        kind=kw["kind"],
        source_id=kw["source_id"],
        name=kw["name"],
        parent_source_id=kw.get("parent_source_id"),
        status=kw.get("status", "running"),
        attributes=kw.get("attributes", {}),
        first_seen_at=kw.get("first_seen_at", ts(0)),
        last_seen_at=kw.get("last_seen_at", ts(0)),
    )


def evt(etype: str, severity: str, kind: str, sid: str, message: str,
        at_ms: int, metrics: dict | None = None, raw: dict | None = None) -> Event:
    """构造事件（探针真实 dataclass，由 build_batch 统一转 payload）。"""
    return Event(
        type=etype,
        severity=severity,
        message=message,
        resource_kind=kind,
        resource_source_id=sid,
        occurred_at=ts(at_ms),
        metrics=metrics or {},
        raw=raw or {},
    )


# ---------------------------------------------------------------- 场景

def build_normal() -> dict:
    """正常态：全量资源快照、无异常事件（events=[]，测 B 的空批处理）。"""
    resources = [
        res(kind="container", source_id="8f1a2b3c4d5e", name="vllm-0",
            attributes={"image": "vllm/vllm-openai:latest", "runtime": "docker"}),
        res(kind="container", source_id="a1b2c3d4e5f6", name="qwen-agent-0",
            attributes={"image": "qwen-agent:2.1", "runtime": "docker"}),
        res(kind="process", source_id="1847263.1827", name="vllm",
            parent_source_id="8f1a2b3c4d5e",
            attributes={"cmdline": ["python", "-m", "vllm.entrypoints.openai.api_server",
                                    "--model", "qwen2.5-7b", "--port", "8000",
                                    "--api-key", "sk-demo"]}),
        res(kind="process", source_id="1847299.1904", name="python",
            parent_source_id="a1b2c3d4e5f6",
            attributes={"cmdline": ["python", "agent_server.py", "--port", "8080"]}),
        res(kind="process", source_id="1847301.1950", name="vllm-worker",
            parent_source_id="8f1a2b3c4d5e",
            attributes={"cmdline": ["python", "-m", "vllm.worker", "--port", "8000"]}),
        res(kind="ai_service", source_id="vllm.8000", name="vllm",
            parent_source_id="8f1a2b3c4d5e",
            attributes={"framework": "vllm", "endpoint": "http://127.0.0.1:8000", "pid": 1827}),
        res(kind="ai_service", source_id="qwen-agent.8080", name="qwen-agent",
            parent_source_id="a1b2c3d4e5f6",
            attributes={"framework": "qwen-agent", "endpoint": "http://127.0.0.1:8080", "pid": 1904}),
    ]
    _, payload = build_batch(AGENT_ID, VM_ID, resources, [],
                             batch_id=fixed_ulid("7K2X8R4T6W0N5P3C"),
                             sent_at=ts(0))
    return payload


def build_container_oom() -> dict:
    """场景二 · 容器 OOMKilled（赛题点名）：oom_killed → crash(137) → restart。"""
    crashed_pid = "1847309.2100"   # 被 OOM 杀死的进程（已退出）
    reborn_pid = "1847312.2117"    # 容器重启后的新进程（pid 复用 → sourceId 变化）
    resources = [
        res(kind="container", source_id="b7c8d9e0f1a2", name="web-0",
            status="running",
            attributes={"image": "vllm/vllm-openai:latest", "runtime": "docker",
                        "restart_count": 1, "oom_killed": True}),
        res(kind="container", source_id="c9d0e1f2a3b4", name="redis-0",
            attributes={"image": "redis:7", "runtime": "docker", "restart_count": 0}),
        res(kind="process", source_id=crashed_pid, name="python",
            parent_source_id="b7c8d9e0f1a2", status="stopped",
            attributes={"cmdline": ["python", "serve.py", "--port", "8000",
                                    "--password", "s3cret"]}),
        res(kind="process", source_id=reborn_pid, name="python",
            parent_source_id="b7c8d9e0f1a2",
            attributes={"cmdline": ["python", "serve.py", "--port", "8000",
                                    "--password", "s3cret"]}),
        res(kind="ai_service", source_id="vllm.8000", name="vllm",
            parent_source_id="b7c8d9e0f1a2",
            attributes={"framework": "vllm", "endpoint": "http://127.0.0.1:8000"}),
    ]
    events = [
        evt("container.oom_killed", "critical", "container", "b7c8d9e0f1a2",
            "容器 web-0 被内核 OOM Killer 终止", 3_000,
            metrics={"memory_limit_mb": 2048, "memory_usage_mb": 2043, "oom_score": 987},
            raw={"kernel_log": "Out of memory: Killed process 2100 (python)"}),
        evt("process.crash", "error", "process", crashed_pid,
            "进程被信号 SIGKILL 终止（137 = 128 + 9，OOM 特征）", 3_200,
            metrics={"exit_code": 137, "signal": "SIGKILL"}),
        evt("container.restart", "warning", "container", "b7c8d9e0f1a2",
            "容器 web-0 发生一次重启（docker restart 计数 0 → 1）", 5_000,
            metrics={"restart_count": 1}),
    ]
    _, payload = build_batch(AGENT_ID, VM_ID, resources, events,
                             batch_id=fixed_ulid("9M3Y1V6B2H8D4S0Q"),
                             sent_at=ts(6_000))
    return payload


def build_gpu_exhausted() -> dict:
    """场景一 · GPU 显存耗尽（探针症状侧 + 访客层 GPU 数据）。

    根因事件 `gpu.memory.exhausted` 由 B 的 zsvirt-adapter 产生（D-028，
    GPU 数据三层分工）。探针提供两样东西：
    1. **访客层 GPU 数据**（`gpu` kind，origin=probe）：nvidia-smi 可见的
       `memUsedBytes`/`processes[]`，用于「自身超配 vs 邻居干扰」交叉验证。
       本例 `memUsedBytes` ≈ 95% → 指向**自身超配**（DATA_MODEL §2.2）。
    2. VM 内症状（`process.io_wait.high`）。
    供 B 做跨层关联测试：GPU 根因事件 × 探针访客层占用 × 症状事件。
    """
    resources = [
        res(kind="gpu", source_id="GPU-3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44",
            name="Tesla V100-SXM2-32GB",
            attributes={
                "uuid": "GPU-3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44",
                "model": "Tesla V100-SXM2-32GB",
                "pciAddress": "00000000:00:08.0",
                "memTotalBytes": 34359738368,   # 32 GiB
                "memUsedBytes": 32641751450,    # ≈ 95%（自身超配）
                "processes": [
                    {"pid": 1827, "usedMemBytes": 30064771072},  # vllm 主进程 28 GiB
                    {"pid": 1950, "usedMemBytes": 2576980378},   # worker 2.4 GiB
                ],
            }),
        res(kind="container", source_id="8f1a2b3c4d5e", name="vllm-0",
            attributes={"image": "vllm/vllm-openai:latest", "runtime": "docker"}),
        res(kind="process", source_id="1847263.1827", name="vllm",
            parent_source_id="8f1a2b3c4d5e",
            attributes={"cmdline": ["python", "-m", "vllm.entrypoints.openai.api_server",
                                    "--model", "qwen2.5-7b", "--port", "8000"]}),
        res(kind="process", source_id="1847301.1950", name="vllm-worker",
            parent_source_id="8f1a2b3c4d5e",
            attributes={"cmdline": ["python", "-m", "vllm.worker", "--port", "8000"]}),
        res(kind="ai_service", source_id="vllm.8000", name="vllm",
            parent_source_id="8f1a2b3c4d5e",
            attributes={"framework": "vllm", "endpoint": "http://127.0.0.1:8000", "pid": 1827}),
    ]
    events = [
        evt("process.io_wait.high", "warning", "process", "1847301.1950",
            "vllm-worker I/O 等待持续偏高（可能为显存-内存换页放大）", 2_000,
            metrics={"io_wait_pct": 47.3, "threshold_pct": 30, "window_sec": 60}),
        evt("process.io_wait.high", "warning", "process", "1847263.1827",
            "vllm 主进程 I/O 等待升高", 8_000,
            metrics={"io_wait_pct": 35.1, "threshold_pct": 30, "window_sec": 60}),
    ]
    _, payload = build_batch(AGENT_ID, VM_ID, resources, events,
                             batch_id=fixed_ulid("5Q2N8W4K7T1X6R3M"),
                             sent_at=ts(9_000))
    return payload


def build_agent_network_failure() -> dict:
    """场景三 · Agent / 网络异常（X-08 目标格式）。

    `inference.*` / `agent.*` / `container.network.unreachable` 的**真实采集**
    依赖 AI 工作负载日志规范（X-08，未定）。本样例给出冻结枚举下这些事件的
    **目标格式**（字段与 severity 遵循 F-01），供 B 的契约测试与诊断规则
    （场景三根因候选 `agent.network.timeout`）先行开发；X-08 落地后由
    真实日志采集产出同格式数据。
    """
    agent_ulid = fixed_ulid("A6K3M9Q2W5T8R1X4")
    task_ulid = fixed_ulid("B8N4D7H2S6V0P5K9")
    resources = [
        res(kind="container", source_id="a1b2c3d4e5f6", name="qwen-agent-0",
            attributes={"image": "qwen-agent:2.1", "runtime": "docker"}),
        res(kind="ai_service", source_id="vllm.8000", name="vllm",
            parent_source_id="8f1a2b3c4d5e",
            attributes={"framework": "vllm", "endpoint": "http://127.0.0.1:8000"}),
        res(kind="agent", source_id=agent_ulid, name="qwen-agent",
            parent_source_id="a1b2c3d4e5f6",
            attributes={"framework": "qwen-agent", "model": "qwen2.5-7b"}),
        res(kind="task", source_id=task_ulid, name="translate-doc-42",
            parent_source_id=agent_ulid,
            attributes={"task_type": "tool_call", "retries": 3}),
    ]
    events = [
        evt("container.network.unreachable", "error", "container", "a1b2c3d4e5f6",
            "容器 qwen-agent-0 到推理服务的网络不可达", 1_000,
            metrics={"target": "10.0.3.15:8000", "attempts": 3, "timeout_ms": 5000}),
        evt("inference.timeout", "error", "ai_service", "vllm.8000",
            "推理请求超过 30s 未返回", 4_000,
            metrics={"latency_ms": 30120, "timeout_ms": 30000, "model": "qwen2.5-7b"}),
        evt("inference.error", "error", "ai_service", "vllm.8000",
            "推理服务返回 503（upstream overloaded）", 9_000,
            metrics={"status_code": 503, "model": "qwen2.5-7b"}),
        evt("agent.network.timeout", "error", "agent", agent_ulid,
            "Agent 调用推理服务超时", 12_000,
            metrics={"target": "vllm.8000", "timeout_ms": 30000}),
        evt("agent.task.failed", "error", "task", task_ulid,
            "任务 translate-doc-42 重试 3 次后失败", 15_000,
            metrics={"retries": 3, "duration_ms": 46800}),
    ]
    _, payload = build_batch(AGENT_ID, VM_ID, resources, events,
                             batch_id=fixed_ulid("2V9P5C8Y4B1N7D3H"),
                             sent_at=ts(16_000))
    return payload


# ---------------------------------------------------------------- 自校验

def validate_payload(p: dict, name: str) -> list[str]:
    """契约自校验。任何一条失败都会让生成失败 —— 样例必须是可提交的定版。"""
    errors: list[str] = []

    def err(msg: str) -> None:
        errors.append(f"{name}: {msg}")

    for key in ("agentId", "vmId", "agentVersion", "batchId", "sentAt",
                "resources", "events"):
        if key not in p:
            err(f"缺顶层字段 {key}")
    if errors:
        return errors

    if ":" in p["agentId"]:
        err(f"agentId 含冒号: {p['agentId']!r}")
    parts = p["vmId"].split(":")
    if len(parts) < 3 or parts[0] != "vm":
        err(f"vmId 非 'vm:zsvirt:<uuid>' 形态: {p['vmId']!r}")
    if not ULID_RE.match(p["batchId"]):
        err(f"batchId 非合法 ULID: {p['batchId']!r}")
    if not TS_RE.match(p["sentAt"]):
        err(f"sentAt 非 ISO8601 UTC ms: {p['sentAt']!r}")

    ref_index: set[tuple[str, str]] = set()
    for i, r in enumerate(p["resources"]):
        for key in ("kind", "sourceId", "name"):
            if not r.get(key):
                err(f"resources[{i}] 缺 {key}")
        if r["kind"] not in PROBE_KINDS:
            err(f"resources[{i}].kind 非法: {r['kind']!r}")
        if ":" in r["sourceId"]:
            err(f"resources[{i}].sourceId 含冒号（B 侧会拒收）: {r['sourceId']!r}")
        if r["kind"] in {"agent", "task"} and not ULID_RE.match(r["sourceId"]):
            err(f"resources[{i}] agent/task 的 sourceId 应为 ULID: {r['sourceId']!r}")
        for tkey in ("firstSeenAt", "lastSeenAt"):
            if r.get(tkey) and not TS_RE.match(r[tkey]):
                err(f"resources[{i}].{tkey} 时间格式非法: {r[tkey]!r}")
        if r.get("parentSourceId") and ":" in r["parentSourceId"]:
            err(f"resources[{i}].parentSourceId 含冒号: {r['parentSourceId']!r}")
        ref_index.add((r["kind"], r["sourceId"]))

    for i, e in enumerate(p["events"]):
        if not TS_RE.match(e.get("occurredAt", "")):
            err(f"events[{i}].occurredAt 时间格式非法: {e.get('occurredAt')!r}")
        if e.get("type") not in EVENT_TYPES:
            err(f"events[{i}].type 不在 F-01 冻结枚举: {e.get('type')!r}")
        if e.get("severity") not in SEVERITIES:
            err(f"events[{i}].severity 非法: {e.get('severity')!r}")
        ref = e.get("resourceRef", {})
        pair = (ref.get("kind"), ref.get("sourceId"))
        if pair not in ref_index:
            err(f"events[{i}].resourceRef 指向本批未上报的资源: {pair}")

    if len(p["resources"]) > 500:
        err("resources 超 500 上限（Q4）")
    if len(p["events"]) > 1000:
        err("events 超 1000 上限（Q4）")
    size = len(json.dumps(p).encode("utf-8"))
    if size > 1024 * 1024:
        err(f"payload 超 1MB（Q4）: {size} bytes")
    return errors


SAMPLES = {
    "normal.json": build_normal,
    "container_oom_killed.json": build_container_oom,
    "gpu_memory_exhausted.json": build_gpu_exhausted,
    "agent_network_failure.json": build_agent_network_failure,
}


def main() -> None:
    out_dir = _HERE
    failed = False
    for filename, builder in SAMPLES.items():
        payload = builder()
        errors = validate_payload(payload, filename)
        if errors:
            failed = True
            for e in errors:
                print(f"[FAIL] {e}")
            continue
        path = out_dir / filename
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        size = path.stat().st_size
        print(f"[OK] {filename}  resources={len(payload['resources'])} "
              f"events={len(payload['events'])}  {size} bytes  "
              f"batchId={payload['batchId']}")
    if failed:
        raise SystemExit(1)
    print("全部样例生成并通过契约自校验。")


if __name__ == "__main__":
    main()
