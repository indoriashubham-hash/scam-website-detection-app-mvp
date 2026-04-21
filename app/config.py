"""Centralised settings. All runtime knobs pass through here."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- infra ---
    database_url: str = Field("postgresql+asyncpg://wri:wri@postgres:5432/wri", alias="DATABASE_URL")
    redis_url: str = Field("redis://redis:6379/0", alias="REDIS_URL")

    # --- object store ---
    s3_endpoint: str = Field("http://minio:9000", alias="S3_ENDPOINT")
    s3_access_key: str = Field("wri", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field("wri-secret", alias="S3_SECRET_KEY")
    s3_bucket: str = Field("wri-evidence", alias="S3_BUCKET")
    s3_public_base: str = Field("http://localhost:9000/wri-evidence", alias="S3_PUBLIC_BASE")

    # --- crawl knobs ---
    crawl_max_pages: int = Field(40, alias="CRAWL_MAX_PAGES")
    crawl_per_host_rps: float = Field(1.0, alias="CRAWL_PER_HOST_RPS")
    crawl_page_timeout_ms: int = Field(30_000, alias="CRAWL_PAGE_TIMEOUT_MS")
    crawl_nav_idle_ms: int = Field(15_000, alias="CRAWL_NAV_IDLE_MS")
    crawl_user_agent: str = Field(
        "WebsiteRiskInvestigator/0.1 (+https://example.com/bot)", alias="CRAWL_USER_AGENT"
    )

    # --- feature flags ---
    enable_playwright: bool = Field(True, alias="ENABLE_PLAYWRIGHT")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
