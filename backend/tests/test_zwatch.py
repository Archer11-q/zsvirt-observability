"""ZWatch 指标渠道测试（命题方答复关闭 X-07 后的真实适配）。

测试不需要网络、不需要 ZSvirt 环境：HTTP 边界是 `ZWatchTransport` 协议，
下面用 `FakeTransport` 替换它。这样测的是**我们的映射逻辑**，
而真实环境的联调留给集成阶段 —— 两者不该混在同一个测试里。

重点覆盖四件事，每件都对应一个会静默出错的地方：

1. **读不到 ≠ 数值 0**（命题方答复第 03 条点名）。把"读不到"当"占用 0%"
   在诊断上会被当成好消息，是最坏的一类错误。
2. **使用率 ≠ 字节数**（答复第 07 条原话："不应直接写成'已用显存字节数'"）。
   从百分比反推的字节数不是观测值，`mem_used_bytes` 必须保持 None。
3. **直通 ≠ vGPU 切分**（答复第 05 条：平台未发现 MDEV 实例）。
   直通下没有"本机占了多少"，归因口径必须如实标 `passthrough`。
4. **形状不符要喊出来**，不能静默返回空数据。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.zsvirt import GpuMetricsUnavailable, ZWatchProvider
from app.zsvirt.watch import (
    GPU_METRIC_NAMES,
    GPU_NAMESPACE,
    CardSnapshot,
    HttpZWatchTransport,
    ZWatchClient,
    ZWatchConfig,
    ZWatchSchemaError,
    ZWatchSession,
    ZWatchTransport,
    ZWatchUnavailable,
    _parse_metric_data,
    latest_snapshot_per_gpu,
)

BASE = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
SERIAL = "GPU-2f1a9c10-4b7e-4d21"


# ================================================================ 测试替身


class FakeTransport:
    """记录请求并回放预设响应的假传输。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, dict[str, str]]] = []
        self.get_responses: dict[str, Any] = {}
        self.post_responses: dict[str, Any] = {}
        self.raise_on: dict[str, Exception] = {}

    def get(self, path: str, params: Any, headers: dict[str, str]) -> Any:
        self.calls.append(("GET", path, params, headers))
        if path in self.raise_on:
            raise self.raise_on[path]
        if path not in self.get_responses:
            raise ZWatchUnavailable(f"no fake response for GET {path}")
        return self.get_responses[path]

    def post(self, path: str, body: Any, headers: dict[str, str]) -> Any:
        self.calls.append(("POST", path, body, headers))
        if path in self.raise_on:
            raise self.raise_on[path]
        if path not in self.post_responses:
            raise ZWatchUnavailable(f"no fake response for POST {path}")
        return self.post_responses[path]

    def paths(self) -> list[str]:
        return [c[1] for c in self.calls]

    def params_for(self, path: str) -> dict[str, Any]:
        for method, called_path, params, _headers in self.calls:
            if method == "GET" and called_path == path:
                return dict(params)
        raise AssertionError(f"{path} was never called")


def metric_payload(points: dict[str, list[Any]]) -> dict[str, Any]:
    """构造 GetMetricData 响应。空列表代表"该指标无采样"。"""
    return {
        "inventories": [
            {
                "metricName": name,
                "labels": {
                    "HostUuid": "host-uuid-1",
                    "PciDeviceAddress": "0000:01:00.0",
                    "GpuSerialNumber": SERIAL,
                },
                "data": data,
            }
            for name, data in points.items()
        ]
    }


def epoch(offset_sec: int = 0) -> int:
    return int((BASE + timedelta(seconds=offset_sec)).timestamp())


def healthy_payload(at_offset: int = 0) -> dict[str, Any]:
    return metric_payload(
        {
            "GpuUtilization": [[epoch(at_offset), 37.5]],
            "GpuMemoryUtilization": [[epoch(at_offset), 41.2]],
            "GpuTemperature": [[epoch(at_offset), 62.0]],
            "GpuStatus": [[epoch(at_offset), "Connected"]],
            "GpuPowerDraw": [[epoch(at_offset), 118.0]],
        }
    )


