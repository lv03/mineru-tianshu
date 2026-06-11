"""
MinerU Tianshu - 统一配置层
Centralized Settings

把分散在各处、默认值容易打架的关键环境变量收敛到单一来源，提供类型校验与安全默认值，
消除「同一配置多处 os.getenv、默认值不一致」整类问题。

适用范围（务实迁移）：
    - 仅覆盖 Web/DB/基础设施/认证等 config-light 模块的跨切面配置。
    - 重 ML 引擎模块（paddleocr/sensevoice/video/watermark 等）的模型路径类 env 不在此处，保持原样。

兼容性：
    - 字段同时兼容旧的 RUSTFS_* 命名（通过 AliasChoices）。
    - 仅集中「声明 + 默认值 + 校验」，各模块按需读取 settings.xxx；旧 env 名称保持不变。
"""

import os
from functools import lru_cache
from typing import Optional

from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # ---- 运行模式 ----
    env: str = Field(default="", validation_alias="ENV")
    require_secrets: bool = Field(default=False, validation_alias="REQUIRE_SECRETS")

    # ---- 认证 / 签名 ----
    jwt_secret_key: str = Field(default="your-secret-key-change-in-production", validation_alias="JWT_SECRET_KEY")
    jwt_expire_minutes: int = Field(default=1440, validation_alias="JWT_EXPIRE_MINUTES")
    # 文件签名 URL：密钥优先 FILE_URL_SECRET，未设置时由 file_url 回落到 jwt_secret_key
    file_url_secret: Optional[str] = Field(default=None, validation_alias="FILE_URL_SECRET")
    file_url_ttl: int = Field(default=3600, validation_alias="FILE_URL_TTL")

    # ---- 对象存储（MinIO，兼容 RUSTFS_*）----
    minio_enabled: bool = Field(
        default=False, validation_alias=AliasChoices("MINIO_ENABLED", "RUSTFS_ENABLED")
    )
    minio_endpoint: str = Field(
        default="minio:9000", validation_alias=AliasChoices("MINIO_ENDPOINT", "RUSTFS_ENDPOINT")
    )
    minio_access_key: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("MINIO_ACCESS_KEY", "RUSTFS_ACCESS_KEY")
    )
    minio_secret_key: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("MINIO_SECRET_KEY", "RUSTFS_SECRET_KEY")
    )
    minio_bucket: str = Field(
        default="ts-img", validation_alias=AliasChoices("MINIO_BUCKET", "RUSTFS_BUCKET")
    )
    minio_public_url: str = Field(
        default="", validation_alias=AliasChoices("MINIO_PUBLIC_URL", "RUSTFS_PUBLIC_URL")
    )
    minio_presign_ttl: int = Field(default=3600, validation_alias="MINIO_PRESIGN_TTL")

    # ---- Redis 队列 ----
    redis_queue_enabled: bool = Field(default=False, validation_alias="REDIS_QUEUE_ENABLED")

    # ---- 队列 / 并发 / 可靠性 ----
    max_concurrent_tasks: int = Field(default=1, validation_alias="MAX_CONCURRENT_TASKS")
    max_retry: int = Field(default=3, validation_alias="MAX_RETRY")
    worker_heartbeat_interval: int = Field(default=60, validation_alias="WORKER_HEARTBEAT_INTERVAL")
    service_max_restarts: int = Field(default=5, validation_alias="SERVICE_MAX_RESTARTS")

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production" or self.require_secrets

    @property
    def jwt_secret_is_insecure(self) -> bool:
        key = (self.jwt_secret_key or "")
        # 为空、精确占位符、或仍含模板标记 "change-in-production" 均视为不安全
        return (not key) or key == "your-secret-key-change-in-production" or ("change-in-production" in key.lower())


@lru_cache
def get_settings() -> Settings:
    """进程内单例。注意：start_all 在 load_dotenv(override=True) 之后才应首次取用。"""
    return Settings()


# 便捷别名
def reload_settings() -> Settings:
    """清除缓存并重新读取（主要用于测试或 .env 重载后）。"""
    get_settings.cache_clear()
    return get_settings()
