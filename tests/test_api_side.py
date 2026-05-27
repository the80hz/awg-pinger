import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

import api_side.main
from api_side.main import app, latest_results, result_history, used_nonces
from api_side.telegram import failure_message, telegram_config
from shared.schemas import CheckResult
from shared.security import canonical_json, sign_body


SECRET = "test-secret-value-that-is-long-enough"


def configure_clients(monkeypatch, tmp_path, enabled: bool = True) -> None:
    clients_path = tmp_path / "clients.json"
    clients_path.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "client-1",
                        "secret": SECRET,
                        "enabled": enabled,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIENTS_PATH", str(clients_path))


def signed_headers(body: str, nonce: str = "nonce-1234567890123456") -> dict[str, str]:
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "Content-Type": "application/json",
        "X-Client-Id": "client-1",
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": sign_body(SECRET, timestamp, nonce, body),
    }


def check_payload(client_id: str = "client-1") -> dict:
    return {
        "client_id": client_id,
        "server_id": "vps-1",
        "server_name": "VPS 1",
        "ok": True,
        "comment": "tunnel is reachable",
        "duration_ms": 123,
    }


def reset_state() -> None:
    latest_results.clear()
    result_history.clear()
    used_nonces.clear()


def test_receive_and_list_latest(monkeypatch, tmp_path) -> None:
    configure_clients(monkeypatch, tmp_path)
    reset_state()
    client = TestClient(app)
    body = canonical_json(check_payload())

    response = client.post(
        "/checks",
        content=body,
        headers=signed_headers(body),
    )
    assert response.status_code == 200

    latest_response = client.get("/checks/latest")
    assert latest_response.status_code == 200
    assert latest_response.json()["results"][0]["client_id"] == "client-1"
    assert latest_response.json()["results"][0]["server_id"] == "vps-1"


def test_rejects_invalid_signature(monkeypatch, tmp_path) -> None:
    configure_clients(monkeypatch, tmp_path)
    reset_state()
    client = TestClient(app)
    body = canonical_json(check_payload())
    headers = signed_headers(body)
    headers["X-Signature"] = "0" * 64

    response = client.post("/checks", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"]["comment"] == "invalid signature"


def test_rejects_replayed_nonce(monkeypatch, tmp_path) -> None:
    configure_clients(monkeypatch, tmp_path)
    reset_state()
    client = TestClient(app)
    body = canonical_json(check_payload())
    headers = signed_headers(body, nonce="nonce-abcdef1234567890")

    assert client.post("/checks", content=body, headers=headers).status_code == 200
    replay_response = client.post("/checks", content=body, headers=headers)

    assert replay_response.status_code == 409
    assert replay_response.json()["detail"]["comment"] == "nonce already used"


def test_rejects_client_id_mismatch(monkeypatch, tmp_path) -> None:
    configure_clients(monkeypatch, tmp_path)
    reset_state()
    client = TestClient(app)
    body = canonical_json(check_payload(client_id="other-client"))

    response = client.post("/checks", content=body, headers=signed_headers(body))

    assert response.status_code == 401
    assert response.json()["detail"]["comment"] == "client id header does not match payload"


def test_rejects_expired_timestamp(monkeypatch, tmp_path) -> None:
    configure_clients(monkeypatch, tmp_path)
    reset_state()
    client = TestClient(app)
    body = canonical_json(check_payload())
    timestamp = (datetime.now(UTC) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    headers = {
        "Content-Type": "application/json",
        "X-Client-Id": "client-1",
        "X-Timestamp": timestamp,
        "X-Nonce": "nonce-expired123456789",
        "X-Signature": sign_body(SECRET, timestamp, "nonce-expired123456789", body),
    }

    response = client.post("/checks", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"]["comment"] == "timestamp is outside allowed window"


def test_rejects_disabled_client(monkeypatch, tmp_path) -> None:
    configure_clients(monkeypatch, tmp_path, enabled=False)
    reset_state()
    client = TestClient(app)
    body = canonical_json(check_payload())

    response = client.post("/checks", content=body, headers=signed_headers(body))

    assert response.status_code == 401
    assert response.json()["detail"]["comment"] == "unknown or disabled client"


def test_does_not_notify_on_success(monkeypatch, tmp_path) -> None:
    configure_clients(monkeypatch, tmp_path)
    reset_state()
    calls = []

    async def fake_send_failure_notification(result: CheckResult) -> None:
        calls.append(result)

    monkeypatch.setattr(api_side.main, "send_failure_notification", fake_send_failure_notification)
    client = TestClient(app)
    body = canonical_json(check_payload())

    response = client.post(
        "/checks",
        content=body,
        headers=signed_headers(body, nonce="nonce-success12345678"),
    )

    assert response.status_code == 200
    assert calls == []


def test_notifies_on_failure(monkeypatch, tmp_path) -> None:
    configure_clients(monkeypatch, tmp_path)
    reset_state()
    calls = []

    async def fake_send_failure_notification(result: CheckResult) -> None:
        calls.append(result)

    monkeypatch.setattr(api_side.main, "send_failure_notification", fake_send_failure_notification)
    client = TestClient(app)
    payload = check_payload()
    payload["ok"] = False
    payload["comment"] = "ping failed"
    payload["command_output"] = "100% packet loss"
    body = canonical_json(payload)

    response = client.post(
        "/checks",
        content=body,
        headers=signed_headers(body, nonce="nonce-failure12345678"),
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0].server_id == "vps-1"
    assert calls[0].ok is False


def test_failure_message_contains_context() -> None:
    message = failure_message(
        CheckResult(
            client_id="client-1",
            server_id="vps-1",
            server_name="VPS 1",
            ok=False,
            comment="ping failed",
            duration_ms=123,
            command_output="100% packet loss",
        )
    )

    assert "AWG tunnel check failed" in message
    assert "client-1" in message
    assert "VPS 1" in message
    assert "ping failed" in message
    assert "100% packet loss" in message


def test_telegram_config_loads_from_file(monkeypatch, tmp_path) -> None:
    telegram_path = tmp_path / "telegram.json"
    telegram_path.write_text(
        json.dumps({"bot_token": "file-token", "chat_id": "file-chat"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CONFIG_PATH", str(telegram_path))

    assert telegram_config() == ("file-token", "file-chat")


def test_telegram_env_overrides_file(monkeypatch, tmp_path) -> None:
    telegram_path = tmp_path / "telegram.json"
    telegram_path.write_text(
        json.dumps({"bot_token": "file-token", "chat_id": "file-chat"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_CONFIG_PATH", str(telegram_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat")

    assert telegram_config() == ("env-token", "env-chat")
