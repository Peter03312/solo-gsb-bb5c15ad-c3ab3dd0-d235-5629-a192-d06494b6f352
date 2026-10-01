"""应用配置（环境变量）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    user = os.environ.get("POSTGRES_USER", "plugindb")
    password = os.environ.get("POSTGRES_PASSWORD", "plugindb")
    host = os.environ.get("POSTGRES_HOST", "db")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "plugindb")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


@dataclass(frozen=True)
class Settings:
    database_url: str = _database_url()
    package_dir: str = os.environ.get("PACKAGE_DIR", "/data/packages")
    max_upload_bytes: int = int(os.environ.get("MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
    api_token: str | None = os.environ.get("API_TOKEN") or None


settings = Settings()
