"""Turn raw tunnel-check failures into human-readable causes.

The raw failures ("command timed out: awg-quick up ...") hide the actual
problem, which is almost always one of a handful of cases: broken DNS on the
checker itself, an endpoint hostname that does not resolve, a server that
never answers the handshake, or a tunnel that comes up but forwards nothing.
"""

import ipaddress
import re

from shared.settings import ServerConfig


# `Temporary failure in name resolution: `turk.gruz200.uk:55424'. Trying again ...`
_AWG_RESOLVE_RE = re.compile(
    r"(?:Temporary failure in name resolution|Name or service not known|"
    r"Name does not resolve): `([^']+)'"
)
# `RESOLVE: Cannot resolve host address: naebalova.net:50443 (...)`
_OVPN_RESOLVE_RE = re.compile(r"Cannot resolve host address: (\S+)")
# `TCP: connect to [AF_INET]109.73.204.170:443 failed: Connection timed out`
_OVPN_CONNECT_RE = re.compile(r"connect to \[AF_INET6?\](\S+) failed: ([^\n]+)")


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _describe_tunnel_up(comment: str, output: str, server: ServerConfig) -> str:
    hosts = _unique(_AWG_RESOLVE_RE.findall(output))
    if hosts:
        return (
            f"cannot resolve VPN endpoint {', '.join(hosts)} — DNS lookup failed on the "
            f"checker before the tunnel came up; this is a checker-side problem, the VPN "
            f"server was never reached ({comment})"
        )
    if "File exists" in output or "Address already in use" in output:
        return (
            f"cannot create the tunnel interface — a previous check left the interface or "
            f"its address behind; it will be cleaned up before the next attempt ({comment})"
        )
    if "Operation not permitted" in output:
        return (
            f"cannot create the tunnel interface — the container lacks NET_ADMIN "
            f"privileges ({comment})"
        )
    return f"tunnel did not come up within {server.timeout_seconds}s ({comment})"


def _describe_ping(
    comment: str,
    output: str,
    server: ServerConfig,
    no_handshake: bool | None,
) -> str:
    ping_started = "PING" in output
    dns_failed = "failure in name resolution" in output or (
        not ping_started and not _is_ip_address(server.ping_host)
    )

    if dns_failed:
        detail = (
            f"DNS lookup of '{server.ping_host}' through the tunnel never answered"
            if not ping_started
            else f"cannot resolve '{server.ping_host}' through the tunnel"
        )
    elif "100% packet loss" in output:
        detail = f"ping to '{server.ping_host}' lost 100% of packets"
    elif "Network is unreachable" in output or "Destination Host Unreachable" in output:
        detail = f"no route to '{server.ping_host}' through the tunnel"
    else:
        detail = f"ping to '{server.ping_host}' through the tunnel failed ({comment})"

    if no_handshake is True:
        return (
            f"tunnel came up but the VPN server is not responding: no WireGuard handshake "
            f"completed and {detail} — the server is down, unreachable, or the keys are wrong"
        )
    if no_handshake is False:
        return (
            f"handshake with the VPN server succeeded, but {detail} — the server accepts "
            f"the connection and then does not forward traffic"
        )
    return f"tunnel came up but no traffic goes through it: {detail}"


def _describe_openvpn(comment: str, output: str) -> str:
    problems: list[str] = []

    hosts = _unique(_OVPN_RESOLVE_RE.findall(output))
    if hosts:
        problems.append(
            f"cannot resolve server address {', '.join(hosts)} — DNS is broken on the "
            f"checker, the VPN server was never reached"
        )

    failed_connects = _OVPN_CONNECT_RE.findall(output)
    timed_out = _unique(addr for addr, why in failed_connects if "timed out" in why)
    refused = _unique(addr for addr, why in failed_connects if "refused" in why)
    if timed_out:
        problems.append(
            f"TCP connection to {', '.join(timed_out)} timed out — server unreachable, "
            f"down, or the port is blocked"
        )
    if refused:
        problems.append(f"connection to {', '.join(refused)} refused — nothing listens on that port")
    if "Server poll timeout" in output and not timed_out:
        problems.append("no reply from the server over UDP — server down or the port is blocked")
    if "AUTH_FAILED" in output or "auth-failure" in output:
        problems.append("server rejected the credentials (AUTH_FAILED)")
    if "TLS Error" in output or "TLS handshake failed" in output:
        problems.append("TLS handshake failed — wrong certificates or the traffic is being filtered")

    if problems:
        return f"openvpn could not connect: {'; '.join(problems)} ({comment})"
    return comment


def describe_failure(
    stage: str | None,
    comment: str,
    output: str | None,
    server: ServerConfig,
    no_handshake: bool | None = None,
) -> str:
    output = output or ""
    if stage == "tunnel-up":
        return _describe_tunnel_up(comment, output, server)
    if stage == "ping":
        return _describe_ping(comment, output, server, no_handshake)
    if stage == "openvpn":
        return _describe_openvpn(comment, output)
    return comment
