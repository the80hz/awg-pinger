import asyncio
import logging
import os

import httpx
from pydantic import ValidationError

from awg_client_side.checker import check_tunnel
from shared.settings import get_settings_path, load_client_settings


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("awg-client-side")


def auth_headers() -> dict[str, str]:
    token = os.getenv("API_TOKEN")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


async def report_result(client: httpx.AsyncClient, api_base_url: str, result: dict) -> None:
    response = await client.post(
        f"{api_base_url}/checks",
        json=result,
        headers=auth_headers(),
    )
    response.raise_for_status()


async def run_once() -> None:
    settings_path = get_settings_path()
    settings = load_client_settings()
    timeout = httpx.Timeout(settings.request_timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for server in settings.servers:
            result = await check_tunnel(settings_path, settings.client_id, server)
            try:
                await report_result(client, settings.api_base_url, result.model_dump(mode="json"))
                logger.info("reported check result server_id=%s ok=%s", server.id, result.ok)
            except httpx.HTTPError as exc:
                logger.error("failed to report check result server_id=%s error=%s", server.id, exc)


async def run_forever() -> None:
    while True:
        try:
            settings = load_client_settings()
            await run_once()
            await asyncio.sleep(settings.interval_seconds)
        except (FileNotFoundError, ValidationError, ValueError) as exc:
            logger.error("settings error: %s", exc)
            await asyncio.sleep(60)


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
