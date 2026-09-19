"""AI 服务采集器：从进程命令行识别 AI 服务。

产出 `ai_service` 资源，`parentSourceId` 挂到所属容器（cgroup 解析）。
`inference.*` 事件依赖 AI 服务的日志/指标接口（见 PROBE_DESIGN R1、
FREEZE_ACK 专业意见 1），待日志规范明确后实现。
"""

from __future__ import annotations

from ..idgen import ai_service_source_id
from ..model import Event, Resource
from .base import Collector
from .procutil import (
    container_source_id,
    extract_port,
    framework_name,
    list_pids,
    read_cmdline,
)


class AIServiceCollector(Collector):
    name = "ai_service"
    interval = 10.0

    def collect(self) -> tuple[list[Resource], list[Event]]:
        resources: list[Resource] = []
        events: list[Event] = []

        for pid in list_pids():
            cmdline = read_cmdline(pid)
            framework = framework_name(cmdline)
            if not framework:
                continue
            port = extract_port(cmdline)
            if port is None:
                continue  # sourceId 依赖「服务名+端口」，端口未知则跳过

            source_id = ai_service_source_id(framework, port)
            # 服务所属容器（若有）：ai_service 挂到容器资源下（container→ai_service 边）
            parent_id = container_source_id(pid)
            resources.append(
                Resource(
                    kind="ai_service",
                    source_id=source_id,
                    name=framework,
                    parent_source_id=parent_id,
                    attributes={
                        "framework": framework,
                        "endpoint": f"http://127.0.0.1:{port}",
                        "pid": pid,
                    },
                )
            )

        # inference.timeout / inference.error 依赖推理服务日志或指标接口，
        # 此处留空，待团队明确「AI 工作负载日志规范」后实现。
        return resources, events
