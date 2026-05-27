from datetime import UTC, datetime

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class CheckResult(BaseModel):
    client_id: str
    server_id: str
    server_name: str | None = None
    ok: bool
    checked_at: datetime = Field(default_factory=utc_now)
    duration_ms: int | None = None
    comment: str
    command_output: str | None = None
    ping_output: str | None = None
