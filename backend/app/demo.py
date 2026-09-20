"""可复现的演示运行器（`docs/DEVELOPMENT_PLAN.md` 任务 7 的核心交付物）。

三个子命令，覆盖演示需要的全部动作：

| 命令 | 用途 |
|---|---|
| `reset` | 清空数据，让演示从干净状态开始（**只删数据，不动表结构**） |
| `seed` | 灌入一个或全部故障场景，产生告警与自动诊断 |
| `show` | 把当前状态渲染成给评委看的报告（告警 → 诊断 → 影响范围 → 建议） |
| `demo` | `reset` + `seed` + `show` 一条龙，用于现场 |

## 为什么这些场景是"模拟"的，以及为什么必须说明

演示机通常没有 GPU（本项目的开发机就没有），因此场景数据来自
`app/zsvirt` 的 `SimulatedProvider`。`DATA_MODEL.md` §4.2.1 的诚实性要求是：
模拟数据**必须可识别**。所以：

- 每条读数的 `origin` 是 `simulated`；
- 事件的 `metrics` 与 `message` 都来自模拟读数；
- `show` 的输出**开头就打印**"以下数据由模拟渠道产生"，而不是藏在脚注里；
- `GET /api/health` 的 `gpuProvider.mode` 也是 `simulated`。

一条模拟的读数被当成真实 GPU 数据来讲解，是这个项目最严重的信任事故，
比任何功能缺失都糟。

## 为什么演示数据必须可复现

`SimulatedProvider(seed=...)` 决定了指标；场景定义在
`app/zsvirt/scenarios.py`，两者的参数都固定。因此**同一台机器上重复运行
得到同样的数字**，讲解词不会与界面对不上。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.db import get_session_factory
from app.enums import Severity
from app.zsvirt.scenarios import SCENARIO_NAMES, build_batch, describe_scenarios

#: 演示时统一使用的时间基准。固定值 —— 让所有时间戳可复现。
DEMO_NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- 清空


def reset(session: Session) -> dict[str, int]:
    """清空演示数据（**保留表结构**）。

    用 `TRUNCATE ... CASCADE` 而不是 drop/create：演示中途重置不该触发建表，
    也不该让 Alembic 版本表丢状态。
    """
    from app.models import Base

    counts: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        counts[table.name] = int(
            session.execute(text(f'SELECT count(*) FROM "{table.name}"')).scalar() or 0
        )

    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    if names:
        session.execute(text(f"TRUNCATE {names} CASCADE"))
    session.commit()
    return counts


# ---------------------------------------------------------------- 灌入


def seed(
    session: Session,
    names: list[str],
    *,
    now: datetime | None = None,
    diagnose_warnings: bool = False,
) -> list[dict]:
    """灌入指定场景，返回每个场景的摘要。

    直接调用 `ingest_batch` 而不是走 HTTP：演示脚本不该依赖服务是否已启动，
    而且这样能拿到逐场景的返回值（HTTP 每次一个批次）。

    `diagnose_warnings=True` 时，对自动诊断**按门限跳过**的告警走一次手动路径。
    这不是绕过门限，而是演示该有的分工：自动覆盖严重事故，其余按需触发。
    """
    from app.diagnosis.auto import run_auto_diagnosis
    from app.ingest.schemas import IngestBatch
    from app.ingest.service import ingest_batch

    moment = now or DEMO_NOW
    summaries: list[dict] = []

    for index, name in enumerate(names):
        payload = build_batch(name, at=moment, batch_id=f"demo-{name}")
        result, duplicate = ingest_batch(
            session,
            batch=IngestBatch.model_validate(payload),
            received_at=moment + timedelta(seconds=index),
        )
        # 告警引擎已在 `ingest_batch` 内部（同事务）求值完毕，这里只做自动诊断。
        session.commit()

        auto = run_auto_diagnosis(session, list(result.touchedAlertIds), now=moment)
        session.commit()

        manual: dict[str, str] = {}
        if diagnose_warnings and auto.skipped_below_severity:
            # 覆盖被门限跳过的告警。
            #
            # **复用同一个函数**并传更低门限，而不是在这里另写一遍
            # resolve → run → link：那样会绕过事故聚合，对同一事故里的
            # warning 告警再产出一条重复结论（第一版就是这么错的）。
            skipped = list(auto.skipped_below_severity)
            relaxed = run_auto_diagnosis(
                session,
                skipped,
                min_severity=Severity.INFO.value,
                now=moment,
            )
            session.commit()
            manual = {
                alert_id: diag_id
                for alert_id, diag_id in relaxed.linked.items()
                if alert_id in skipped
            }
            # 放宽门限后已经全部处理，原摘要里的"跳过"不再成立
            auto.skipped_below_severity.clear()
            for alert_id, diag_id in manual.items():
                auto.linked[alert_id] = diag_id

        summaries.append(
            {
                "scenario": name,
                "batchId": result.batchId,
                "duplicate": duplicate,
                "resources": result.accepted.resources,
                "events": result.accepted.events,
                "alerts": result.alerts,
                "autoDiagnosis": auto.as_dict(),
                "manualDiagnosis": manual,
            }
        )
    return summaries


# ---------------------------------------------------------------- 展示


def seed_cross_layer(session: Session, *, now: datetime | None = None) -> list[dict]:
    """灌入跨层场景：平台层 → 成员 A 的探针症状 → B 的 GPU 根因。

    三个批次缺一不可 —— 探针在 VM 内看不到平台侧显存，GPU 渠道看不到 VM 内进程，
    跨层结论只能由两者叠在同一条链上得出。
    """
    from app.diagnosis.auto import run_auto_diagnosis
    from app.ingest.schemas import IngestBatch
    from app.ingest.service import ingest_batch
    from app.zsvirt.scenarios import build_cross_layer_batches

    moment = now or DEMO_NOW
    summaries: list[dict] = []

    for index, payload in enumerate(build_cross_layer_batches()):
        result, duplicate = ingest_batch(
            session,
            batch=IngestBatch.model_validate(payload),
            received_at=moment + timedelta(seconds=index),
        )
        session.commit()

        auto = run_auto_diagnosis(session, list(result.touchedAlertIds), now=moment)
        session.commit()

        summaries.append(
            {
                "batchId": result.batchId,
                "duplicate": duplicate,
                "resources": result.accepted.resources,
                "events": result.accepted.events,
                "alerts": result.alerts,
                "autoDiagnosis": auto.as_dict(),
            }
        )
    return summaries


def collect(session: Session) -> dict[str, Any]:
    """把当前状态整理成一份报告用的结构。"""
    from app.alerts.repository import query_alerts
    from app.diagnosis.service import query_diagnoses
    from app.graph.repository import load_graph

    resource_count = int(session.execute(text("select count(*) from resource")).scalar() or 0)
    nodes, edges = load_graph(session)
    alerts, _ = query_alerts(session, limit=100)
    diagnoses, _ = query_diagnoses(session, limit=100)

    return {
        "counts": {
            "resources": resource_count,
            "edges": len(edges),
            "alerts": len(alerts),
            "diagnoses": len(diagnoses),
        },
        "alerts": [
            {
                "id": a.id,
                "ruleId": a.rule_id,
                "title": a.title,
                "severity": a.severity,
                "state": a.state,
                "count": a.count,
                "resourceId": a.resource_id,
                "evidenceEventIds": list(a.evidence_event_ids or []),
                "diagnosisId": a.diagnosis_id,
                "firstFiredAt": a.first_fired_at.isoformat(),
                "lastFiredAt": a.last_fired_at.isoformat(),
                "labels": a.labels or {},
            }
            for a in alerts
        ],
        "diagnoses": [
            {
                "id": d.id,
                "createdAt": d.created_at.isoformat(),
                "trigger": d.trigger,
                "rootCause": d.root_cause,
                "confidence": d.confidence,
                "confidenceBreakdown": d.confidence_breakdown or [],
                "affectedResources": list(d.affected_resources or []),
                "onChain": list(d.on_chain or []),
                "potentiallyAffected": list(d.potentially_affected or []),
                "evidence": d.evidence or [],
                "recommendation": d.recommendation or [],
                "ruleSetVersion": d.rule_set_version,
                "notes": list(d.notes or []),
            }
            for d in diagnoses
        ],
        "topology": {
            "nodes": [
                {"id": r.id, "kind": r.kind, "name": r.name, "parentId": r.parent_id}
                for r in nodes
            ],
            "edges": [
                {"parentId": e.parent_id, "childId": e.child_id} for e in edges
            ],
        },
        "resourceKinds": dict(
            session.execute(
                text("select kind, count(*) from resource group by kind order by kind")
            ).all()
        ),
    }


CAUSE_LABELS = {
    "GPU_MEMORY_EXHAUSTED": "GPU 显存耗尽（本机超配）",
    "GPU_NEIGHBOR_CONTENTION": "GPU 争用（同宿主其他负载占用）",
    "GPU_UTILIZATION_SATURATED": "GPU 算力饱和",
    "CONTAINER_MEMORY_LIMIT": "容器内存限额触顶",
    "CONTAINER_RESTART_LOOP": "容器重启循环",
    "VM_MEMORY_EXHAUSTED": "虚拟机内存耗尽",
    "VM_DISK_IO_SATURATED": "虚拟机磁盘 I/O 饱和",
    "NETWORK_UNREACHABLE": "网络不可达",
    "AGENT_TASK_FAILURE": "智能体任务失败",
    "MODEL_COMPUTE_BOUND": "模型计算受限",
    "UNKNOWN": "未识别（证据不足）",
}


def render(report: dict[str, Any], *, out=sys.stdout) -> None:
    """把报告渲染成给人看的文本。**开头必须声明数据来源**。"""
    def line(text_: str = "") -> None:
        out.write(text_ + "\n")

    line("=" * 74)
    line(f"Crosslayer 演示报告   v{__version__}")
    line("=" * 74)
    line()
    line("⚠️  数据来源声明：本报告的场景数据由**模拟 GPU 渠道**（SimulatedProvider）")
    line("    产生 —— 演示机没有 GPU。模拟数据的 origin=simulated 全程可见，")
    line("    见 GET /api/health 的 gpuProvider.mode。真实部署下该字段会是")
    line("    zsvirt-zwatch 或 probe。")
    line()

    counts = report["counts"]
    line(f"资源 {counts['resources']}  边 {counts['edges']}  "
         f"告警 {counts['alerts']}  诊断 {counts['diagnoses']}")
    line()

    line("-" * 74)
    line("① 资源拓扑（真实上报链路，非手工构造）")
    line("-" * 74)
    children = {e["childId"]: e["parentId"] for e in report["topology"]["edges"]}
    by_id = {n["id"]: n for n in report["topology"]["nodes"]}
    roots = [n for n in report["topology"]["nodes"] if n["id"] not in children]
    for root in sorted(roots, key=lambda n: n["id"]):
        _render_subtree(root["id"], by_id, children, line, prefix="", last=True)
    line()

    line("-" * 74)
    line("② 告警（由规则自动产生，不是手工插入）")
    line("-" * 74)
    if not report["alerts"]:
        line("  （无）")
    for alert in report["alerts"]:
        line(
            f"  [{alert['severity']:8s}] {alert['ruleId']:22s} {alert['title']}"
        )
        line(
            f"            资源={alert['resourceId']}  状态={alert['state']}  "
            f"触发次数={alert['count']}  证据={len(alert['evidenceEventIds'])} 条"
        )
        if alert["diagnosisId"]:
            line(f"            → 已关联诊断 {alert['diagnosisId']}")
    line()

    line("-" * 74)
    line("③ 根因诊断（规则加权评分，可由下方贡献逐项复算）")
    line("-" * 74)
    if not report["diagnoses"]:
        line("  （无）")
    for diag in report["diagnoses"]:
        cause = diag["rootCause"]
        line(f"  {cause}  —— {CAUSE_LABELS.get(cause, cause)}")
        line(f"    置信 {diag['confidence']}   规则集 {diag['ruleSetVersion']}")
        for item in diag["confidenceBreakdown"]:
            line(
                f"      {item['ruleId']:22s} {item['contribution']:+.2f}  "
                f"{item.get('observed', '')}"
            )
        line(f"    受影响（有证据+下游）: {diag['affectedResources'] or '（无）'}")
        line(f"    证据链过渡层          : {diag['onChain'] or '（无）'}")
        line(f"    潜在受影响（无证据）  : {diag['potentiallyAffected'] or '（无）'}")
        kinds = [f"{e.get('type')}:{e.get('name')}" for e in diag["evidence"]]
        line(f"    证据: {kinds}")
        for advice in diag["recommendation"]:
            line(f"    建议: [{advice['code']}] {advice['text']}")
        for note in diag["notes"]:
            line(f"    备注: {note}")
        line()

    line("=" * 74)
    line("演示要点：GPU 归因的区分是本项目的核心能力 ——")
    line("  宿主 GPU 满 + 本机 vGPU 占用高  ⇒ GPU_MEMORY_EXHAUSTED（自己超配）")
    line("  宿主 GPU 满 + 本机 vGPU 占用低  ⇒ GPU_NEIGHBOR_CONTENTION（邻居占用）")
    line("  两者的处置方式完全不同，单来源采集无法区分。")
    line("=" * 74)


def _render_subtree(
    node_id: str,
    by_id: dict[str, dict],
    children: dict[str, str],
    line,
    *,
    prefix: str,
    last: bool,
) -> None:
    node = by_id.get(node_id)
    if node is None:
        return
    connector = "└─ " if last else "├─ "
    line(f"  {prefix}{connector}{node['kind']:10s} {node['id']}")
    kids = sorted(k for k, parent in children.items() if parent == node_id)
    for index, kid in enumerate(kids):
        _render_subtree(
            kid,
            by_id,
            children,
            line,
            prefix=prefix + ("   " if last else "│  "),
            last=index == len(kids) - 1,
        )


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.demo",
        description="Crosslayer 演示运行器（可复现）",
    )
    parser.add_argument(
        "command",
        choices=["reset", "seed", "show", "demo", "list"],
        help="reset=清空数据；seed=灌入场景；show=渲染报告；demo=三者一条龙；list=列出场景",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=list(SCENARIO_NAMES),
        help="要灌入的场景，可重复；省略时 demo/seed 使用全部场景",
    )
    parser.add_argument("--json", action="store_true", help="show 输出 JSON 而不是文本报告")
    parser.add_argument(
        "--cross-layer",
        action="store_true",
        help="灌入跨层场景（平台层 + 成员 A 的探针症状 + B 的 GPU 根因）",
    )
    parser.add_argument(
        "--diagnose-warnings",
        action="store_true",
        help="对自动诊断按门限跳过的告警补跑一次手动诊断（演示两条路径）",
    )
    args = parser.parse_args(argv)

    if args.command == "list":
        for name, description in describe_scenarios().items():
            print(f"  {name:24s} {description}")
        return 0

    session = get_session_factory()()
    try:
        if args.command == "reset":
            counts = reset(session)
            print("已清空：" + ", ".join(f"{k}={v}" for k, v in counts.items() if v))
            print("（表结构保留）")
            return 0

        if args.command in ("seed", "demo") and args.cross_layer:
            if args.command == "demo":
                reset(session)
            print("跨层场景：平台层（ZSvirt 资产） → 成员 A 的探针样例 → B 的 GPU 渠道")
            print("  探针在 VM 内看不到平台侧显存；GPU 渠道看不到 VM 内进程。")
            print("  跨层结论只能由两者叠在同一条链上得出 —— 这是单来源采集做不到的。")
            print()
            for summary in seed_cross_layer(session):
                auto = summary["autoDiagnosis"]
                print(
                    f"  [{summary['batchId']}] "
                    f"资源 {summary['resources']}  事件 {summary['events']}  "
                    f"新建告警 {summary['alerts'].get('created', 0)}  "
                    f"自动诊断 {len(auto.get('linked', {}))} 条"
                )
        elif args.command in ("seed", "demo"):
            names = args.scenario or list(SCENARIO_NAMES)
            if args.command == "demo":
                reset(session)
            summaries = seed(session, names, diagnose_warnings=args.diagnose_warnings)
            for summary in summaries:
                alerts = summary["alerts"]
                auto = summary["autoDiagnosis"]
                print(
                    f"  [{summary['scenario']}] "
                    f"资源 {summary['resources']}  事件 {summary['events']}  "
                    f"新建告警 {alerts.get('created', 0)}  累加 {alerts.get('updated', 0)}  "
                    f"自动诊断 {len(auto.get('linked', {}))} 条"
                )
                # **如实报告跳过了什么、为什么** ——
                # 否则"这条告警为什么没有诊断"只能去翻设计文档
                for key, why in (
                    ("skippedBelowSeverity", "严重级别低于自动诊断门限（error）"),
                    ("skippedAlreadyDiagnosed", "已有诊断结论（幂等）"),
                    ("skippedNotFiring", "已恢复或已静默"),
                ):
                    count = auto.get(key, 0)
                    if count:
                        print(f"            跳过 {count} 条：{why}")
                if auto.get("truncated"):
                    print("            ⚠️ 已达单次诊断上限，其余留待手动补")
                if summary.get("manualDiagnosis"):
                    print(
                        f"            手动补诊断 {len(summary['manualDiagnosis'])} 条"
                        "（演示「按需触发」路径）"
                    )
            if args.command == "seed":
                return 0

        report = collect(session)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            render(report)
        return 0
    finally:
        session.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
