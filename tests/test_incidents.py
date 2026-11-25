import asyncio
import html
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from api_side import telegram
from api_side.incidents import database, destination_key, record_result
from shared.schemas import CheckResult

KEY = destination_key("123:token", "chat")
START = datetime(2026, 10, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "NOTIFICATION_DB_PATH", str(tmp_path / "state" / "notifications.db")
    )
    monkeypatch.setattr(telegram, "telegram_config", lambda: ("123:token", "chat"))


def result(minute=0, ok=False, client="one", server="sg", **kwargs):
    return CheckResult(
        client_id=client,
        server_id=server,
        ok=ok,
        checked_at=START + timedelta(minutes=minute),
        comment="handshake failed",
        **kwargs,
    )


def rows():
    with database() as db:
        return db.execute("SELECT * FROM incidents ORDER BY id").fetchall()


def ready():
    with database() as db:
        db.execute("UPDATE incidents SET next_attempt=0")


def transport(monkeypatch, handler=None):
    calls = []
    real_client = httpx.Client

    def handle(request):
        import json

        calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        if handler:
            return handler(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    monkeypatch.setattr(
        telegram.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return calls


def test_lifecycle_and_restart(monkeypatch):
    calls = transport(monkeypatch)
    record_result(result(ok=True), KEY)
    telegram.deliver_pending()
    assert calls == []
    record_result(result(1), KEY)
    telegram.deliver_pending()
    # Every operation reopens SQLite; no in-memory incident cache survives between calls.
    record_result(result(31), KEY)
    telegram.deliver_pending()
    record_result(result(61, ok=True), KEY)
    telegram.deliver_pending()
    record_result(result(91, ok=True), KEY)
    telegram.deliver_pending()
    record_result(result(121), KEY)
    telegram.deliver_pending()
    assert [method for method, _ in calls] == [
        "sendMessage",
        "editMessageText",
        "editMessageText",
        "sendMessage",
    ]
    assert calls[1][1]["message_id"] == calls[2][1]["message_id"] == 42
    assert "Неудачных проверок: 2" in calls[1][1]["text"]
    assert "восстановлен" in calls[2][1]["text"]
    assert "1 ч 0 мин" in calls[2][1]["text"]
    assert len(rows()) == 2


def test_duplicates_stale_results_and_concurrent_ingestion():
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: record_result(result(5), KEY), range(16)))
    record_result(result(4), KEY)
    assert rows()[0]["failures"] == 1
    record_result(result(6, ok=True), KEY)
    record_result(result(5), KEY)
    assert len(rows()) == 1
    assert rows()[0]["recovered"] == 1
    record_result(result(7), KEY)
    record_result(result(8, client="two"), KEY)
    record_result(result(8, server="us"), KEY)
    assert len(rows()) == 4


def test_timeout_on_edit_keeps_id_and_retries_recovery(monkeypatch):
    calls = transport(monkeypatch)
    record_result(result(), KEY)
    telegram.deliver_pending()
    record_result(result(1, ok=True), KEY)
    original = telegram.httpx.Client

    def failed(*args, **kwargs):
        raise httpx.ReadTimeout("secret must not be logged")

    # Exercise the HTTP request, not client construction.
    with monkeypatch.context() as patch, original() as client:
        patch.setattr(client, "post", failed)
        assert telegram.deliver_one(client, "123:token", "chat", rows()[0])
    row = rows()[0]
    assert row["message_id"] == 42
    assert row["delivered_revision"] < row["revision"]
    ready()
    telegram.deliver_pending()
    assert [method for method, _ in calls] == ["sendMessage", "editMessageText"]
    assert rows()[0]["delivered_revision"] == rows()[0]["revision"]


@pytest.mark.parametrize(
    "status,description", [(500, "server error"), (403, "Forbidden")]
)
def test_errors_do_not_replace_message(monkeypatch, status, description):
    record_result(result(), KEY)
    with database() as db:
        db.execute("UPDATE incidents SET message_id=42")
    calls = transport(
        monkeypatch,
        lambda _: httpx.Response(
            status, json={"ok": False, "description": description}
        ),
    )
    telegram.deliver_pending()
    assert calls[0][0] == "editMessageText"
    assert rows()[0]["message_id"] == 42
    assert rows()[0]["delivered_revision"] == 0


