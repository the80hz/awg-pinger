import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from api_side import results
from api_side.clients import load_clients_settings
from api_side.incidents import destination_key, record_result
from api_side.telegram import notification_worker, telegram_config
from shared.schemas import CheckResult
from shared.security import NONCE_RE, parse_utc_timestamp, signature_matches


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await asyncio.to_thread(results.initialize)
    stop = asyncio.Event()
    worker = None
    if os.getenv("TELEGRAM_WORKER_ENABLED", "1") == "1":
        worker = asyncio.create_task(notification_worker(stop))
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            await worker


app = FastAPI(title="VPN Pinger API Side", version="0.1.0", lifespan=lifespan)

web_dir = Path(__file__).with_name("web")
app.mount("/static", StaticFiles(directory=web_dir), name="static")
used_nonces: dict[tuple[str, str], datetime] = {}
MAX_CLOCK_SKEW = timedelta(minutes=2)
MAX_BODY_BYTES = 64 * 1024


def reject(status_code: int, comment: str) -> NoReturn:
    raise HTTPException(
        status_code=status_code, detail={"ok": False, "comment": comment}
    )


def prune_nonces(now: datetime) -> None:
    expired = [
        key
        for key, timestamp in used_nonces.items()
        if now - timestamp > MAX_CLOCK_SKEW
    ]
    for key in expired:
        del used_nonces[key]


def verify_request_signature(
    body: str,
    result: CheckResult,
    x_client_id: str | None,
    x_timestamp: str | None,
    x_nonce: str | None,
    x_signature: str | None,
) -> None:
    if not x_client_id or not x_timestamp or not x_nonce or not x_signature:
        reject(401, "missing signature headers")
    if x_client_id != result.client_id:
        reject(401, "client id header does not match payload")
    if not NONCE_RE.fullmatch(x_nonce):
        reject(401, "invalid nonce")

    try:
        settings = load_clients_settings()
    except (FileNotFoundError, ValidationError, ValueError):
        reject(500, "clients settings error")

    api_client = settings.get_client(x_client_id)
    if api_client is None or not api_client.enabled:
        reject(401, "unknown or disabled client")

    try:
        request_time = parse_utc_timestamp(x_timestamp)
    except ValueError:
        reject(401, "invalid timestamp")

    now = datetime.now(UTC)
    if abs(now - request_time) > MAX_CLOCK_SKEW:
        reject(401, "timestamp is outside allowed window")

    prune_nonces(now)
    nonce_key = (x_client_id, x_nonce)
    if nonce_key in used_nonces:
        reject(409, "nonce already used")

    if not signature_matches(
        api_client.secret, x_timestamp, x_nonce, body, x_signature
    ):
        reject(401, "invalid signature")

    used_nonces[nonce_key] = request_time


@app.get("/health")
async def health() -> dict[str, object]:
    return {"ok": True}


@app.post("/checks")
async def receive_check(
    request: Request,
    x_client_id: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
    x_nonce: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
) -> dict[str, object]:
    body_bytes = await request.body()
    if len(body_bytes) > MAX_BODY_BYTES:
        reject(413, "request body too large")

    body = body_bytes.decode("utf-8")
    try:
        result = CheckResult.model_validate_json(body)
    except ValueError as exc:
        reject(422, f"invalid check result: {exc}")

    verify_request_signature(
        body, result, x_client_id, x_timestamp, x_nonce, x_signature
    )
    config = telegram_config()
    if config is not None:
        # Commit before acknowledging the check, independently of Telegram delivery.
        await asyncio.to_thread(record_result, result, destination_key(*config))
    await asyncio.to_thread(results.save, result)
    return {"ok": True, "received_at": datetime.now(UTC)}


@app.get("/checks/latest")
async def list_latest() -> dict[str, object]:
    records = await asyncio.to_thread(results.list_results)
    return {"results": [record.model_dump(mode="json") for record in records]}


@app.get("/checks/latest/{client_id}/{server_id}")
async def get_latest(client_id: str, server_id: str) -> dict[str, object]:
    result = await asyncio.to_thread(results.get_result, client_id, server_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "comment": "check result not found"}
        )
    return result.model_dump(mode="json")


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(web_dir / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/dashboard/data")
async def dashboard_data() -> dict[str, object]:
    clients = load_clients_settings().clients
    names = {client.client_id: client.name for client in clients if client.enabled}
    records = await asyncio.to_thread(results.list_results)
    fields = {"client_id", "server_id", "server_name", "ok", "checked_at", "duration_ms", "comment"}
    return {
        "clients": sorted(names),
        "client_names": names,
        "results": [
            record.model_dump(mode="json", include=fields)
            for record in records
            if record.client_id in names
        ],
    }
