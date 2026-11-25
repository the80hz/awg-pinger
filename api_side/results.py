import os
import sqlite3
from contextlib import closing
from datetime import UTC
from pathlib import Path

from shared.schemas import CheckResult


def connect() -> sqlite3.Connection:
    path = Path(os.getenv("RESULTS_DB_PATH", "/app/state/results.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, timeout=10)


def initialize() -> None:
    with closing(connect()) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS latest (
                client_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                checked_at REAL NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (client_id, server_id)
            )
        """)


def save(result: CheckResult) -> None:
    checked_at = result.checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    with closing(connect()) as db, db:
        db.execute(
            """
            INSERT INTO latest (client_id, server_id, checked_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (client_id, server_id) DO UPDATE SET
                checked_at = excluded.checked_at, payload = excluded.payload
            WHERE excluded.checked_at > latest.checked_at
            """,
            (
                result.client_id,
                result.server_id,
                checked_at.timestamp(),
                result.model_dump_json(),
            ),
        )


def list_results() -> list[CheckResult]:
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT payload FROM latest ORDER BY client_id, server_id"
        ).fetchall()
    return [CheckResult.model_validate_json(payload) for (payload,) in rows]


def get_result(client_id: str, server_id: str) -> CheckResult | None:
    with closing(connect()) as db:
        row = db.execute(
            "SELECT payload FROM latest WHERE client_id = ? AND server_id = ?",
            (client_id, server_id),
        ).fetchone()
    return CheckResult.model_validate_json(row[0]) if row else None
