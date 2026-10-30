"""Durable incident state and coalescing outbox; timestamps are UTC watermarks."""

import hashlib
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path

from shared.schemas import CheckResult


def state_path() -> Path:
    return Path(os.getenv("NOTIFICATION_DB_PATH", "/app/state/notifications.sqlite3"))


def destination_key(token: str, chat_id: str) -> str:
    # Token rotation for the same Telegram bot preserves its message ownership.
    bot_id = token.split(":", 1)[0]
    return hashlib.sha256(f"{bot_id}\n{chat_id}".encode()).hexdigest()


@contextmanager
def database() -> Generator[sqlite3.Connection, None, None]:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        # Serialize migration and state transitions across threads/processes.
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            migration = Path(__file__).with_name("migrations") / "001_incidents.sql"
            for statement in migration.read_text().split(";"):
                if statement.strip():
                    db.execute(statement)
        elif version != 1:
            raise RuntimeError("unsupported notification database version")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def record_result(result: CheckResult, destination: str) -> None:
    # Old clients may send naive dates; interpret them as UTC, not host local time.
    checked_at = result.checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    result = result.model_copy(update={"checked_at": checked_at.astimezone(UTC)})
    stamp = result.checked_at.isoformat(timespec="microseconds")
    key = (destination, result.client_id, result.server_id)
    with database() as db:
        previous = db.execute(
            "SELECT checked_at FROM checks WHERE destination=? AND client_id=? AND server_id=?",
            key,
        ).fetchone()
        if previous and previous["checked_at"] >= stamp:
            return
        db.execute("INSERT OR REPLACE INTO checks VALUES (?, ?, ?, ?)", (*key, stamp))
        active = db.execute(
            "SELECT id FROM incidents WHERE destination=? AND client_id=? AND server_id=? "
            "AND recovered=0",
            key,
        ).fetchone()
        if active:
            db.execute(
                "UPDATE incidents SET result_json=?, recovered=?, failures=failures+?, "
                "revision=revision+1 WHERE id=?",
                (
                    result.model_dump_json(),
                    int(result.ok),
                    int(not result.ok),
                    active["id"],
                ),
            )
        elif not result.ok:
            db.execute(
                "INSERT INTO incidents (destination, client_id, server_id, started_at, result_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (*key, stamp, result.model_dump_json()),
            )