def test_deleted_message_replaced_on_next_attempt(monkeypatch):
    record_result(result(), KEY)
    with database() as db:
        db.execute("UPDATE incidents SET message_id=42")
    responses = iter(
        [
            httpx.Response(
                400,
                json={
                    "ok": False,
                    "description": "Bad Request: message to edit not found",
                },
            ),
            httpx.Response(200, json={"ok": True, "result": {"message_id": 43}}),
        ]
    )
    calls = transport(monkeypatch, lambda _: next(responses))
    telegram.deliver_pending()
    assert rows()[0]["message_id"] is None
    ready()
    telegram.deliver_pending()
    assert [method for method, _ in calls] == ["editMessageText", "sendMessage"]
    assert rows()[0]["message_id"] == 43


def test_not_modified_is_success(monkeypatch):
    record_result(result(), KEY)
    with database() as db:
        db.execute("UPDATE incidents SET message_id=42")
    transport(
        monkeypatch,
        lambda _: httpx.Response(
            400,
            json={"ok": False, "description": "Bad Request: message is not modified"},
        ),
    )
    telegram.deliver_pending()
    assert rows()[0]["delivered_revision"] == 1


def test_rate_limit_defers_whole_destination(monkeypatch):
    record_result(result(), KEY)
    record_result(result(server="us"), KEY)
    calls = transport(
        monkeypatch,
        lambda _: httpx.Response(
            429, json={"ok": False, "parameters": {"retry_after": 600}}
        ),
    )
    telegram.deliver_pending()
    assert len(calls) == 1
    assert all(row["next_attempt"] > telegram.time.time() + 590 for row in rows())
    record_result(result(server="new-server"), KEY)
    telegram.deliver_pending()
    assert len(calls) == 1


def test_new_revision_during_delivery_is_not_lost(monkeypatch):
    record_result(result(), KEY)

    def response(_):
        record_result(result(1, ok=True), KEY)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    calls = transport(monkeypatch, response)
    telegram.deliver_pending()
    assert rows()[0]["delivered_revision"] == 1
    assert rows()[0]["revision"] == 2
    telegram.deliver_pending()
    assert calls[1][0] == "editMessageText"
    assert rows()[0]["delivered_revision"] == 2


def test_safe_bounded_message_and_destination_isolation(monkeypatch):
    monkeypatch.setattr(telegram, "client_display_name", lambda _: "Home <test>")
    record_result(result(command_output="<&😀" * 5000, server_name="<b>name</b>"), KEY)
    message = telegram.incident_message(rows()[0])
    assert "Home &lt;test&gt;" in message
    assert "&lt;b&gt;name&lt;/b&gt;" in message
    assert len(html.unescape(message).encode("utf-16-le")) // 2 < 4096
    assert destination_key("123:rotated", "chat") == KEY
    monkeypatch.setattr(telegram, "telegram_config", lambda: ("456:other", "chat"))
    calls = transport(monkeypatch)
    telegram.deliver_pending()
    assert calls == []


def test_worker_retries_without_new_checks(monkeypatch):
    record_result(result(), KEY)
    calls = transport(monkeypatch)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(telegram.notification_worker(stop))
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(run())
    assert calls[0][0] == "sendMessage"


def test_dispatch_lock_prevents_duplicate_send(monkeypatch):
    record_result(result(), KEY)

    def response(_):
        # Simulate another dispatcher entering during an in-flight request.
        telegram.deliver_pending()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    calls = transport(monkeypatch, response)
    telegram.deliver_pending()
    assert len(calls) == 1


def test_state_survives_separate_process(monkeypatch):
    import subprocess
    import sys

    calls = transport(monkeypatch)
    record_result(result(), KEY)
    telegram.deliver_pending()
    code = """
from datetime import datetime, UTC
from api_side.incidents import record_result, destination_key
from shared.schemas import CheckResult
record_result(CheckResult(client_id="one", server_id="sg", ok=True,
    comment="recovered", checked_at=datetime(2026, 10, 30, 1, tzinfo=UTC)),
    destination_key("123:token", "chat"))
"""
    subprocess.run([sys.executable, "-c", code], check=True)
    telegram.deliver_pending()
    assert [method for method, _ in calls] == ["sendMessage", "editMessageText"]
    assert calls[1][1]["message_id"] == 42


@pytest.mark.parametrize("body", [[], {}, {"ok": True}])
def test_malformed_response_is_retried(monkeypatch, body):
    record_result(result(), KEY)
    transport(monkeypatch, lambda _: httpx.Response(200, json=body))
    telegram.deliver_pending()
    assert rows()[0]["delivered_revision"] == 0
    assert rows()[0]["attempts"] == 1
