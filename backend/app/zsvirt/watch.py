"""ZWatch 指标渠道客户端（`docs/DATA_MODEL.md` §4.2.1 的 L2 层）。

本模块把 ZSvirt `zwatch` 的**指标查询 API** 封成一个可注入、可测试的客户端。
它是 `app/zsvirt/__init__.py` 里 `GpuMetricsProvider` 协议的一个实现依赖 ——
协议（渠道边界）与传输（HTTP + 响应解析）分开放，是因为两者的变化原因完全不同：
协议随产品设计变，传输随 ZSvirt 版本变。

## 依据

命题方第一轮答复确认：

- ZWatch 查询能力**已启用在用**，8 个 GET 接口已实测（**关闭外部阻塞 X-07**）；
- GPU 指标 namespace = `ZStack/Host`，可取 5 个指标：
  `GpuUtilization` / `GpuMemoryUtilization` / `GpuTemperature` / `GpuStatus` / `GpuPowerDraw`；
- 标签含 `HostUuid`、`PciDeviceAddress`，部分序列含 `GpuSerialNumber`；
- `GpuMemoryUtilization` **不应直接当作"已用显存字节数"**。

路由后缀（答复附录）：

| 接口 | 方法 | 路由后缀 |
|---|---|---|
| `GetAllMetricMetadata` | GET | `/zwatch/metrics/meta-data` |
| `GetMetricData` | GET | `/zwatch/metrics` |
| `QueryGpuDevice` | GET | `/zwatch/...`（静态资产，见 `_parse_gpu_devices`） |

## 响应形状的假设只有一个，而且失败时会**喊出来**

答复给到了路由与指标名，但**没有给请求参数名与响应字段名**。适配器必须假定一个形状，
否则一行代码都写不出来。本模块的做法是：

1. 把假设**集中**在 `_first_*` 取值助手与 `_parse_*` 函数里，不散落到业务代码；
2. 假定 ZStack 惯例：时间序列数据点位于 `inventories`，形如
   `[{"metricName": "GpuUtilization", "labels": {...}, "data": [[<秒>, <值>], ...]}]`；
3. 形状不符时抛 `ZWatchSchemaError`，**消息里带上实际拿到的键名**，
   让联调时一眼看出真实字段是什么，而不是静默返回空数据。

第 3 点是有意的取舍：一个"解析不出来就返回空列表"的适配器，在演示现场表现为
"GPU 指标一直是 —"，而排查方向会被误导到权限或网络。**宁可响一声。**
第二轮提问已向命题方索取响应体样例（外部阻塞 X-12），
拿到后只需改 `_parse_metric_data`。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

#: ZWatch 的 GPU 指标（namespace `ZStack/Host`），按命题方答复确认的 5 项。
GPU_METRIC_NAMES: tuple[str, ...] = (
    "GpuUtilization",
    "GpuMemoryUtilization",
    "GpuTemperature",
    "GpuStatus",
    "GpuPowerDraw",
)

#: GPU 指标所在命名空间。
GPU_NAMESPACE = "ZStack/Host"

#: 默认查询窗口（分钟）。答复实测"查询 5 个指标均返回最近 15 分钟数据"。
DEFAULT_WINDOW_MINUTES = 15

#: 同一时刻的多个指标允许的采样偏差（秒）。5 个指标由不同采集器写入，
#: 时间戳不会逐微秒对齐；超过这个偏差就不算同一次快照，避免把 14 分钟前的
#: 利用率和刚刚的显存占用拼成一条读数。
SNAPSHOT_TOLERANCE_SEC = 120


class ZWatchUnavailable(RuntimeError):
    """ZWatch 不可达 / 认证失败 / 未配置。

    与 `GpuMetricsUnavailable` 分开：那个是**渠道边界**的契约异常，
    这个是**传输层**的具体故障。上层把前者转成用户可见的缺口，
    后者用于诊断"到底是没配、连不上、还是认证过期"。
    """


class ZWatchSchemaError(RuntimeError):
    """响应形状与预期不符。

    **不得降级成空数据**：形状不符意味着我们对接口的理解是错的，
    继续跑只会产出一片"看起来正常"的空白。
    """


# ================================================================ 取值助手


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    """取第一个**存在且非 None** 的键。

    不用 `or`：合法的 `0`（"显存占用 0%"是好消息）会被 `or` 当成缺失而跳过 ——
    这正是 `docs/DECISIONS.md` 里反复出现的同一类缺陷。
    """
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _first_str(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _first_present({key: mapping.get(key)}, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_float(mapping: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                continue
    return None


def _to_epoch_seconds(value: Any) -> int | None:
    """时间戳归一化到**秒**。

    答复未说明单位。ZStack 惯例是毫秒 epoch，但也有接口用秒；
    这里按量级判定（> 1e11 视为毫秒），避免把 1.7e12 当成"年份 55834"。
    """
    raw = _first_float({"v": value}, "v")
    if raw is None:
        return None
    if raw > 1e11:  # 毫秒
        return int(raw // 1000)
    return int(raw)


def _classify_metric(value: Any, *, context: str) -> float | None:
    """把非数值型指标读数翻译成数字，或明确拒绝。

    `GpuStatus` 是状态而非数值（ZStack 里可能是 `Connected` / `Disconnected`）。
    猜一个映射表不如**返回 None 并说明** —— 第二轮提问已就此发问。
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            del context
            return None
    return None


