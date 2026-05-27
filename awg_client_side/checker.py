import asyncio
import os
import time
from pathlib import Path

from shared.schemas import CheckResult
from shared.settings import ServerConfig


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
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise TunnelCheckError(f"command not found: {command[0]}") from exc
    except PermissionError as exc:
        raise TunnelCheckError(f"permission denied running command: {command[0]}") from exc

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise TunnelCheckError(f"command timed out: {' '.join(command)}") from exc

    output = stdout.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise TunnelCheckError(f"command failed: {' '.join(command)}", output)
    return output


async def check_tunnel(settings_path: Path, client_id: str, server: ServerConfig) -> CheckResult:
    config_path = config_path_for(settings_path, server)
    started_at = time.monotonic()
    tunnel_quick_cmd = os.getenv("TUNNEL_QUICK_CMD", "awg-quick")
    interface_is_up = False

    try:
        await _run([tunnel_quick_cmd, "up", str(config_path)], server.timeout_seconds)
        interface_is_up = True
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
        return CheckResult(
            client_id=client_id,
            server_id=server.id,
            server_name=server.name,
            ok=True,
            comment="tunnel is reachable",
            duration_ms=round((time.monotonic() - started_at) * 1000),
            ping_output=ping_output,
        )
    except TunnelCheckError as exc:
        return CheckResult(
            client_id=client_id,
            server_id=server.id,
            server_name=server.name,
            ok=False,
            comment=exc.comment,
            duration_ms=round((time.monotonic() - started_at) * 1000),
            command_output=exc.command_output,
        )
    finally:
        if interface_is_up:
            try:
                await _run([tunnel_quick_cmd, "down", str(config_path)], server.timeout_seconds)
            except TunnelCheckError:
                pass
