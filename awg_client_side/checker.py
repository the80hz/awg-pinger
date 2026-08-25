import asyncio
import contextlib
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

from awg_client_side.diagnose import describe_failure
from shared.schemas import CheckResult
from shared.settings import ServerConfig


logger = logging.getLogger("awg-client-side.checker")


class TunnelCheckError(Exception):
    def __init__(
        self,
        comment: str,
        command_output: str | None = None,
        stage: str | None = None,
    ) -> None:
        super().__init__(comment)
        self.comment = comment
        self.command_output = command_output
        self.stage = stage
        self.no_handshake: bool | None = None


def resolv_conf_path() -> Path:
    return Path(os.getenv("RESOLV_CONF_PATH", "/etc/resolv.conf"))


def read_resolv_conf() -> str | None:
    try:
        return resolv_conf_path().read_text(encoding="utf-8")
    except OSError:
        return None


def restore_resolv_conf(saved: str | None) -> None:
    """Put resolv.conf back if a tunnel up/down cycle clobbered it.

    awg-quick manages DNS through resolvconf when the config has a DNS= line;
    an interrupted teardown can leave /etc/resolv.conf empty, which breaks every
    later check on this machine until someone fixes the file by hand.
    """
    if not saved or "nameserver" not in saved:
        return
    current = read_resolv_conf()
    if current == saved or (current is not None and "nameserver" in current):
        return
    try:
        resolv_conf_path().write_text(saved, encoding="utf-8")
        logger.warning("restored %s after tunnel check left it without nameservers", resolv_conf_path())
    except OSError as exc:
        logger.error("failed to restore %s: %s", resolv_conf_path(), exc)


def config_path_for(settings_path: Path, server: ServerConfig) -> Path:
    data_dir = settings_path.parent.resolve()
    config_path = (data_dir / server.config).resolve()
    if data_dir not in config_path.parents:
        raise TunnelCheckError("config path escapes data directory")
    if not config_path.exists():
        raise TunnelCheckError(f"config file not found: {server.config}")
    return config_path


async def _run(command: list[str], timeout: int, stage: str | None = None) -> str:
    command_text = " ".join(command)
    logger.debug("running command timeout=%ss command=%s", timeout, command_text)
    with tempfile.TemporaryFile() as output_file:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=output_file,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            logger.error("command not found command=%s", command[0])
            raise TunnelCheckError(f"command not found: {command[0]}", stage=stage) from exc
        except PermissionError as exc:
            logger.error("permission denied command=%s", command[0])
            raise TunnelCheckError(
                f"permission denied running command: {command[0]}", stage=stage
            ) from exc

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            output = _read_output_file(output_file)
            logger.error("command timed out command=%s output=%s", command_text, output)
            raise TunnelCheckError(f"command timed out: {command_text}", output, stage=stage) from exc

        output = _read_output_file(output_file)

    if process.returncode != 0:
        logger.error(
            "command failed returncode=%s command=%s output=%s",
            process.returncode,
            command_text,
            output,
        )
        raise TunnelCheckError(f"command failed: {command_text}", output, stage=stage)
    logger.debug("command completed command=%s output=%s", command_text, output)
    return output


async def _ping(server: ServerConfig) -> str:
    return await _run(
        [
            "ping",
            "-c",
            str(server.ping_count),
            "-W",
            str(max(1, min(server.timeout_seconds, 10))),
            server.ping_host,
        ],
        server.timeout_seconds,
        stage="ping",
    )


def _read_output_file(output_file) -> str:
    output_file.seek(0)
    return output_file.read().decode("utf-8", errors="replace").strip()


def _read_live_output_file(output_file: BinaryIO) -> str:
    size = os.fstat(output_file.fileno()).st_size
    return os.pread(output_file.fileno(), size, 0).decode("utf-8", errors="replace").strip()


async def _wait_for_openvpn(
    process: asyncio.subprocess.Process,
    output_file: BinaryIO,
    timeout: int,
) -> None:
    deadline = time.monotonic() + timeout
    ready_marker = "Initialization Sequence Completed"

    while time.monotonic() < deadline:
        output = _read_live_output_file(output_file)
        if ready_marker in output:
            return
        if process.returncode is not None:
            await process.wait()
            raise TunnelCheckError("openvpn exited before tunnel became ready", output, stage="openvpn")
        await asyncio.sleep(0.1)

    raise TunnelCheckError(
        "openvpn tunnel startup timed out", _read_live_output_file(output_file), stage="openvpn"
    )


async def _stop_openvpn(process: asyncio.subprocess.Process, timeout: int) -> None:
    if process.returncode is not None:
        await process.wait()
        return

    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=min(timeout, 10))
    except asyncio.TimeoutError:
        logger.warning("openvpn did not stop after SIGTERM; killing it")
        process.kill()
        await process.wait()


