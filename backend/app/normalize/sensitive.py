"""敏感信息过滤与脱敏（B 侧兜底关口）。

设计与规则见 `docs/SENSITIVE_DATA.md`。

**双层防护的职责划分**：

| 层 | 位置 | 角色 |
|---|---|---|
| 第一道 | 成员 A 的探针（`agents/probe/sensitive.py`） | 不采集环境变量、cmdline 掩码、attributes 白名单 |
| **兜底** | **本模块（B 侧 `normalize` 之前）** | 不假设 A 已脱敏；入库前再过一遍 |

两侧**必须逐条行为一致** —— 实现依据同一份测试向量
（`shared/sensitive_vectors.json`），由 `tests/test_sensitive_vectors.py` 断言。
这样"规则一致"就是可验证事实，而不是口头承诺。

设计约束：

- **纯函数、无外部依赖**：便于单测，也便于将来被探针复用同一套向量校验。
- **幂等**：已掩码的内容再次处理结果不变（掩码值是 `***`，不含敏感参数名）。
- **不猜测**：只按规则处理，不尝试"智能识别"。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------- 键名规则

# 后缀匹配：环境变量类 / 凭据类（SENSITIVE_DATA §3）
_SENSITIVE_KEY_SUFFIX = re.compile(
    r".*(_KEY|_TOKEN|_SECRET|_PASSWORD|_PASSWD|_CREDENTIAL)$", re.IGNORECASE
)

# 前缀匹配：AWS_* / OPENAI_* 等云厂商凭据
_SENSITIVE_KEY_PREFIX = re.compile(r"^(AWS_|OPENAI_)", re.IGNORECASE)

# 整名匹配（含子串）：HTTP 凭据与证书类
_CREDENTIAL_KEY = re.compile(
    r"(authorization|cookie|set-cookie|private_key|certificate)", re.IGNORECASE
)

# 裸字段名：`password` / `passwd` / `secret` / `token` 这类不带下划线前缀的形态。
#
# ⚠️ 这是 **B 侧刻意比探针更严** 的一条规则（探针当前只认 `_PASSWORD` 后缀）。
# 理由：`{"password": "x"}` 是最常见的现场形态，探针侧漏掉会形成真实泄漏路径，
# 而 B 是兜底关口，不能跟着漏。见 docs/SENSITIVE_DATA.md §9。
# 用全等匹配（不是子串），因此 `password_hint`、`secretary`、`tokenizer`
# 这类词不会被误杀。
_BARE_SECRET_KEY = re.compile(
    r"^(password|passwd|secret|token|apikey|api_key|access_key|secret_key)$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------- 命令行规则

# cmdline 中的敏感参数名（注意：`--tokenizer` 这类会被误命中，
# 这是与探针一致地接受的已知折中 —— 见 SENSITIVE_DATA §9 的说明）
_CMD_SECRET_NAME = re.compile(
    r"(password|passwd|token|api[-_]?key|secret|authorization)", re.IGNORECASE
)

MASK = "***"


def is_sensitive_key(key: str) -> bool:
    """判断键名是否命中敏感规则。

    三条规则取或：

      1. **后缀** `_KEY` / `_TOKEN` / `_SECRET` / `_PASSWORD` / `_PASSWD` / `_CREDENTIAL`
      2. **前缀** `AWS_` / `OPENAI_`
      3. **子串** `authorization` / `cookie` / `set-cookie` / `private_key` / `certificate`
      4. **全等** `password` / `passwd` / `secret` / `token` / `api_key` 等裸字段名
         （**B 侧比探针更严**，见 `docs/SENSITIVE_DATA.md` §9）

    第 3 条用子串匹配（`search`）而非前缀匹配 —— 这是与探针侧
    `agents/probe/sensitive.py` **刻意保持一致**的选择：子串匹配更宽松
    （会命中 `cookie_count` 这类反例），但"两侧一致"比"单侧更精确"更重要。

    **已知折中**：`cookie_count` 会被误判为敏感。对这类字段的过度脱敏只损失
    一点诊断信息，不构成泄漏；而两侧规则不一致会形成**真实缝隙**，风险更高。
    """
    if not isinstance(key, str):
        return False
    return bool(
        _SENSITIVE_KEY_SUFFIX.match(key)
        or _SENSITIVE_KEY_PREFIX.match(key)
        or _CREDENTIAL_KEY.search(key)
        or _BARE_SECRET_KEY.match(key)
    )


def mask_cmdline(args: Any) -> list[str]:
    """掩码命令行中的敏感参数值。

    支持两种形态：

    - `--password=secret`  → `--password=***`（等号形式）
    - `--password secret`  → `--password ***`（分离形式，
      **仅当下一项不是另一个 flag 时才掩码**，避免 `--token --port 8000`
      把 `--port` 吃掉）

    非字符串项按 `str()` 转换；非列表输入返回空列表（调用方应先做类型校验）。
    """
    if not isinstance(args, list):
        return []

    out: list[str] = []
    i = 0
    while i < len(args):
        arg = str(args[i])
        if _CMD_SECRET_NAME.search(arg):
            if "=" in arg:
                out.append(re.sub(r"=.*$", f"={MASK}", arg))
            elif i + 1 < len(args) and not str(args[i + 1]).startswith("-"):
                out.append(arg)
                out.append(MASK)
                i += 1
            else:
                out.append(arg)
        else:
            out.append(arg)
        i += 1
    return out


def filter_sensitive(raw: Any, *, mask: bool = False) -> Any:
    """递归过滤敏感字段。

    对 **dict 与 list** 都递归清理：

    - dict：命中规则的键直接**删除**（`mask=True` 时改为把值替换为 `MASK`）
    - list / tuple：逐元素递归（**必须**处理 —— 凭据藏在数组元素里
      是真实泄漏路径，例如 Docker inspect 返回的 `Env` 数组）
    - 其余类型原样返回

    不修改入参。

    ⚠️ **与探针侧的已知差异**：探针的 `filter_sensitive` 只处理 dict
    第一层且不递归；本函数递归且处理 list。**B 侧更严** —— 因为 B 是兜底关口，
    不能因为探针漏了就跟着漏。见 `docs/SENSITIVE_DATA.md` §9。
    """
    if isinstance(raw, dict):
        result: dict[str, Any] = {}
        for key, value in raw.items():
            if is_sensitive_key(key):
                if mask:
                    result[key] = MASK
                continue
            result[key] = filter_sensitive(value, mask=mask)
        return result
    if isinstance(raw, (list, tuple)):
        return [filter_sensitive(item, mask=mask) for item in raw]
    return raw


# ---------------------------------------------------------------- 集成入口


def should_drop_field(key: str) -> bool:
    """白名单优先策略下的判定入口（SENSITIVE_DATA §3）。

    当前实现等价于 `is_sensitive_key`；保留独立函数名是为了将来引入
    "未经契约声明的字段一律丢弃"的白名单逻辑时，调用方无需改动。
    """
    return is_sensitive_key(key)


def redact_event(payload: dict[str, Any]) -> dict[str, Any]:
    """对一条待落库的事件做整体脱敏。

    处理面：

    - `raw`：递归丢弃敏感键
    - `attributes`：递归丢弃敏感键（防止探针白名单漏洞导致的环境变量泄漏）
    - `message`：按连接串 / 常见密钥形态做掩码
    - `cmdline`（若在 `attributes` 中）：单独走 `mask_cmdline` 的等号/分离形态处理

    不修改入参（返回新对象）。`Event` 的必填字段（`type` / `severity` /
    `occurredAt` / `resourceRef`）不在此处校验，由上层 Schema 校验负责。
    """
    out: dict[str, Any] = dict(payload)

    if "raw" in out:
        out["raw"] = filter_sensitive(out["raw"])

    attrs = out.get("attributes")
    if isinstance(attrs, dict):
        attrs = filter_sensitive(attrs)
        # cmdline 单独处理：掩码参数值而不是整个删掉（保留诊断价值）
        if "cmdline" in attrs:
            cmd = attrs["cmdline"]
            if isinstance(cmd, list):
                attrs["cmdline"] = mask_cmdline(cmd)
            elif isinstance(cmd, str):
                attrs["cmdline"] = _mask_cmdline_string(cmd)
        out["attributes"] = attrs

    if isinstance(out.get("message"), str):
        out["message"] = mask_secrets_in_text(out["message"])

    return out


def _mask_cmdline_string(cmdline: str) -> str:
    """处理以字符串形式给出的 cmdline（有些采集源给的是整串）。"""
    return " ".join(mask_cmdline(cmdline.split()))


# 连接串中的用户名密码：scheme://user:pass@host
_CONN_STRING = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/@\s]+):(?P<pw>[^@\s]+)@"
)

# 常见密钥形态前缀
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"),
)


def mask_secrets_in_text(text: str) -> str:
    """对自由文本做掩码：连接串凭据 + 常见密钥形态。

    只做形态匹配，不做语义猜测。用于 `Event.message` 这类人可读字段。
    """
    if not isinstance(text, str):
        return text

    masked = _CONN_STRING.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{MASK}@", text)
    for pattern in _SECRET_VALUE_PATTERNS:
        masked = pattern.sub(MASK, masked)
    return masked