def healthy_reading(**overrides: Any) -> Any:
    """一条**带标签**的 ZWatch 读数。

    `labels` 是关键：平台层 ID 桥接（`app/zsvirt/harvest.py`）靠
    `HostUuid` / `PciDeviceAddress` / `GpuSerialNumber` 拼 `{kind}:zsvirt:{uuid}`。
    """
    from app.zsvirt import GpuMetricReading

    defaults: dict[str, Any] = {
        "sampled_at": BASE,
        "origin": "zsvirt-zwatch",
        "gpu_serial": SERIAL,
        "host_mem_usage_pct": 41.2,
        "self_vgpu_mem_usage_pct": 41.2,
        "utilization_pct": 37.5,
        "mem_used_bytes": None,
        "mem_total_bytes": 24576 * 1024 * 1024,
        "temperature_c": 62.0,
        "quota_bytes": None,
        "allocated_bytes": None,
        "attribution": "passthrough",
        "labels": {
            "HostUuid": "host-uuid-1",
            "PciDeviceAddress": "0000:01:00.0",
            "GpuSerialNumber": SERIAL,
        },
    }
    defaults.update(overrides)
    return GpuMetricReading(**defaults)


def _client(transport: FakeTransport, *, token: str = "tok", **config_kwargs: Any) -> ZWatchClient:
    return ZWatchClient(
        config=ZWatchConfig(endpoint="https://10.0.0.1", **config_kwargs),
        transport=transport,
        session=ZWatchSession(transport=transport, style="oauth", token=token),
    )


def make_provider(transport: FakeTransport, *, token: str = "tok", **kwargs: Any) -> ZWatchProvider:
    """造一个走假传输的 provider（凭据齐全是默认值 —— 否则探活必然失败）。

    `**kwargs` 只喂给 `ZWatchProvider` 自己（`cache_ttl_sec` / `gpu_selector`），
    不喂给 `ZWatchConfig` —— 后者不认识它们。
    """
    kwargs.setdefault("endpoint", "https://10.0.0.1")
    return ZWatchProvider(client=_client(transport, token=token), **kwargs)


# ================================================================ 解析


class TestParseMetricData:
    def test_parses_the_documented_shape(self) -> None:
        """5 个指标都在响应里，其中 4 个是数值型 ⇒ 解析出 4 条样本。

        `GpuStatus` 是状态字符串，按设计跳过（见下一条测试）。
        """
        samples = _parse_metric_data(healthy_payload())
        assert {s.metric for s in samples} == {
            "GpuUtilization",
            "GpuMemoryUtilization",
            "GpuTemperature",
            "GpuPowerDraw",
        }
        assert all(s.at == BASE for s in samples)

    def test_gpu_status_string_is_not_silently_zero(self) -> None:
        """`GpuStatus` 是状态不是数值。解析不出来就**跳过**，不猜成 0。

        猜成 0 会让"卡状态未知"看起来像"卡状态正常值 0"，正是答复第 03 条
        警告的那类混淆。
        """
        samples = _parse_metric_data(healthy_payload())
        assert "GpuStatus" not in {s.metric for s in samples}

    def test_missing_data_key_means_no_samples_not_zero(self) -> None:
        """`data` 缺失 ⇒ 该指标**零个样本**，不是"值为 0"。"""
        payload = metric_payload({"GpuUtilization": []})
        del payload["inventories"][0]["data"]
        assert _parse_metric_data(payload) == []

    def test_explicit_zero_is_preserved(self) -> None:
        """**回归测试**：合法的 0 必须留住 —— "显存占用 0%"是好消息。"""
        samples = _parse_metric_data(metric_payload({"GpuMemoryUtilization": [[epoch(), 0]]}))
        assert len(samples) == 1
        assert samples[0].value == 0.0

    def test_millisecond_timestamps_are_normalized(self) -> None:
        """答复未给时间戳单位。毫秒 epoch 必须归一化到秒，否则年份会变成 55834。"""
        ms = epoch() * 1000
        samples = _parse_metric_data(metric_payload({"GpuUtilization": [[ms, 10.0]]}))
        assert samples[0].at == BASE

    def test_dict_shaped_points_are_accepted(self) -> None:
        """`[{"time":..,"value":..}]` 形式在 ZStack 各版本都出现过，接受两种。"""
        payload = metric_payload({"GpuUtilization": [{"time": epoch(), "value": 12.5}]})
        samples = _parse_metric_data(payload)
        assert samples[0].value == 12.5

    def test_missing_inventories_raises_with_actual_keys(self) -> None:
        """形状不符必须**喊出来**，且消息里带上实际键名，便于联调定位。"""
        with pytest.raises(ZWatchSchemaError) as info:
            _parse_metric_data({"result": {"data": []}})
        assert "inventories" in str(info.value)
        assert "result" in str(info.value), "错误消息必须包含实际顶层键"

    def test_data_not_a_list_raises(self) -> None:
        with pytest.raises(ZWatchSchemaError, match="应为列表"):
            _parse_metric_data({"inventories": [{"metricName": "GpuUtilization", "data": "oops"}]})

    def test_entry_without_metric_name_raises(self) -> None:
        with pytest.raises(ZWatchSchemaError, match="缺少指标名"):
            _parse_metric_data({"inventories": [{"data": []}]})

    def test_bad_timestamp_raises_rather_than_guessing(self) -> None:
        payload = metric_payload({"GpuUtilization": [["not-a-time", 1.0]]})
        with pytest.raises(ZWatchSchemaError, match="时间戳无法解析"):
            _parse_metric_data(payload)

    def test_inventories_not_a_list_raises(self) -> None:
        with pytest.raises(ZWatchSchemaError, match="应为列表"):
            _parse_metric_data({"inventories": {"a": 1}})