# ================================================================ 数据点


@dataclass(frozen=True)
class MetricSample:
    """一个指标在某一时刻的取值。"""

    metric: str
    at: datetime
    value: float
    labels: dict[str, Any] = field(default_factory=dict)

    @property
    def gpu_identity(self) -> str | None:
        """这条样本属于哪块卡。

        优先级：序列号 → PCI 地址 → 宿主 UUID。答复说"部分序列"带序列号，
        因此 PCI 地址必须是**并列**的可用标识，不能只认序列号。
        """
        serial = _first_str(self.labels, "GpuSerialNumber", "gpuSerialNumber", "serialNumber")
        if serial:
            return serial
        pci = _first_str(self.labels, "PciDeviceAddress", "pciDeviceAddress", "pciAddress")
        if pci:
            return pci
        return _first_str(self.labels, "HostUuid", "hostUuid")


@dataclass(frozen=True)
class GpuDeviceInfo:
    """`QueryGpuDevice` 返回的静态资产（答复确认该接口存在且字段为静态信息）。"""

    serial_number: str
    pci_address: str
    model: str
    mem_total_bytes: int
    power_watts: int
    is_driver_loaded: bool
    host_uuid: str | None = None


# ================================================================ 解析


def _parse_metric_data(payload: Any) -> list[MetricSample]:
    """解析 `GetMetricData` 响应。

    预期（单一假设，集中在此）：

    ```json
    {"inventories": [
        {"metricName": "GpuUtilization",
         "labels": {"HostUuid": "...", "PciDeviceAddress": "0000:01:00.0"},
         "data": [[1758456000, 37.5], [1758456030, 41.0]]}
    ]}
    ```

    也接受 `data` 为 `[{"time": ..., "value": ...}]` 的字典形式 ——
    两种写法在 ZStack 各版本里都出现过，接受两种比猜一种更稳。
    """
    root = _as_dict(payload)
    inventories = root.get("inventories")
    if inventories is None:
        raise ZWatchSchemaError(
            "GetMetricData 响应里没有 `inventories`；"
            f"实际顶层键：{sorted(root)}。"
            "适配器的响应形状假设需要按真实样例修正（外部阻塞 X-12）"
        )
    if not isinstance(inventories, list):
        raise ZWatchSchemaError(f"`inventories` 应为列表，实际是 {type(inventories).__name__}")

    samples: list[MetricSample] = []
    for index, entry in enumerate(inventories):
        item = _as_dict(entry)
        if not item:
            raise ZWatchSchemaError(f"inventories[{index}] 不是对象：{entry!r}")

        metric = _first_str(item, "metricName", "metric", "name")
        if not metric:
            raise ZWatchSchemaError(f"inventories[{index}] 缺少指标名；实际键：{sorted(item)}")

        labels = _as_dict(_first_present(item, "labels", "tags"))
        points = item.get("data")
        if points is None:
            # 无采样。**不是 0，也不是空列表**：整条 inventory 缺失 `data`
            # 意味着该指标在该窗口没有数据点，调用方要能区分。
            continue
        if not isinstance(points, list):
            raise ZWatchSchemaError(
                f"inventories[{index}].data 应为列表，实际是 {type(points).__name__}"
            )

        for point_index, point in enumerate(points):
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                stamps, raw_value = point[0], point[1]
            elif isinstance(point, dict):
                stamps = _first_present(point, "time", "timestamp", "at")
                raw_value = _first_present(point, "value", "val")
            else:
                raise ZWatchSchemaError(
                    f"inventories[{index}].data[{point_index}] 形状未知：{point!r}"
                )

            epoch = _to_epoch_seconds(stamps)
            if epoch is None:
                raise ZWatchSchemaError(
                    f"inventories[{index}].data[{point_index}] 的时间戳无法解析：{stamps!r}"
                )

            value = _classify_metric(raw_value, context=f"{metric}[{point_index}]")
            if value is None:
                # `GpuStatus` 等非数值指标：跳过，由上层以"该指标不可用"表达，
                # 不在这里编造映射。
                continue

            samples.append(
                MetricSample(
                    metric=metric,
                    at=datetime.fromtimestamp(epoch, tz=UTC),
                    value=value,
                    labels=labels,
                )
            )

    return samples


