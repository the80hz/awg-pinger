import html
import json
import logging
import os
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, ValidationError

from shared.schemas import CheckResult


logger = logging.getLogger("api-side.telegram")


class TelegramSettings(BaseModel):
    bot_token: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)


def telegram_settings_path() -> Path:
    return Path(os.getenv("TELEGRAM_CONFIG_PATH", "/app/api-data/telegram.json"))


def load_telegram_settings() -> TelegramSettings | None:
    path = telegram_settings_path()
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as file:
            raw_settings = json.load(file)
        return TelegramSettings.model_validate(raw_settings)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        logger.error("failed to load telegram settings from %s: %s", path, exc)
        return None


def telegram_config() -> tuple[str, str] | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id

    settings = load_telegram_settings()
    if settings is None:
        return None
    return settings.bot_token, settings.chat_id


def failure_message(result: CheckResult) -> str:
    server_label = result.server_name or result.server_id
    lines = [
        "<b>AWG tunnel check failed</b>",
        f"Client: <code>{html.escape(result.client_id)}</code>",
        f"Server: <code>{html.escape(server_label)}</code>",
        f"Server ID: <code>{html.escape(result.server_id)}</code>",
        f"Checked at: <code>{html.escape(result.checked_at.isoformat())}</code>",
        f"Comment: {html.escape(result.comment)}",
    ]
    if result.duration_ms is not None:
        lines.append(f"Duration: <code>{result.duration_ms} ms</code>")
    if result.command_output:
        output = result.command_output[-1500:]
        lines.append("")
        lines.append("<b>Command output</b>")
        lines.append(f"<pre>{html.escape(output)}</pre>")
    return "\n".join(lines)


async def send_failure_notification(result: CheckResult) -> None:
    config = telegram_config()
    if config is None:
        return

    token, chat_id = config
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": failure_message(result),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error(
            "failed to send telegram notification client_id=%s server_id=%s error=%s",
            result.client_id,
            result.server_id,
            exc,
        )