# ================================================================ 快照拼装


class TestLatestSnapshotPerGpu:
    def test_groups_by_serial_and_picks_newest(self) -> None:
        samples = _parse_metric_data(
            metric_payload(
                {
                    "GpuUtilization": [[epoch(0), 10.0], [epoch(30), 20.0]],
                    "GpuMemoryUtilization": [[epoch(30), 30.0]],
                }
            )
        )
        snapshots = latest_snapshot_per_gpu(samples)
        assert len(snapshots) == 1
        assert snapshots[0].utilization_pct == 20.0, "应取最新那一条"
        assert snapshots[0].at == BASE + timedelta(seconds=30)

    def test_stale_sample_outside_tolerance_is_excluded(self) -> None:
        """14 分钟前的利用率不能和刚刚的显存占用拼成一条读数。

        拼出来的数字**看不出来不对**，但会把一次真实的显存告急讲成历史故事。
        """
        samples = _parse_metric_data(
            metric_payload(
                {
                    "GpuUtilization": [[epoch(0), 99.0]],
                    "GpuMemoryUtilization": [[epoch(600), 10.0]],
                }
            )
        )
        snapshots = latest_snapshot_per_gpu(samples)
        assert snapshots[0].mem_usage_pct == 10.0
        assert snapshots[0].utilization_pct is None, "超出容差的旧样本必须被排除"

    def test_two_cards_are_kept_apart(self) -> None:
        payload = {
            "inventories": [
                {
                    "metricName": "GpuUtilization",
                    "labels": {"GpuSerialNumber": "GPU-A"},
                    "data": [[epoch(), 11.0]],
                },
                {
                    "metricName": "GpuUtilization",
                    "labels": {"GpuSerialNumber": "GPU-B"},
                    "data": [[epoch(), 22.0]],
                },
            ]
        }
        snapshots = latest_snapshot_per_gpu(_parse_metric_data(payload))
        assert [s.gpu_identity for s in snapshots] == ["GPU-A", "GPU-B"]

    def test_falls_back_to_pci_address_when_serial_missing(self) -> None:
        """答复说"**部分**序列带序列号" ⇒ PCI 地址必须是并列可用的标识。"""
        payload = {
            "inventories": [
                {
                    "metricName": "GpuUtilization",
                    "labels": {"PciDeviceAddress": "0000:02:00.0"},
                    "data": [[epoch(), 5.0]],
                }
            ]
        }
        snapshots = latest_snapshot_per_gpu(_parse_metric_data(payload))
        assert snapshots[0].gpu_identity == "0000:02:00.0"


