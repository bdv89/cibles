"""Application settings using Pydantic BaseSettings."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration loaded from env vars or CLI."""

    # Database
    db_path: Path = Path("inventory.sqlite")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # SDS files root directory
    sds_root: Path = Path(__file__).parent.parent  # server_display/

    # Metrics database
    metrics_db: Path = Path(__file__).parent.parent / "metrics.sqlite"

    # Edit mode
    edit_password: str = ""

    # Read protection (optional - empty = public access)
    read_password: str = ""

    # Rate limiting
    rate_limit: int = 60  # requests per minute per IP

    # Backup settings (only used if edit_password is set)
    backup_dir: Path = Path(__file__).parent.parent / "backups"
    backup_keep: int = 7

    # Upload limit for documents (SDS / COA / BC).
    # NOTE: nginx caps /cibles/ at client_max_body_size 100m — anything larger is
    # rejected with a 413 before reaching FastAPI. Keep the two values aligned.
    max_sds_size: int = 100 * 1024 * 1024  # 100 MB

    class Config:
        env_prefix = ""
        case_sensitive = False

    @property
    def editing_enabled(self) -> bool:
        return bool(self.edit_password)

    @property
    def read_protected(self) -> bool:
        return bool(self.read_password)


# Global settings instance - initialized by main.py or run.py
settings: Optional[Settings] = None


def init_settings(**kwargs) -> Settings:
    """Initialize settings with optional overrides."""
    global settings
    settings = Settings(**kwargs)
    return settings


def get_settings() -> Settings:
    """Get current settings, initializing with defaults if needed."""
    global settings
    if settings is None:
        settings = Settings()
    return settings
