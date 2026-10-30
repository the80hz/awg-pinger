import asyncio
import fcntl
import html
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, ValidationError

from api_side.incidents import database, destination_key, state_path
from shared.schemas import CheckResult

logger = logging.getLogger("api-side.telegram")


class TelegramSettings(BaseModel):
    bot_token: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)


class SentMessage(BaseModel):
    message_id: int = Field(gt=0)


class RetryParameters(BaseModel):
    retry_after: int = Field(default=30, ge=0)


class TelegramResponse(BaseModel):
    ok: bool
    result: SentMessage | None = None
    description: str = ""
    error_code: int | None = None
    parameters: RetryParameters = Field(default_factory=RetryParameters)


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


def incident_message(row: sqlite3.Row) -> str:
    result = CheckResult.model_validate_json(row["result_json"])
    started = datetime.fromisoformat(row["started_at"])
    elapsed = max(0, int((result.checked_at - started).total_seconds()))
    status = "✅ VPN восстановлен" if result.ok else "🔴 VPN недоступен"

    # Bound every external field before escaping, leaving room below Telegram's limit.
    def escaped(value: str, limit: int = 120) -> str:
        return html.escape(value[:limit])

    lines = [
        f"<b>{status}</b>",
        f"Пингер: <code>{escaped(result.client_id)}</code>",
        f"Сервер: <code>{escaped(result.server_name or result.server_id)}</code>",
        f"ID: <code>{escaped(result.server_id)}</code>",
        f"Начало: {started:%Y-%m-%d %H:%M:%S} UTC",
        f"Последняя проверка: {result.checked_at:%Y-%m-%d %H:%M:%S} UTC",
        f"Неудачных проверок: {row['failures']}",
        f"С момента обнаружения: {elapsed // 3600} ч {(elapsed % 3600) // 60} мин",
        f"Результат: {escaped(result.comment, 400)}",
    ]
    if not result.ok and result.command_output:
        lines.append(f"<pre>{html.escape(result.command_output[-900:])}</pre>")
    return "\n".join(lines)


def deliver_pending() -> None:
    """One dispatcher per database, including when multiple API workers run.

    A file lock spans HTTP calls; SQLite transactions stay short so ingestion
    remains available while Telegram is slow. The outbox coalesces new revisions.
    """
    config = telegram_config()
    if config is None:
        return
    token, chat_id = config
    destination = destination_key(token, chat_id)
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with database() as db:
            cooldown = db.execute(
                "SELECT until FROM delivery_cooldowns WHERE destination=?",
                (destination,),
            ).fetchone()
            if cooldown and cooldown["until"] > time.time():
                return
            pending = db.execute(
                "SELECT * FROM incidents WHERE destination=? "
                "AND delivered_revision<revision AND next_attempt<=? ORDER BY id LIMIT 50",
                (destination, time.time()),
            ).fetchall()
        with httpx.Client(timeout=10) as client:
            for row in pending:
                if not deliver_one(client, token, chat_id, row):
                    break


def deliver_one(
    client: httpx.Client, token: str, chat_id: str, row: sqlite3.Row
) -> bool:
    message_id = row["message_id"]
    method = "editMessageText" if message_id is not None else "sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": incident_message(row),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if message_id is not None:
        payload["message_id"] = message_id
    delay = min(1800, 30 * 2 ** min(row["attempts"], 6))
    rate_limited = False
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/{method}", json=payload
        )
        body = TelegramResponse.model_validate(response.json())
        description = body.description.lower()
        if body.ok:
            if message_id is None:
                if body.result is None:
                    raise ValueError("missing telegram message id")
                message_id = body.result.message_id
        elif method == "editMessageText" and "message is not modified" in description:
            pass  # A previous edit may have succeeded before our connection was lost.
        else:
            if response.status_code == 429 or body.error_code == 429:
                delay = max(delay, body.parameters.retry_after)
                rate_limited = True
            elif (
                method == "editMessageText"
                and "message to edit not found" in description
            ):
                # Only explicit deletion permits replacement; never on timeout/403/5xx.
                with database() as db:
                    db.execute(
                        "UPDATE incidents SET message_id=NULL WHERE id=?", (row["id"],)
                    )
            raise ValueError("telegram delivery rejected")
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        # Never log HTTP exception strings: their URL contains the bot token.
        logger.warning(
            "telegram delivery deferred incident_id=%s method=%s", row["id"], method
        )
        with database() as db:
            db.execute(
                "UPDATE incidents SET attempts=attempts+1, next_attempt=? WHERE id=?",
                (time.time() + delay, row["id"]),
            )
            if rate_limited:
                db.execute(
                    "INSERT OR REPLACE INTO delivery_cooldowns VALUES (?, ?)",
                    (row["destination"], time.time() + delay),
                )
                db.execute(
                    "UPDATE incidents SET next_attempt=MAX(next_attempt, ?) WHERE destination=?",
                    (time.time() + delay, row["destination"]),
                )
        return not rate_limited
    with database() as db:
        db.execute(
            "UPDATE incidents SET message_id=?, delivered_revision=?, attempts=0, next_attempt=0 "
            "WHERE id=?",
            (message_id, row["revision"], row["id"]),
        )
    return True


async def notification_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(deliver_pending)
        except (sqlite3.Error, OSError, ValueError, RuntimeError):
            logger.error("notification dispatcher failed; retrying")
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass
