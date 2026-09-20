"""读取 /proc 的共享工具（只读、容错），供 process / ai_service / network 采集器复用。"""

from __future__ import annotations

import os
import re

# 容器归属的 cgroup 特征
CGROUP_MARKERS = ("docker", "kubepods", "containerd", "lxc")

# 容器 ID：cgroup 路径中的 64 位十六进制段（Docker / containerd 均如此）
_CONTAINER_ID_RE = re.compile(r"/([0-9a-f]{64})(?:/|$|\s)")

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
    """解析 /proc/<pid>/stat。comm 字段可能含空格，从右括号处切分。

    字段索引（相对 `comm` 之后的 `rest` 数组，即 `rest[n]` = 第 `n+3` 字段）：
    - state（3）、ppid（4）
    - utime（14）= rest[11]、stime（15）= rest[12]
    - starttime（22）= rest[19]
    - delayacct_blkio_ticks（42）= rest[39]（供 io_wait 窗口化；内核未开
      CONFIG_TASK_DELAY_ACCT 时为 0）
    """
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
        if len(rest) >= 40:
            fields["utime"] = rest[11]
            fields["stime"] = rest[12]
            fields["blkio_ticks"] = rest[39]
    return fields


def read_cmdline(pid: int) -> list[str]:
    """读取 /proc/<pid>/cmdline，按 NUL 切分。"""
    raw = read_text(f"/proc/{pid}/cmdline")
    return [a for a in raw.split("\0") if a] if raw else []


def in_container(pid: int) -> bool:
    """判断进程是否属于容器（cgroup 命中 docker/k8s 特征）。"""
    raw = read_text(f"/proc/{pid}/cgroup")
    return bool(raw and any(m in raw for m in CGROUP_MARKERS))


def container_source_id(pid: int) -> str | None:
    """解析进程所属容器的 sourceId（容器 ID 前 12 位，D-031）。

    从 `/proc/<pid>/cgroup` 中提取 64 位十六进制容器 ID，取前 12 位。
    与 `ContainerCollector` 用 Docker API `Id[:12]` 得到的是**同一个值**，
    因此可作为 `parentSourceId` 把进程/服务挂到容器资源下。非容器进程返回 None。
    """
    raw = read_text(f"/proc/{pid}/cgroup")
    if not raw:
        return None
    for line in raw.splitlines():
        m = _CONTAINER_ID_RE.search(line)
        if m:
            return m.group(1)[:12]
    return None


def extract_port(cmdline: list[str]) -> int | None:
    """从命令行提取监听端口（`--port 8000` / `-p 8000` / `--port=8000`）。"""
    for i, arg in enumerate(cmdline):
        if arg in ("--port", "-p") and i + 1 < len(cmdline):
            try:
                return int(cmdline[i + 1])
            except ValueError:
                return None
        if arg.startswith("--port="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                return None
    return None


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
