"""读取 /proc 的共享工具（只读、容错），供 process / ai_service 采集器复用。"""

from __future__ import annotations

import os

# 容器归属的 cgroup 特征
CGROUP_MARKERS = ("docker", "kubepods", "containerd", "lxc")

# AI 服务框架特征（cmdline 命中即视为 AI 框架进程）
_FRAMEWORKS = {
    "vllm": "vllm",
    "triton": "triton",
    "ollama": "ollama",
    "sglang": "sglang",
    "torchserve": "torchserve",
    "uvicorn": "uvicorn",
    "gunicorn": "gunicorn",
    "fastapi": "fastapi",
}


def read_text(path: str) -> str | None:
    """读取文件为文本，失败返回 None。"""
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def list_pids() -> list[int]:
    """列出 /proc 下所有数字目录名（pid）。"""
    try:
        return [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return []


def read_stat(pid: int) -> dict[str, str] | None:
    """解析 /proc/<pid>/stat。comm 字段可能含空格，从右括号处切分。"""
    raw = read_text(f"/proc/{pid}/stat")
    if not raw:
        return None
    close = raw.rfind(")")
    head, rest = raw[: close + 1].split(" ", 1), raw[close + 2 :].split()
    fields = {"pid": head[0], "comm": head[1].strip("()")}
    if len(rest) >= 20:
        fields["state"] = rest[0]  # 进程状态，D=不可中断睡眠
        fields["ppid"] = rest[1]
        fields["starttime"] = rest[19]  # 第 22 字段
    return fields


def read_cmdline(pid: int) -> list[str]:
    """读取 /proc/<pid>/cmdline，按 NUL 切分。"""
    raw = read_text(f"/proc/{pid}/cmdline")
    return [a for a in raw.split("\0") if a] if raw else []


def in_container(pid: int) -> bool:
    """判断进程是否属于容器（cgroup 命中 docker/k8s 特征）。"""
    raw = read_text(f"/proc/{pid}/cgroup")
    return bool(raw and any(m in raw for m in CGROUP_MARKERS))


def rss_bytes(pid: int) -> int:
    """读取 VmRSS（kB），返回字节数。"""
    status = read_text(f"/proc/{pid}/status") or ""
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                return 0
    return 0


def framework_name(cmdline: list[str]) -> str | None:
    """从命令行识别 AI 框架名，未命中返回 None。"""
    joined = " ".join(cmdline)
    for marker, name in _FRAMEWORKS.items():
        if marker in joined:
            return name
    return None


def is_framework(cmdline: list[str]) -> bool:
    return framework_name(cmdline) is not None
