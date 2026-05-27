from datetime import UTC, datetime, timedelta

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import ValidationError

from api_side.clients import load_clients_settings
from api_side.telegram import send_failure_notification
from shared.schemas import CheckResult
from shared.security import NONCE_RE, parse_utc_timestamp, signature_matches


app = FastAPI(title="AWG Pinger API Side", version="0.1.0")

latest_results: dict[tuple[str, str], CheckResult] = {}
result_history: list[CheckResult] = []
used_nonces: dict[tuple[str, str], datetime] = {}
MAX_CLOCK_SKEW = timedelta(minutes=2)
MAX_BODY_BYTES = 64 * 1024


def reject(status_code: int, comment: str) -> None:
    raise HTTPException(status_code=status_code, detail={"ok": False, "comment": comment})


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
    if not all([x_client_id, x_timestamp, x_nonce, x_signature]):
        reject(401, "missing signature headers")
    if x_client_id != result.client_id:
        reject(401, "client id header does not match payload")
    if not NONCE_RE.fullmatch(x_nonce):
        reject(401, "invalid nonce")

    try:
        settings = load_clients_settings()
    except (FileNotFoundError, ValidationError, ValueError) as exc:
        reject(500, f"clients settings error: {exc}")

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

    if not signature_matches(api_client.secret, x_timestamp, x_nonce, body, x_signature):
        reject(401, "invalid signature")

    used_nonces[nonce_key] = request_time


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/checks")
async def receive_check(
    request: Request,
    background_tasks: BackgroundTasks,
    x_client_id: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
    x_nonce: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
) -> dict:
    body_bytes = await request.body()
    if len(body_bytes) > MAX_BODY_BYTES:
        reject(413, "request body too large")

    body = body_bytes.decode("utf-8")
    try:
        result = CheckResult.model_validate_json(body)
    except ValueError as exc:
        reject(422, f"invalid check result: {exc}")

    verify_request_signature(body, result, x_client_id, x_timestamp, x_nonce, x_signature)
    latest_results[(result.client_id, result.server_id)] = result
    result_history.append(result)
    if not result.ok:
        background_tasks.add_task(send_failure_notification, result)
    return {"ok": True, "received_at": datetime.now(UTC)}


@app.get("/checks/latest")
async def list_latest() -> dict:
    results = sorted(
        latest_results.values(),
        key=lambda item: (item.client_id, item.server_id),
    )
    return {"results": [result.model_dump(mode="json") for result in results]}


@app.get("/checks/latest/{client_id}/{server_id}")
async def get_latest(client_id: str, server_id: str) -> dict:
    result = latest_results.get((client_id, server_id))
    if result is None:
        raise HTTPException(status_code=404, detail={"ok": False, "comment": "check result not found"})
    return result.model_dump(mode="json")
