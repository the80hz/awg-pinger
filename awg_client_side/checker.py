import asyncio
import logging
import os
import time
from pathlib import Path

from shared.schemas import CheckResult
from shared.settings import ServerConfig


logger = logging.getLogger("awg-client-side.checker")


class TunnelCheckError(Exception):
    def __init__(self, comment: str, command_output: str | None = None) -> None:
        super().__init__(comment)
        self.comment = comment
        self.command_output = command_output


def config_path_for(settings_path: Path, server: ServerConfig) -> Path:
    data_dir = settings_path.parent.resolve()
    config_path = (data_dir / server.config).resolve()
    if data_dir not in config_path.parents:
        raise TunnelCheckError("config path escapes data directory")
    if not config_path.exists():
        raise TunnelCheckError(f"config file not found: {server.config}")
    return config_path


async def _run(command: list[str], timeout: int) -> str:
    command_text = " ".join(command)
    logger.debug("running command timeout=%ss command=%s", timeout, command_text)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        logger.error("command not found command=%s", command[0])
        raise TunnelCheckError(f"command not found: {command[0]}") from exc
    except PermissionError as exc:
        logger.error("permission denied command=%s", command[0])
        raise TunnelCheckError(f"permission denied running command: {command[0]}") from exc

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        logger.error("command timed out command=%s", command_text)
        raise TunnelCheckError(f"command timed out: {command_text}") from exc

    output = stdout.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        logger.error(
            "command failed returncode=%s command=%s output=%s",
            process.returncode,
            command_text,
            output,
        )
        raise TunnelCheckError(f"command failed: {command_text}", output)
    logger.debug("command completed command=%s output=%s", command_text, output)
    return output


async def check_tunnel(settings_path: Path, client_id: str, server: ServerConfig) -> CheckResult:
    config_path = config_path_for(settings_path, server)
    started_at = time.monotonic()
    tunnel_quick_cmd = os.getenv("TUNNEL_QUICK_CMD", "awg-quick")
    interface_is_up = False
    logger.info(
        "starting tunnel check client_id=%s server_id=%s config=%s ping_host=%s",
        client_id,
        server.id,
        config_path,
        server.ping_host,
    )

    try:
        await _run([tunnel_quick_cmd, "up", str(config_path)], server.timeout_seconds)
        interface_is_up = True
        logger.info("tunnel is up server_id=%s", server.id)
        ping_output = await _run(
            [
                "ping",
                "-c",
                str(server.ping_count),
                "-W",
                str(max(1, min(server.timeout_seconds, 10))),
                server.ping_host,
            ],
            server.timeout_seconds,
        )
        duration_ms = round((time.monotonic() - started_at) * 1000)
        logger.info("tunnel check succeeded server_id=%s duration_ms=%s", server.id, duration_ms)
        return CheckResult(
            client_id=client_id,
            server_id=server.id,
            server_name=server.name,
            ok=True,
            comment="tunnel is reachable",
            duration_ms=duration_ms,
            ping_output=ping_output,
        )
    except TunnelCheckError as exc:
        duration_ms = round((time.monotonic() - started_at) * 1000)
        logger.warning(
            "tunnel check failed server_id=%s duration_ms=%s comment=%s output=%s",
            server.id,
            duration_ms,
            exc.comment,
            exc.command_output,
        )
        return CheckResult(
            client_id=client_id,
            server_id=server.id,
            server_name=server.name,
            ok=False,
            comment=exc.comment,
            duration_ms=duration_ms,
            command_output=exc.command_output,
        )
    finally:
        if interface_is_up:
            try:
                logger.info("bringing tunnel down server_id=%s", server.id)
                await _run([tunnel_quick_cmd, "down", str(config_path)], server.timeout_seconds)
            except TunnelCheckError:
                logger.exception("failed to bring tunnel down server_id=%s", server.id)