# ================================================================ 会话


class TestZWatchSession:
    def test_not_configured_without_credentials(self) -> None:
        session = ZWatchSession(transport=FakeTransport(), style="oauth", token="")
        assert session.configured is False
        with pytest.raises(ZWatchUnavailable, match="凭据未配置"):
            session.headers()

    def test_oauth_token_is_used_directly(self) -> None:
        transport = FakeTransport()
        session = ZWatchSession(transport=transport, style="oauth", token="tok-1")
        assert session.headers() == {"Authorization": "OAuth tok-1"}
        assert transport.calls == [], "静态 token 不应触发登录调用"

    def test_accesskey_logs_in_and_caches(self) -> None:
        transport = FakeTransport()
        transport.post_responses["/zstack/v1/accounts/login"] = {
            "session": {"uuid": "sess-1", "expiredDate": epoch(7200) * 1000}
        }
        session = ZWatchSession(
            transport=transport, style="accesskey", access_key="ak", secret_key="sk"
        )
        assert session.headers() == {"Authorization": "OAuth sess-1"}
        assert session.headers() == {"Authorization": "OAuth sess-1"}
        assert transport.paths().count("/zstack/v1/accounts/login") == 1, "未过期不应重复登录"

    def test_session_is_renewed_before_expiry(self) -> None:
        """答复第 12 条："会话并非永久有效，过期后需重新认证"。

        用假时钟推进到"距到期不足 5 分钟"，下一次取头必须**主动续期** ——
        一个"启动时取一次 token 用到底"的实现会在演示中途失效。
        """
        transport = FakeTransport()
        transport.post_responses["/zstack/v1/accounts/login"] = {"session": {"uuid": "sess-1"}}

        now = [0.0]
        session = ZWatchSession(
            transport=transport,
            style="accesskey",
            access_key="ak",
            secret_key="sk",
            ttl_sec=7200,
            clock=lambda: now[0],
        )
        session.headers()
        now[0] = 7200 - 10  # 距到期只剩 10 秒，已进入提前续期窗口
        session.headers()
        assert transport.paths().count("/zstack/v1/accounts/login") == 2

    def test_login_without_session_id_raises_with_keys(self) -> None:
        transport = FakeTransport()
        transport.post_responses["/zstack/v1/accounts/login"] = {"unexpected": 1}
        session = ZWatchSession(
            transport=transport, style="accesskey", access_key="ak", secret_key="sk"
        )
        with pytest.raises(ZWatchSchemaError) as info:
            session.headers()
        assert "unexpected" in str(info.value)

    def test_invalidate_forces_relogin(self) -> None:
        transport = FakeTransport()
        transport.post_responses["/zstack/v1/accounts/login"] = {"session": {"uuid": "s"}}
        session = ZWatchSession(
            transport=transport, style="accesskey", access_key="ak", secret_key="sk"
        )
        session.headers()
        session.invalidate()
        session.headers()
        assert transport.paths().count("/zstack/v1/accounts/login") == 2


# ================================================================ 客户端


