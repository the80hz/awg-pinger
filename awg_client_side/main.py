import asyncio
import logging
import os
import secrets
import socket
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from awg_client_side.checker import check_tunnel
from shared.security import canonical_json, sign_body
from shared.settings import get_settings_path, load_client_settings


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("awg-client-side")


def warn_if_loopback_api_url(api_base_url: str) -> None:
    host = urlparse(api_base_url).hostname
    if host in {"localhost", "127.0.0.1", "::1"}:
        logger.warning(
            "api_base_url points to loopback host=%s; inside Docker this points to awg-client-side, "
            "not api-side. Use http://api-side:8000 when both services run in the same compose project.",
            host,
        )


def signed_headers(client_id: str, client_secret: str, body: str) -> dict[str, str]:
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    nonce = secrets.token_urlsafe(32)
    signature = sign_body(client_secret, timestamp, nonce, body)
    return {
        "Content-Type": "application/json",
        "X-Client-Id": client_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
    }


async def report_result(
    client: httpx.AsyncClient,
    api_base_url: str,
    client_id: str,
    client_secret: str,
    result: dict,
) -> None:
    body = canonical_json(result)
    logger.debug(
        "posting check result api_base_url=%s client_id=%s server_id=%s ok=%s body_bytes=%s",
        api_base_url,
        client_id,
        result.get("server_id"),
        result.get("ok"),
        len(body.encode("utf-8")),
    )
    response = await client.post(
        f"{api_base_url}/checks",
        content=body,
        headers=signed_headers(client_id, client_secret, body),
    )
    logger.debug(
        "api response status_code=%s server_id=%s response=%s",
        response.status_code,
        result.get("server_id"),
        response.text[:1000],
    )
    response.raise_for_status()


def log_api_resolution(api_base_url: str) -> None:
    host = urlparse(api_base_url).hostname
    if not host:
        logger.warning("api_base_url has no hostname api_base_url=%s", api_base_url)
        return
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, None)})
        logger.debug("resolved api host host=%s addresses=%s", host, addresses)
    except socket.gaierror as exc:
        logger.error("failed to resolve api host host=%s error=%s", host, exc)


async def run_once() -> None:
    settings_path = get_settings_path()
    logger.info("loading settings path=%s", settings_path)
    settings = load_client_settings()
    logger.info(
        "loaded settings client_id=%s api_base_url=%s interval_seconds=%s servers=%s",
        settings.client_id,
        settings.api_base_url,
        settings.interval_seconds,
        len(settings.servers),
    )
    warn_if_loopback_api_url(settings.api_base_url)
    log_api_resolution(settings.api_base_url)
    timeout = httpx.Timeout(settings.request_timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for server in settings.servers:
            logger.info("checking server server_id=%s name=%s", server.id, server.name)
            result = await check_tunnel(settings_path, settings.client_id, server)
            log_api_resolution(settings.api_base_url)
            try:
                await report_result(
                    client,
                    settings.api_base_url,
                    settings.client_id,
                    settings.client_secret,
                    result.model_dump(mode="json"),
                )
                logger.info(
                    "reported check result server_id=%s ok=%s duration_ms=%s",
                    server.id,
                    result.ok,
                    result.duration_ms,
                )
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "api rejected check result server_id=%s status_code=%s response=%s",
                    server.id,
                    exc.response.status_code,
                    exc.response.text[:2000],
                )
            except httpx.HTTPError as exc:
                logger.error("failed to report check result server_id=%s error=%r", server.id, exc)


async def run_forever() -> None:
    logger.info("starting awg-client-side")
    while True:
        try:
            settings = load_client_settings()
            await run_once()
            logger.info("sleeping interval_seconds=%s", settings.interval_seconds)
            await asyncio.sleep(settings.interval_seconds)
        except (FileNotFoundError, ValidationError, ValueError) as exc:
            logger.exception("settings error: %s", exc)
            await asyncio.sleep(60)


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