async def _cleanup_leftover_interface(
    tunnel_quick_cmd: str, config_path: Path, server: ServerConfig
) -> None:
    interface = config_path.stem
    if not Path("/sys/class/net", interface).exists():
        return
    logger.warning(
        "leftover tunnel interface found from a previous check; bringing it down "
        "interface=%s server_id=%s",
        interface,
        server.id,
    )
    try:
        await _run([tunnel_quick_cmd, "down", str(config_path)], server.timeout_seconds)
    except TunnelCheckError:
        logger.warning("failed to clean up leftover interface interface=%s", interface)


async def _no_handshake(config_path: Path, server: ServerConfig) -> bool | None:
    """True if the peer never completed a handshake on the freshly created tunnel."""
    awg_cmd = os.getenv("AWG_CMD", "awg")
    try:
        output = await _run(
            [awg_cmd, "show", config_path.stem, "latest-handshakes"], 10, stage="diagnostics"
        )
    except TunnelCheckError:
        return None
    timestamps = [
        parts[-1] for line in output.splitlines() if (parts := line.split()) and parts[-1].isdigit()
    ]
    if not timestamps:
        return None
    return all(int(timestamp) == 0 for timestamp in timestamps)


async def _check_awg(config_path: Path, server: ServerConfig) -> str:
    tunnel_quick_cmd = os.getenv("TUNNEL_QUICK_CMD", "awg-quick")
    await _cleanup_leftover_interface(tunnel_quick_cmd, config_path, server)
    attempted_up = False
    try:
        attempted_up = True
        await _run(
            [tunnel_quick_cmd, "up", str(config_path)], server.timeout_seconds, stage="tunnel-up"
        )
        logger.info("AWG tunnel is up server_id=%s", server.id)
        try:
            return await _ping(server)
        except TunnelCheckError as exc:
            if exc.stage == "ping":
                exc.no_handshake = await _no_handshake(config_path, server)
            raise
    finally:
        if attempted_up:
            try:
                logger.info("bringing AWG tunnel down server_id=%s", server.id)
                await _run([tunnel_quick_cmd, "down", str(config_path)], server.timeout_seconds)
            except TunnelCheckError:
                logger.warning("failed to bring AWG tunnel down server_id=%s", server.id)


async def _check_openvpn(config_path: Path, server: ServerConfig) -> str:
    openvpn_cmd = os.getenv("OPENVPN_CMD", "openvpn")
    output_file = tempfile.TemporaryFile()
    process: asyncio.subprocess.Process | None = None
    try:
        command = [openvpn_cmd, "--config", str(config_path)]
        logger.debug("starting OpenVPN command=%s", " ".join(command))
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=output_file,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise TunnelCheckError(f"command not found: {openvpn_cmd}", stage="openvpn") from exc
        except PermissionError as exc:
            raise TunnelCheckError(
                f"permission denied running command: {openvpn_cmd}", stage="openvpn"
            ) from exc

        await _wait_for_openvpn(process, output_file, server.timeout_seconds)
        logger.info("OpenVPN tunnel is up server_id=%s", server.id)
        return await _ping(server)
    finally:
        if process is not None:
            logger.info("bringing OpenVPN tunnel down server_id=%s", server.id)
            await _stop_openvpn(process, server.timeout_seconds)
        output_file.close()


async def check_tunnel(
    settings_path: Path,
    client_id: str,
    server: ServerConfig,
    local_dns_problem: str | None = None,
) -> CheckResult:
    config_path = config_path_for(settings_path, server)
    started_at = time.monotonic()
    logger.info(
        "starting tunnel check client_id=%s server_id=%s type=%s config=%s ping_host=%s",
        client_id,
        server.id,
        server.tunnel_type,
        config_path,
        server.ping_host,
    )
    saved_resolv_conf = read_resolv_conf()

    try:
        if server.tunnel_type == "openvpn":
            ping_output = await _check_openvpn(config_path, server)
        else:
            ping_output = await _check_awg(config_path, server)
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
        comment = describe_failure(
            exc.stage, exc.comment, exc.command_output, server, exc.no_handshake
        )
        if local_dns_problem:
            comment = (
                f"checker itself is unhealthy: {local_dns_problem} — this failure is most "
                f"likely NOT the VPN server's fault. {comment}"
            )
        logger.warning(
            "tunnel check failed server_id=%s duration_ms=%s stage=%s comment=%s output=%s",
            server.id,
            duration_ms,
            exc.stage,
            comment,
            exc.command_output,
        )
        return CheckResult(
            client_id=client_id,
            server_id=server.id,
            server_name=server.name,
            ok=False,
            comment=comment,
            duration_ms=duration_ms,
            command_output=exc.command_output,
        )
    finally:
        restore_resolv_conf(saved_resolv_conf)
