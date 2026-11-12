"""Fixed installation API configuration.

Trust anchor (Ed25519 public key), target device address and expected device
model are fixed configuration — never supplied per request.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FW_", env_file=".env", extra="ignore")

    database_path: Path = Path("/data/installer.db")
    trusted_public_key_path: Path = Path("/keys/public.pem")
    device_base_url: str = "http://device:8001"
    device_model: str = "gateway-gx100"
    max_firmware_bytes: int = 8 * 1024 * 1024  # hard limit: 8 MiB
    device_timeout_seconds: float = 5.0
    reconcile_interval_seconds: float = 3.0


settings = Settings()
