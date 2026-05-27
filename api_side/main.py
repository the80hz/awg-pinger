import os
from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException

from shared.schemas import CheckResult


app = FastAPI(title="AWG Pinger API Side", version="0.1.0")

latest_results: dict[tuple[str, str], CheckResult] = {}
result_history: list[CheckResult] = []


def verify_token(authorization: str | None) -> None:
    token = os.getenv("API_TOKEN")
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail={"ok": False, "comment": "invalid api token"})


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/checks")
async def receive_check(result: CheckResult, authorization: str | None = Header(default=None)) -> dict:
    verify_token(authorization)
    latest_results[(result.client_id, result.server_id)] = result
    result_history.append(result)
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
