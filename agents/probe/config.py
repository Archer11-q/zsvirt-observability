"""配置加载：环境变量 > JSON 配置文件 > 默认值。

零第三方依赖：不使用 YAML，配置文件为 JSON（标准库 json 解析）。
凭据（token）仅从环境变量注入，绝不写入配置文件或仓库。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields
from pathlib import Path

log = logging.getLogger("probe.config")

ENV_PREFIX = "ZSVIRT_OBS_"

# 环境变量后缀 -> 配置字段名 的映射
_ENV_FIELD_MAP = {
    "BACKEND_URL": "backend_url",
    "PROBE_TOKEN": "probe_token",
    "VM_ID": "vm_id",
    "AGENT_ID": "agent_id",
    "BATCH_SEC": "batch_sec",
    "EVENT_FLUSH_SEC": "event_flush_sec",
    "BATCH_MAX_EVENTS": "batch_max_events",
    "BATCH_MAX_RESOURCES": "batch_max_resources",
    "BATCH_MAX_BYTES": "batch_max_bytes",
    "BUFFER_PATH": "buffer_path",
    "BUFFER_MAX_MB": "buffer_max_mb",
    "CONFIG_FILE": "config_file",
    "LOG_LEVEL": "log_level",
}


@dataclass
class Config:
    """探针运行配置。所有字段均可被 JSON 配置或环境变量覆盖。"""

    backend_url: str = "http://localhost:8080"
    probe_token: str = ""  # 可选 Bearer token，空 = 关闭鉴权
    vm_id: str = ""  # ZSvirt VM UUID，必填（Push 挂 Pull 的唯一锚点）
    agent_id: str = ""  # 默认派生为 probe-<vm-uuid 前 8 位>
    batch_sec: float = 5.0  # 批次聚合间隔（秒）
    event_flush_sec: float = 1.0  # 有事件时最长缓冲（秒），实时优先
    batch_max_events: int = 1000
    batch_max_resources: int = 500
    batch_max_bytes: int = 1_048_576  # 单批 payload 上限 1MB
    buffer_path: str = "probe_buffer.db"
    buffer_max_mb: int = 100
    config_file: str = "config.json"
    log_level: str = "INFO"


def load() -> Config:
    """按优先级加载配置：环境变量 > JSON 配置文件 > 默认值。"""
    cfg = Config()

    # 1) JSON 配置文件
    cfg_file = os.environ.get(ENV_PREFIX + "CONFIG_FILE", cfg.config_file)
    if Path(cfg_file).is_file():
        try:
            data = json.loads(Path(cfg_file).read_text(encoding="utf-8"))
            for key, value in data.items():
                if any(f.name == key for f in fields(Config)):
                    setattr(cfg, key, value)
        except (json.JSONDecodeError, OSError) as exc:  # noqa: BLE001
            log.warning("配置文件 %s 解析失败，已忽略: %s", cfg_file, exc)

    # 2) 环境变量覆盖
    for env_suffix, field_name in _ENV_FIELD_MAP.items():
        raw = os.environ.get(ENV_PREFIX + env_suffix)
        if raw is None:
            continue
        default = getattr(cfg, field_name)
        setattr(cfg, field_name, _coerce(raw, default))

    # 3) 派生字段
    if not cfg.agent_id and cfg.vm_id:
        cfg.agent_id = f"probe-{cfg.vm_id[:8]}"

    return cfg


def validate(cfg: Config) -> None:
    """启动自检。vmId 与 backend_url 缺失时直接报错退出，避免静默采集。"""
    if not cfg.vm_id:
        raise SystemExit(
            "缺少 vmId：请设置环境变量 ZSVIRT_OBS_VM_ID（ZSvirt VM UUID）。"
            "它是 Push 数据挂到 Pull 资源的唯一锚点。"
        )
    if not cfg.backend_url:
        raise SystemExit("缺少 ZSVIRT_OBS_BACKEND_URL（后端地址）。")


def _coerce(raw: str, default):
    """把字符串环境变量转换为字段同类型。"""
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