def _parse_gpu_devices(payload: Any) -> list[GpuDeviceInfo]:
    """解析 `QueryGpuDevice` 响应（静态资产）。

    预期 `inventories` 内为 `GpuDeviceVO`，字段为 ZSvirt 源码里的驼峰命名。
    缺 `Uuid`/序列号或显存时**跳过该条**而不是报错 —— 资产清单缺一条不影响
    指标采集，而指标才是本渠道的主用途。
    """
    root = _as_dict(payload)
    inventories = root.get("inventories")
    if not isinstance(inventories, list):
        raise ZWatchSchemaError(
            f"QueryGpuDevice 响应里没有列表形式的 `inventories`；实际顶层键：{sorted(root)}"
        )

    devices: list[GpuDeviceInfo] = []
    for entry in inventories:
        item = _as_dict(entry)
        serial = _first_str(item, "SerialNumber", "serialNumber", "Uuid", "uuid")
        if not serial:
            continue
        mem_total = _first_float(item, "MemorySize", "memorySize", "MemTotalBytes", "memTotalBytes")
        if mem_total is None:
            continue
        power = _first_float(item, "PowerCapWatts", "powerCapWatts", "PowerWatts")
        driver = _first_present(item, "IsDriverLoaded", "isDriverLoaded")
        devices.append(
            GpuDeviceInfo(
                serial_number=serial,
                pci_address=_first_str(item, "PciDeviceAddress", "pciDeviceAddress", "PciAddress")
                or "",
                model=_first_str(item, "Model", "model", "Name", "name") or "GPU",
                mem_total_bytes=int(mem_total),
                power_watts=int(power or 0),
                is_driver_loaded=bool(driver) if driver is not None else True,
                host_uuid=_first_str(item, "HostUuid", "hostUuid"),
            )
        )
    return devices


# ================================================================ 传输


