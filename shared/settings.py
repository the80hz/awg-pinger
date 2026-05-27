import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator


SERVER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


class ServerConfig(BaseModel):
    id: str
    name: str | None = None
    config: str
    ping_host: str
    ping_count: int = Field(default=3, ge=1, le=10)
    timeout_seconds: int = Field(default=20, ge=3, le=120)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not SERVER_ID_RE.fullmatch(value):
            raise ValueError("server id must contain only letters, numbers, dots, underscores and dashes")
        return value

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("config must be a relative file name inside data directory")
        if path.name != value:
            raise ValueError("config must be a file name, not a nested path")
        if path.suffix != ".conf":
            raise ValueError("config must point to a .conf file")
        return value


class ClientSettings(BaseModel):
    client_id: str = "default"
    api_base_url: str
    interval_seconds: int = Field(default=1800, ge=30)
    request_timeout_seconds: int = Field(default=30, ge=3, le=300)
    servers: list[ServerConfig] = Field(default_factory=list)

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, value: str) -> str:
        cleaned = value.rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("api_base_url must start with http:// or https://")
        return cleaned

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ClientSettings":
        ids = [server.id for server in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError("server ids must be unique")
        return self


def get_settings_path() -> Path:
    return Path(os.getenv("SETTINGS_PATH", "/app/data/settings.json"))


def load_client_settings() -> ClientSettings:
    settings_path = get_settings_path()
    if not settings_path.exists():
        raise FileNotFoundError(f"settings file not found: {settings_path}")

    with settings_path.open("r", encoding="utf-8") as file:
        raw_settings = json.load(file)

    return ClientSettings.model_validate(raw_settings)
