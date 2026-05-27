import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from shared.security import CLIENT_ID_RE


class ApiClient(BaseModel):
    client_id: str
    secret: str = Field(min_length=32)
    enabled: bool = True

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        if not CLIENT_ID_RE.fullmatch(value):
            raise ValueError("client_id must contain only letters, numbers, dots, underscores and dashes")
        return value


class ApiClientsSettings(BaseModel):
    clients: list[ApiClient] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_client_ids(self) -> "ApiClientsSettings":
        ids = [client.client_id for client in self.clients]
        if len(ids) != len(set(ids)):
            raise ValueError("client ids must be unique")
        return self

    def get_client(self, client_id: str) -> ApiClient | None:
        return next((client for client in self.clients if client.client_id == client_id), None)


def get_clients_path() -> Path:
    return Path(os.getenv("CLIENTS_PATH", "/app/api-data/clients.json"))


def load_clients_settings() -> ApiClientsSettings:
    clients_path = get_clients_path()
    if not clients_path.exists():
        raise FileNotFoundError(f"clients file not found: {clients_path}")

    with clients_path.open("r", encoding="utf-8") as file:
        raw_settings = json.load(file)

    return ApiClientsSettings.model_validate(raw_settings)