class TestZWatchClient:
    def test_queries_all_five_metrics_in_one_request(self) -> None:
        """5 个指标一次往返 —— 前端 15 秒轮询不能变成 5 次请求。"""
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        _client(transport).fetch_samples(at=BASE)

        assert transport.paths() == ["/zwatch/metrics"]
        # `metricName` 是重复参数，dict() 会把它折叠，所以核对原始元组。
        raw = [c[2] for c in transport.calls if c[1] == "/zwatch/metrics"][0]
        assert [v for k, v in raw if k == "metricName"] == list(GPU_METRIC_NAMES)

    def test_namespace_is_zstack_host(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        _client(transport).fetch_samples(at=BASE)
        assert ("namespace", GPU_NAMESPACE) in transport.params_for("/zwatch/metrics").items()

    def test_window_bounds_are_sent(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        _client(transport, window_minutes=15).fetch_samples(at=BASE)
        values = transport.params_for("/zwatch/metrics")
        assert values["endTime"] == int(BASE.timestamp())
        assert values["startTime"] == int((BASE - timedelta(minutes=15)).timestamp())

    def test_metadata_is_cached(self) -> None:
        """前端健康检查每 3 秒轮询；没有缓存会把管理节点打成每 3 秒一次。"""
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics/meta-data"] = {
            "inventories": [{"name": "GpuUtilization", "labels": []}]
        }
        client = _client(transport)
        client.metric_metadata()
        client.metric_metadata()
        assert transport.paths().count("/zwatch/metrics/meta-data") == 1

    def test_metadata_force_bypasses_cache(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics/meta-data"] = {"inventories": []}
        client = _client(transport)
        client.metric_metadata()
        client.metric_metadata(force=True)
        assert transport.paths().count("/zwatch/metrics/meta-data") == 2

    def test_available_gpu_metrics_reports_what_the_environment_has(self) -> None:
        """拿"环境里有什么"去问，比把 5 个名字都发过去等其中 2 个报错要好。"""
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics/meta-data"] = {
            "inventories": [
                {"name": "GpuUtilization"},
                {"name": "GpuMemoryUtilization"},
                {"name": "GpuUtilization"},
            ]
        }
        assert _client(transport).available_gpu_metrics() == [
            "GpuUtilization",
            "GpuMemoryUtilization",
        ]

    def test_ping_returns_false_instead_of_raising(self) -> None:
        """探活不该崩（协议要求）。"""
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics/meta-data"] = {"wrong": "shape"}
        assert _client(transport).ping() is False

    def test_401_triggers_one_retry_after_invalidating(self) -> None:
        """服务端提前作废会话是可能的，而本地 TTL 看不出来。"""

        class Flaky(FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.n = 0

            def get(self, path: str, params: Any, headers: dict[str, str]) -> Any:
                self.n += 1
                if self.n == 1:
                    self.calls.append(("GET", path, params, headers))
                    raise ZWatchUnavailable("GET ... 返回 401：unauthorized")
                return super().get(path, params, headers)

        transport = Flaky()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        samples = _client(transport).fetch_samples(at=BASE)
        assert samples, "重试后应当成功"
        assert transport.n == 2, "只重试一次，避免凭据错误时打转"

    def test_gpu_device_failure_does_not_break_metrics(self) -> None:
        """资产清单是补充信息，指标才是主用途 —— 不能因资产接口报错而整个渠道不可用。"""
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        transport.raise_on["/zstack/v1/gpu-devices"] = ZWatchUnavailable("404")
        assert _client(transport).fetch_gpu_devices() == []


# ================================================================ Provider


class TestZWatchProvider:
    def test_not_available_without_credentials(self) -> None:
        """未配置凭据时必须报**不可用**，且零网络调用。"""
        transport = FakeTransport()
        provider = make_provider(transport, token="")
        assert provider.is_available() is False
        assert transport.calls == [], "未配置凭据时不应发请求"

    def test_available_when_credentials_and_probe_succeed(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics/meta-data"] = {"inventories": []}
        assert make_provider(transport).is_available() is True

    def test_unavailable_when_probe_fails(self) -> None:
        transport = FakeTransport()
        transport.raise_on["/zwatch/metrics/meta-data"] = ZWatchUnavailable("connect timeout")
        assert make_provider(transport).is_available() is False

    def test_read_raises_instead_of_returning_zeros(self) -> None:
        """**核心不变量**：读不到必须抛错。

        返回 0 会被上层当成"显存占用 0%"这个**好消息** —— 界面看起来一切正常，
        而实际是读不到。这是答复第 03 条点名的那类混淆。
        """
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = metric_payload({"GpuUtilization": []})
        with pytest.raises(GpuMetricsUnavailable, match="没有返回任何 GPU 指标样本"):
            make_provider(transport).read()

    def test_read_raises_when_only_non_numeric_metrics_present(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = metric_payload(
            {"GpuStatus": [[epoch(), "Connected"]]}
        )
        with pytest.raises(GpuMetricsUnavailable):
            make_provider(transport).read()

    def test_reading_reports_passthrough_attribution(self) -> None:
        """命题方确认当前是 GPU 直通；平台侧**没有**按虚拟机维度的占用。

        因此卡级使用率同时代表宿主与本机，且 `can_attribute=False` ——
        "自己超配 vs 邻居干扰"在直通下**不可得**，不能靠调权重假装可得。
        """
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        reading = make_provider(transport).read()

        assert reading.origin == "zsvirt-zwatch"
        assert reading.attribution == "passthrough"
        assert reading.can_attribute is False
        assert reading.host_mem_usage_pct == 41.2
        assert reading.self_vgpu_mem_usage_pct == reading.host_mem_usage_pct, (
            "直通模式下两个字段是同一个量，不能编一个'本机占用'出来"
        )
        assert reading.quota_bytes is None
        assert reading.allocated_bytes is None

    def test_reading_does_not_invent_byte_counts(self) -> None:
        """答复原话："不应直接写成'已用显存字节数'"。

        从 41.2% × 24GiB 反推出来的数字**不是观测值**，进证据链会让
        "可复算置信度"变成一句空话。
        """
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        assert make_provider(transport).read().mem_used_bytes is None

    def test_temperature_and_utilization_are_mapped(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        reading = make_provider(transport).read()
        assert reading.utilization_pct == 37.5
        assert reading.temperature_c == 62.0

    def test_temperature_may_be_absent(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = metric_payload(
            {"GpuUtilization": [[epoch(), 10.0]], "GpuMemoryUtilization": [[epoch(), 20.0]]}
        )
        assert make_provider(transport).read().temperature_c is None

    def test_readings_are_cached(self) -> None:
        """缓存挡住同一请求内的重复查询。"""
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        provider = make_provider(transport, cache_ttl_sec=60)
        provider.read()
        provider.read()
        assert transport.paths().count("/zwatch/metrics") == 1

    def test_cache_can_be_disabled(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        provider = make_provider(transport, cache_ttl_sec=0)
        provider.read()
        provider.read()
        assert transport.paths().count("/zwatch/metrics") == 2, "ttl=0 时每次都该重新查"

    def test_empty_result_is_not_cached(self) -> None:
        """一次"恰好没有采样"的查询不能把渠道锁在空状态里。"""
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = metric_payload({"GpuUtilization": []})
        provider = make_provider(transport, cache_ttl_sec=60)
        for _ in range(2):
            with pytest.raises(GpuMetricsUnavailable):
                provider.read()
        assert transport.paths().count("/zwatch/metrics") == 2

    def test_assets_prefer_query_gpu_device(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zstack/v1/gpu-devices"] = {
            "inventories": [
                {
                    "SerialNumber": SERIAL,
                    "PciDeviceAddress": "0000:01:00.0",
                    "Model": "NVIDIA Quadro RTX 6000",
                    "MemorySize": 24576 * 1024 * 1024,
                    "PowerCapWatts": 260,
                    "IsDriverLoaded": True,
                }
            ]
        }
        asset = make_provider(transport).assets()[0]
        assert asset.serial_number == SERIAL
        assert asset.model == "NVIDIA Quadro RTX 6000"
        assert asset.mem_total_bytes == 24576 * 1024 * 1024, "应取资产接口的权威容量"
        assert asset.power_watts == 260

    def test_assets_fall_back_to_metric_labels_without_inventing_capacity(self) -> None:
        """资产接口不可用时用标签兜底，但**容量报 0 而不是猜一个数**。

        猜出来的容量会让"显存耗尽"的阈值失去意义。
        """
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        transport.get_responses["/zstack/v1/gpu-devices"] = {"inventories": []}
        asset = make_provider(transport).assets()[0]
        assert asset.serial_number == SERIAL
        assert asset.mem_total_bytes == 0
        assert "unknown" in asset.model

    def test_assets_raise_when_nothing_is_readable(self) -> None:
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = metric_payload({"GpuUtilization": []})
        transport.get_responses["/zstack/v1/gpu-devices"] = {"inventories": []}
        with pytest.raises(GpuMetricsUnavailable, match="读不到任何 GPU 指标样本"):
            make_provider(transport).assets()

    def test_describe_exposes_the_passthrough_limitation(self) -> None:
        """缺口要暴露出来，不能只报一个 `available: true`。"""
        transport = FakeTransport()
        described = make_provider(transport).describe()
        assert described["attribution"] == "passthrough"
        assert described["credentialsConfigured"] is True
        assert "直通" in str(described["note"])

    def test_gpu_selector_picks_a_specific_card(self) -> None:
        payload = {
            "inventories": [
                {
                    "metricName": "GpuMemoryUtilization",
                    "labels": {"GpuSerialNumber": "GPU-A", "PciDeviceAddress": "0000:01:00.0"},
                    "data": [[epoch(), 11.0]],
                },
                {
                    "metricName": "GpuMemoryUtilization",
                    "labels": {"GpuSerialNumber": "GPU-B", "PciDeviceAddress": "0000:02:00.0"},
                    "data": [[epoch(), 22.0]],
                },
            ]
        }
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = payload
        provider = make_provider(transport, gpu_selector=lambda s: s.pci_address == "0000:02:00.0")
        assert provider.read().host_mem_usage_pct == 22.0

    def test_gpu_selector_mismatch_raises_with_candidates(self) -> None:
        """挑不到卡要说明候选有哪些 —— 否则联调时只能靠猜。"""
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        provider = make_provider(transport, gpu_selector=lambda s: False)
        with pytest.raises(GpuMetricsUnavailable, match="候选"):
            provider.read()

    def test_passthrough_reading_has_no_quota_so_quota_rule_cannot_fire(self) -> None:
        """直通下无配额概念 ⇒ `quota_usage_pct` 必须为 None。

        若返回 0，"配额使用率 0%"会是一个**编造出来的好消息**。
        """
        transport = FakeTransport()
        transport.get_responses["/zwatch/metrics"] = healthy_payload()
        assert make_provider(transport).read().quota_usage_pct is None


# ================================================================ 传输层


class TestHttpTransport:
    def test_empty_endpoint_is_rejected_at_construction(self) -> None:
        with pytest.raises(ZWatchUnavailable, match="ZSVIRT_ENDPOINT"):
            HttpZWatchTransport(endpoint="")

    def test_endpoint_trailing_slash_is_normalized(self) -> None:
        assert HttpZWatchTransport(endpoint="https://10.0.0.1/").endpoint == "https://10.0.0.1"

    def test_real_transport_is_structurally_compatible_with_the_protocol(self) -> None:
        """结构化协议：假传输与真传输在类型上必须可互换。

        `isinstance` 对 `Protocol` 不成立，所以核对方法签名而不是断言身份。
        """
        real = HttpZWatchTransport(endpoint="https://10.0.0.1")
        fake = FakeTransport()
        for impl in (real, fake):
            assert callable(impl.get)
            assert callable(impl.post)
        annotations: ZWatchTransport = real  # 类型检查器据此验证协议一致性
        assert annotations is real


def test_snapshot_exposes_identity_helpers() -> None:
    snapshot = CardSnapshot(
        gpu_identity="GPU-A",
        at=BASE,
        utilization_pct=1.0,
        mem_usage_pct=2.0,
        temperature_c=3.0,
        power_watts=4.0,
        labels={"GpuSerialNumber": "GPU-A", "PciDeviceAddress": "0000:01:00.0"},
    )
    assert snapshot.gpu_serial == "GPU-A"
    assert snapshot.pci_address == "0000:01:00.0"
    assert snapshot.host_uuid is None


def test_reading_defaults_match_the_partitioned_design() -> None:
    """默认口径仍是 `partitioned`：既有调用方（模拟渠道）语义不变。"""
    from app.zsvirt import SimulatedProvider

    reading = SimulatedProvider(profile="self_exhausted", seed=1).read()
    assert reading.attribution == "partitioned"
    assert reading.can_attribute is True
    assert reading.mem_used_bytes is not None
    assert reading.temperature_c is not None
