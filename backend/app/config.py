"""应用配置。

配置项定义见 docs/DEPLOYMENT.md §4。
凭据一律从环境变量注入，仓库内只保留 .env.example。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

GpuProviderMode = Literal["zsvirt-zwatch", "guest-smi", "simulated", "auto"]


class Settings(BaseSettings):
    """从环境变量 / .env 加载的运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 服务 ---
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8080, alias="API_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_auth_token: str = Field(default="", alias="API_AUTH_TOKEN")

    # --- 数据库 ---
    database_url: str = Field(
        default="postgresql+psycopg://crosslayer:crosslayer@localhost:5432/crosslayer",
        alias="DATABASE_URL",
    )

    # --- ZSvirt 平台接入 ---
    zsvirt_endpoint: str = Field(default="", alias="ZSVIRT_ENDPOINT")
    zsvirt_auth_token: str = Field(default="", alias="ZSVIRT_AUTH_TOKEN")
    zsvirt_sync_interval_sec: int = Field(default=30, alias="ZSVIRT_SYNC_INTERVAL_SEC")

    # --- GPU 数据渠道（见 docs/DATA_MODEL.md §4.2.1）---
    gpu_provider: GpuProviderMode = Field(default="auto", alias="GPU_PROVIDER")
    simulated_data_enabled: bool = Field(default=False, alias="SIMULATED_DATA_ENABLED")

    # --- 接入限制（成员 A 的 Q4 已确认值）---
    ingest_batch_max: int = Field(default=1000, alias="INGEST_BATCH_MAX")
    ingest_resource_max: int = Field(default=500, alias="INGEST_RESOURCE_MAX")
    ingest_payload_max_bytes: int = Field(default=1024 * 1024, alias="INGEST_PAYLOAD_MAX_BYTES")

    # --- 时钟漂移（成员 A 的 Q3 已确认阈值）---
    clock_drift_warn_ms: int = Field(default=5000, alias="CLOCK_DRIFT_WARN_MS")

    # --- 诊断 ---
    diag_window_tolerance_sec: int = Field(default=30, alias="DIAG_WINDOW_TOLERANCE_SEC")

    # --- 敏感信息 ---
    sensitive_filter_enabled: bool = Field(default=True, alias="SENSITIVE_FILTER_ENABLED")

    @property
    def auth_enabled(self) -> bool:
        """按 docs/API_CONTRACT.md §4.9，认证默认关闭。"""
        return bool(self.api_auth_token.strip())

    @property
    def gpu_provider_mode(self) -> str:
        """**对外的** GPU 数据渠道（`docs/API_CONTRACT.md` §4.8）。

        配置为 `auto` 且开启了模拟数据时，对外必须报 `simulated` —— 这是
        诚实性要求（`DATA_MODEL.md` §4.2.1）：前端要能一眼分辨"这些 GPU 数字
        是真实采集的还是模拟出来的"。**在返回真实指标的地方报 simulated 是
        可接受的，反过来才是错的**，所以判定顺序是先看模拟开关。

        单一定义点：health / workloads / 将来的任何端点都读这里，
        避免同一个标签在三处各算一遍而产生漂移。
        """
        if self.simulated_data_enabled:
            return "simulated"
        return self.gpu_provider


@lru_cache
def get_settings() -> Settings:
    return Settings()
