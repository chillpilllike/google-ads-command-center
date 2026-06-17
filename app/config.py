from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    app_name: str = "Google Ads Command Center"
    app_env: str = "local"
    secret_key: str = Field(default="")
    database_url: Optional[PostgresDsn] = Field(default=None)
    postgres_url: Optional[PostgresDsn] = Field(default=None)
    admin_email: str = "admin"
    admin_password: str = ""
    account_config_path: Path = ROOT_DIR / "config" / "google_ads_accounts.json"
    optimizer_env_file: Path = ROOT_DIR / ".env.gofinch"
    request_timeout_seconds: int = 2
    worker_timeout_seconds: int = 60 * 60
    auto_init_db: bool = False
    public_base_url: str = ""

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("database_url", "postgres_url", mode="before")
    @classmethod
    def normalize_postgres_scheme(cls, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql://", 1)
        return value

    @model_validator(mode="after")
    def require_runtime_secrets(self) -> "Settings":
        if self.database_url is None:
            self.database_url = self.postgres_url
        if self.database_url is None:
            raise ValueError("DATABASE_URL or POSTGRES_URL must be set.")
        if self.app_env == "production":
            weak_secret_keys = {"", "change-me", "change-me-before-production", "replace-with-a-long-random-secret"}
            if self.secret_key in weak_secret_keys:
                raise ValueError("SECRET_KEY must be set to a strong value in production.")
            if not self.admin_password or self.admin_password == "admin123":
                raise ValueError("ADMIN_PASSWORD must be set to a non-default value in production.")
        return self

    @property
    def sqlalchemy_async_url(self) -> str:
        assert self.database_url is not None
        return str(self.database_url).replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def sqlalchemy_sync_url(self) -> str:
        assert self.database_url is not None
        return str(self.database_url).replace("postgresql://", "postgresql+psycopg2://", 1)

    @property
    def dramatiq_pg_url(self) -> str:
        assert self.database_url is not None
        return str(self.database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
