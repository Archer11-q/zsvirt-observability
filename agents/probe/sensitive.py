"""前置脱敏过滤（回应 CONTRACT_FREEZE F-05 问题 2）。

探针侧做「第一道」过滤，B 的 `normalize` 是「兜底」。规则对齐 `docs/SENSITIVE_DATA.md` §3：
- 不采集环境变量（/proc/<pid>/environ 不读，见 collector）。
- cmdline 敏感参数掩码。
- 白名单外的 attributes 字段不采集（见 collector）。
- `raw` 递归删除环境变量类敏感键。
"""

from __future__ import annotations

import re
from typing import Any

# 键名匹配：环境变量类 / 凭据类（SENSITIVE_DATA §3）
_SENSITIVE_KEY_SUFFIX = re.compile(
    r".*(_KEY|_TOKEN|_SECRET|_PASSWORD|_PASSWD|_CREDENTIAL)$", re.IGNORECASE
)
_SENSITIVE_KEY_PREFIX = re.compile(r"^(AWS_|OPENAI_)", re.IGNORECASE)
_CREDENTIAL_KEY = re.compile(
    r"(authorization|cookie|set-cookie|private_key|certificate)", re.IGNORECASE
)

# cmdline 中的敏感参数名
_CMD_SECRET_NAME = re.compile(
    r"(password|passwd|token|api[-_]?key|secret|authorization)", re.IGNORECASE
)


def is_sensitive_key(key: str) -> bool:
    """判断键名是否命中敏感规则。"""
    return bool(
        _SENSITIVE_KEY_SUFFIX.match(key)
        or _SENSITIVE_KEY_PREFIX.match(key)
        or _CREDENTIAL_KEY.match(key)
    )


def mask_cmdline(args: list[str]) -> list[str]:
    """掩码命令行中的敏感参数值。

    支持两种形态：`--password=secret` 与 `--password secret`。
    后者仅在「下一个参数不是另一个 flag」时才把下个参数当值掩码，
    避免 `--token --port 8000` 时误吞 `--port`。
    """
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if _CMD_SECRET_NAME.search(arg):
            if "=" in arg:
                out.append(re.sub(r"=.*$", "=***", arg))
            elif i + 1 < len(args) and not args[i + 1].startswith("-"):
                # `--token secret`：掩码下一个参数值
                out.append(arg)
                out.append("***")
                i += 1
            else:
                out.append(arg)  # 无值或后接另一个 flag
        else:
            out.append(arg)
        i += 1
    return out


def filter_sensitive(raw: dict[str, Any]) -> dict[str, Any]:
    """递归删除 `raw` 中命中敏感键的字段，保留其余内容。"""
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if is_sensitive_key(key):
            continue
        if isinstance(value, dict):
            result[key] = filter_sensitive(value)
        elif isinstance(value, list):
            result[key] = [
                filter_sensitive(v) if isinstance(v, dict) else v for v in value
            ]
        else:
            result[key] = value
    return result