class ZWatchTransport(Protocol):
    """HTTP 边界。测试用假实现替换它，不必起服务、不碰网络。"""

    def get(self, path: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        """GET 并返回已解析的 JSON。失败抛 `ZWatchUnavailable`。"""
        ...

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        """POST 并返回已解析的 JSON。失败抛 `ZWatchUnavailable`。"""
        ...


class HttpZWatchTransport:
    """基于 `httpx` 的真实传输。

    **`verify` 默认关闭**：测试环境的控制台是自签证书（内网 IP + 自签），
    开启校验会让适配器在真实环境里必然失败。这是刻意的取舍，且有代价 ——
    因此它作为**显式参数**暴露，而不是埋在代码里的魔法。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        timeout_sec: float = 8.0,
        verify_tls: bool = False,
    ) -> None:
        if not endpoint:
            raise ZWatchUnavailable("ZSVIRT_ENDPOINT 未配置")
        self.endpoint = endpoint.rstrip("/")
        self.timeout_sec = timeout_sec
        self.verify_tls = verify_tls

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> Any:
        url = f"{self.endpoint}{path}"
        try:
            with httpx.Client(timeout=self.timeout_sec, verify=self.verify_tls) as client:
                response = client.request(
                    method, url, params=params or None, json=body, headers=headers
                )
        except httpx.HTTPError as exc:
            raise ZWatchUnavailable(f"{method} {url} 失败：{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise ZWatchUnavailable(
                f"{method} {url} 返回 {response.status_code}：{response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ZWatchUnavailable(f"{method} {url} 响应不是 JSON") from exc

    def get(self, path: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        return self._request("GET", path, params=params, body=None, headers=headers)

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        return self._request("POST", path, params=None, body=body, headers=headers)


# ================================================================ 会话


class ZWatchSession:
    """认证会话 + 自动续期。

    命题方答复第 12 条：「本次实际登录返回的**会话创建时间与到期时间相差约 2 小时**，
    因此 API 会话并非永久有效，过期后需重新认证。」

    所以这里不做"启动时取一次 token 用到底" —— 那在演示会中途失效，
    而且是那种"前 90 分钟一切正常"的失效，最难现场处理。

    凭据注入方式（`ZSVIRT_AUTH_STYLE`）：

    | 取值 | 用法 |
    |---|---|
    | `oauth` | `ZSVIRT_AUTH_TOKEN` 直接作为 `Authorization: OAuth <token>` |
    | `accesskey` | `ZSVIRT_ACCESS_KEY` + `ZSVIRT_SECRET_KEY`，POST 登录端点换取会话 |

    认证头的确切形式命题方未给死（第二轮提问 §3 已发问），因此两种都支持：
    真机联调时改一个环境变量即可，不必改代码。
    """

    #: 会话有效期按答复的"约 2 小时"取值，并**提前 5 分钟**续期。
    DEFAULT_TTL_SEC = 2 * 60 * 60
    REFRESH_MARGIN_SEC = 5 * 60

    def __init__(
        self,
        *,
        transport: ZWatchTransport | Callable[[], ZWatchTransport],
        style: str = "oauth",
        token: str = "",
        access_key: str = "",
        secret_key: str = "",
        login_path: str = "/zstack/v1/accounts/login",
        ttl_sec: int | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        # 传输可以是实例，也可以是**惰性工厂**。工厂的意义：未配置 endpoint 的
        # 机器上，"检查凭据是否齐全"（零 I/O）不应触发构造真实 HTTP 客户端。
        # 早先存实例，于是读一下 `session` 就把 HttpZWatchTransport 建起来了，
        # 它在 endpoint 为空时抛异常 —— 结果 `is_available()` 从"返回 False"
        # 变成"抛异常"，而降级路径依赖前者。
        self._transport_source = transport
        self.style = style
        self.token = token
        self.access_key = access_key
        self.secret_key = secret_key
        self.login_path = login_path
        self.ttl_sec = ttl_sec if ttl_sec is not None else self.DEFAULT_TTL_SEC
        self._clock = clock
        self._expires_at: float | None = None

    @property
    def transport(self) -> ZWatchTransport:
        if callable(self._transport_source):
            self._transport_source = self._transport_source()
        return self._transport_source

    @property
    def configured(self) -> bool:
        """凭据是否齐全。**不做网络调用** —— 供健康检查快速判定。"""
        if self.style == "accesskey":
            return bool(self.access_key and self.secret_key)
        return bool(self.token)

    def headers(self) -> dict[str, str]:
        """取当前有效的认证头，必要时先登录。"""
        if not self.configured:
            raise ZWatchUnavailable(
                "ZSvirt 凭据未配置：请设置 ZSVIRT_AUTH_TOKEN，"
                "或 ZSVIRT_ACCESS_KEY + ZSVIRT_SECRET_KEY（见 docs/DEPLOYMENT.md §4）"
            )
        if self.style == "accesskey" and self._needs_login():
            self.login()
        return {"Authorization": f"OAuth {self.token}"}

    def _needs_login(self) -> bool:
        if self._expires_at is None:
            return True
        return self._clock() >= self._expires_at - self.REFRESH_MARGIN_SEC

    def login(self) -> None:
        """用 AccessKey/SecretKey 换取会话 token。"""
        payload = self.transport.post(
            self.login_path,
            {
                "logInByAccount": {
                    "accountName": self.access_key,
                    "password": self.secret_key,
                }
            },
            {},
        )
        root = _as_dict(payload)
        # ZStack 登录响应：会话 id 在顶层 `session`（或 `data.session`）。
        session = _as_dict(_first_present(root, "session", "Session"))
        if not session:
            session = _as_dict(_as_dict(root.get("data")).get("session"))
        session_id = _first_str(session, "uuid", "Uuid", "sessionId")
        if not session_id:
            raise ZWatchSchemaError(
                "登录响应里取不到会话 id；实际顶层键："
                f"{sorted(root)}，session 键：{sorted(session)}"
            )
        self.token = session_id

        # 优先用服务端给的到期时间，取不到才退回本地 TTL 估算。
        expire_at = _first_float(session, "expiredDate", "expireTime", "expiredTime")
        if expire_at is not None:
            epoch = _to_epoch_seconds(expire_at)
            remaining = 0 if epoch is None else max(0, epoch - int(datetime.now(UTC).timestamp()))
            self.ttl_sec = remaining or self.DEFAULT_TTL_SEC
        self._expires_at = self._clock() + self.ttl_sec

    def invalidate(self) -> None:
        """标记会话失效，下次取头时强制重新登录。

        服务端返回 401/403 时调用 —— 服务端提前作废会话是可能的，
        而本地 TTL 完全看不出来。
        """
        self._expires_at = None
        if self.style == "accesskey":
            self.token = ""


# ================================================================ 客户端


@dataclass(frozen=True)
class ZWatchConfig:
    """`ZWatchClient` 的构造参数集合。"""

    endpoint: str = ""
    auth_style: str = "oauth"
    auth_token: str = ""
    access_key: str = ""
    secret_key: str = ""
    timeout_sec: float = 8.0
    verify_tls: bool = False
    window_minutes: int = DEFAULT_WINDOW_MINUTES
    metadata_cache_sec: int = 300


class ZWatchClient:
    """ZWatch 指标 API 的客户端。

    `metadata_cache_sec` 是必要的：前端健康检查每 3 秒轮询一次，
    而元数据几乎不变。没有缓存的话，一个页面就能把管理节点的
    `/zwatch/metrics/meta-data` 打成每 3 秒一次。
    """

    def __init__(
        self,
        *,
        config: ZWatchConfig | None = None,
        transport: ZWatchTransport | None = None,
        session: ZWatchSession | None = None,
    ) -> None:
        self.config = config or ZWatchConfig()
        # **传输必须惰性创建。** 早先在 `__init__` 里直接构造
        # `HttpZWatchTransport`，而它在 `endpoint` 为空时抛异常 —— 于是
        # 一台没配 ZSVIRT_ENDPOINT 的机器上 `ZWatchProvider()` 直接构造失败，
        # `resolve_provider()` 的降级路径跟着崩。未配置应该是"报不可用"，
        # 不是"构造不出来"。
        self._transport = transport
        self._session = session
        self._metric_metadata: list[dict[str, Any]] | None = None
        self._metadata_at: float = 0.0

    @property
    def transport(self) -> ZWatchTransport:
        if self._transport is None:
            self._transport = HttpZWatchTransport(
                endpoint=self.config.endpoint,
                timeout_sec=self.config.timeout_sec,
                verify_tls=self.config.verify_tls,
            )
        return self._transport

    @property
    def session(self) -> ZWatchSession:
        if self._session is None:
            self._session = ZWatchSession(
                transport=lambda: self.transport,
                style=self.config.auth_style,
                token=self.config.auth_token,
                access_key=self.config.access_key,
                secret_key=self.config.secret_key,
            )
        return self._session

    # ---- 元数据 ----

    def metric_metadata(self, *, force: bool = False) -> list[dict[str, Any]]:
        """`GetAllMetricMetadata`，带 TTL 缓存。"""
        now = time.monotonic()
        if (
            not force
            and self._metric_metadata is not None
            and now - self._metadata_at < self.config.metadata_cache_sec
        ):
            return self._metric_metadata

        payload = self._get("/zwatch/metrics/meta-data", {"namespace": GPU_NAMESPACE})
        root = _as_dict(payload)
        inventories = root.get("inventories")
        if inventories is None:
            raise ZWatchSchemaError(
                f"GetAllMetricMetadata 响应里没有 `inventories`；实际顶层键：{sorted(root)}"
            )
        if not isinstance(inventories, list):
            raise ZWatchSchemaError(f"`inventories` 应为列表，实际是 {type(inventories).__name__}")
        self._metric_metadata = [_as_dict(x) for x in inventories]
        self._metadata_at = now
        return self._metric_metadata

    def available_gpu_metrics(self) -> list[str]:
        """环境里**实际可用**的 GPU 指标名。

        与 `GPU_METRIC_NAMES`（我们想要的）区分开：拿环境里有什么去问，
        比把 5 个名字都发过去、等其中 2 个报错要好。
        """
        found: list[str] = []
        for entry in self.metric_metadata():
            name = _first_str(entry, "name", "metricName", "Name")
            if name and name not in found:
                found.append(name)
        return found

    # ---- 指标 ----

    def fetch_samples(
        self,
        *,
        metrics: tuple[str, ...] = GPU_METRIC_NAMES,
        at: datetime | None = None,
        window_minutes: int | None = None,
        namespace: str = GPU_NAMESPACE,
    ) -> list[MetricSample]:
        """取一条时间窗内的全部指标样本。

        一次请求查多个指标（`metricName` 重复参数），而不是每个指标一次 ——
        5 个指标一次往返，对轮询友好。
        """
        moment = at or datetime.now(UTC)
        window = window_minutes if window_minutes is not None else self.config.window_minutes
        start = moment - timedelta(minutes=window)

        params: list[tuple[str, Any]] = [("namespace", namespace)]
        params.extend(("metricName", name) for name in metrics)
        params.append(("startTime", int(start.timestamp())))
        params.append(("endTime", int(moment.timestamp())))

        payload = self._get("/zwatch/metrics", params)
        return _parse_metric_data(payload)

    # ---- 静态资产 ----

    def fetch_gpu_devices(self) -> list[GpuDeviceInfo]:
        """`QueryGpuDevice` 静态资产。取不到时返回空列表。

        **失败不抛**：资产清单只是补充（显存总容量、型号），指标才是主用途。
        因为资产接口报错就让整个渠道不可用，是把补充信息当成了必需信息。
        """
        try:
            payload = self._get("/zstack/v1/gpu-devices", {})
        except (ZWatchUnavailable, ZWatchSchemaError):
            return []
        try:
            return _parse_gpu_devices(payload)
        except ZWatchSchemaError:
            return []

    # ---- 探活 ----

    def ping(self) -> bool:
        """真实探活：**不做**缓存，用于判断渠道此刻是否可用。"""
        try:
            self.metric_metadata(force=True)
        except (ZWatchUnavailable, ZWatchSchemaError):
            return False
        return True

    def _get(self, path: str, params: Any) -> Any:
        headers = self.session.headers()
        try:
            return self.transport.get(path, params, headers)
        except ZWatchUnavailable as exc:
            # 401/403 很可能是本地 TTL 还没到、但服务端已作废会话。
            # 作废本地会话并**重试一次** —— 只重试一次，避免凭据错误时打转。
            if "401" not in str(exc) and "403" not in str(exc):
                raise
            self.session.invalidate()
            return self.transport.get(path, params, self.session.headers())


# ================================================================ 快照


@dataclass(frozen=True)
class CardSnapshot:
    """一块卡在某一时刻的指标快照（由 5 个指标拼成一条读数）。"""

    gpu_identity: str
    at: datetime
    utilization_pct: float | None
    mem_usage_pct: float | None
    temperature_c: float | None
    power_watts: float | None
    labels: dict[str, Any] = field(default_factory=dict)

    @property
    def gpu_serial(self) -> str | None:
        return _first_str(self.labels, "GpuSerialNumber", "gpuSerialNumber", "serialNumber")

    @property
    def pci_address(self) -> str | None:
        return _first_str(self.labels, "PciDeviceAddress", "pciDeviceAddress", "pciAddress")

    @property
    def host_uuid(self) -> str | None:
        return _first_str(self.labels, "HostUuid", "hostUuid")


def latest_snapshot_per_gpu(
    samples: list[MetricSample],
    *,
    tolerance_sec: int = SNAPSHOT_TOLERANCE_SEC,
) -> list[CardSnapshot]:
    """把零散样本按键（卡）× 指标的最新值拼成快照列表。

    每块卡取**该卡所有指标里最新的时间戳**为基准，再把与它相差不超过
    `tolerance_sec` 的样本纳进来。这样即使某个指标晚到几十秒，也不会
    让整条读数变成"一半是旧值"。
    """
    by_gpu: dict[str, list[MetricSample]] = {}
    for sample in samples:
        identity = sample.gpu_identity
        if identity:
            by_gpu.setdefault(identity, []).append(sample)

    snapshots: list[CardSnapshot] = []
    for identity, gpu_samples in sorted(by_gpu.items()):
        newest = max(s.at for s in gpu_samples)
        recent = [s for s in gpu_samples if (newest - s.at).total_seconds() <= tolerance_sec]

        chosen: dict[str, MetricSample] = {}
        for sample in sorted(recent, key=lambda s: s.at):
            chosen[sample.metric] = sample  # 同指标取最新的一个

        labels: dict[str, Any] = {}
        for sample in chosen.values():
            labels.update(sample.labels)

        snapshots.append(
            CardSnapshot(
                gpu_identity=identity,
                at=newest,
                utilization_pct=chosen["GpuUtilization"].value
                if "GpuUtilization" in chosen
                else None,
                mem_usage_pct=chosen["GpuMemoryUtilization"].value
                if "GpuMemoryUtilization" in chosen
                else None,
                temperature_c=chosen["GpuTemperature"].value
                if "GpuTemperature" in chosen
                else None,
                power_watts=chosen["GpuPowerDraw"].value if "GpuPowerDraw" in chosen else None,
                labels=labels,
            )
        )
    return snapshots


__all__ = [
    "DEFAULT_WINDOW_MINUTES",
    "GPU_METRIC_NAMES",
    "GPU_NAMESPACE",
    "SNAPSHOT_TOLERANCE_SEC",
    "CardSnapshot",
    "GpuDeviceInfo",
    "HttpZWatchTransport",
    "MetricSample",
    "ZWatchClient",
    "ZWatchConfig",
    "ZWatchSchemaError",
    "ZWatchSession",
    "ZWatchTransport",
    "ZWatchUnavailable",
    "latest_snapshot_per_gpu",
]
