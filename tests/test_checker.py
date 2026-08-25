import asyncio

from awg_client_side import checker, main
from shared.settings import ServerConfig


def test_check_tunnel_uses_openvpn_driver(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "profile.ovpn"
    config_path.write_text("client\ndev tun\n", encoding="utf-8")
    server = ServerConfig(
        id="openvpn-1",
        type="openvpn",
        config=config_path.name,
        ping_host="1.1.1.1",
    )
    calls = []

    async def fake_check_openvpn(path, checked_server):
        calls.append((path, checked_server))
        return "ping succeeded"

    async def fail_if_awg_called(path, checked_server):
        raise AssertionError("AWG driver must not be used for an OpenVPN profile")

    monkeypatch.setattr(checker, "_check_openvpn", fake_check_openvpn)
    monkeypatch.setattr(checker, "_check_awg", fail_if_awg_called)

    result = asyncio.run(checker.check_tunnel(tmp_path / "settings.json", "client-1", server))

    assert result.ok is True
    assert result.ping_output == "ping succeeded"
    assert calls == [(config_path, server)]


def test_check_tunnel_reports_openvpn_failure(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "profile.ovpn"
    config_path.write_text("client\ndev tun\n", encoding="utf-8")
    server = ServerConfig(
        id="openvpn-1",
        type="openvpn",
        config=config_path.name,
        ping_host="1.1.1.1",
    )

    async def fake_check_openvpn(path, checked_server):
        raise checker.TunnelCheckError("openvpn tunnel startup timed out", "connection failed")

    monkeypatch.setattr(checker, "_check_openvpn", fake_check_openvpn)

    result = asyncio.run(checker.check_tunnel(tmp_path / "settings.json", "client-1", server))

    assert result.ok is False
    assert result.comment == "openvpn tunnel startup timed out"
    assert result.command_output == "connection failed"


def test_check_tunnel_prefixes_local_dns_problem(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "profile.ovpn"
    config_path.write_text("client\ndev tun\n", encoding="utf-8")
    server = ServerConfig(
        id="openvpn-1",
        type="openvpn",
        config=config_path.name,
        ping_host="1.1.1.1",
    )

    async def fake_check_openvpn(path, checked_server):
        raise checker.TunnelCheckError("openvpn tunnel startup timed out", "connection failed")

    monkeypatch.setattr(checker, "_check_openvpn", fake_check_openvpn)

    result = asyncio.run(
        checker.check_tunnel(
            tmp_path / "settings.json",
            "client-1",
            server,
            local_dns_problem="/etc/resolv.conf has no nameserver entries",
        )
    )

    assert result.ok is False
    assert result.comment.startswith(
        "checker itself is unhealthy: /etc/resolv.conf has no nameserver entries"
    )
    assert "NOT the VPN server's fault" in result.comment
    assert "openvpn tunnel startup timed out" in result.comment


def test_check_tunnel_restores_wiped_resolv_conf(monkeypatch, tmp_path) -> None:
    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text("nameserver 127.0.0.11\n", encoding="utf-8")
    monkeypatch.setenv("RESOLV_CONF_PATH", str(resolv_conf))

    config_path = tmp_path / "profile.ovpn"
    config_path.write_text("client\ndev tun\n", encoding="utf-8")
    server = ServerConfig(
        id="openvpn-1",
        type="openvpn",
        config=config_path.name,
        ping_host="1.1.1.1",
    )

    async def wiping_check_openvpn(path, checked_server):
        resolv_conf.write_text("", encoding="utf-8")
        return "ping succeeded"

    monkeypatch.setattr(checker, "_check_openvpn", wiping_check_openvpn)

    result = asyncio.run(checker.check_tunnel(tmp_path / "settings.json", "client-1", server))

    assert result.ok is True
    assert resolv_conf.read_text(encoding="utf-8") == "nameserver 127.0.0.11\n"


def test_detect_local_dns_problem_reports_empty_resolv_conf(monkeypatch, tmp_path) -> None:
    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text("   \n\n", encoding="utf-8")
    monkeypatch.setenv("RESOLV_CONF_PATH", str(resolv_conf))

    problem = asyncio.run(main.detect_local_dns_problem())

    assert problem is not None
    assert "no nameserver entries" in problem


def test_seconds_until_next_slot_on_slot_returns_zero() -> None:
    assert main.seconds_until_next_slot(3600.0, 1800, 0) == 0.0
    assert main.seconds_until_next_slot(4500.0, 1800, 900) == 0.0


def test_seconds_until_next_slot_offset_zero_waits_to_next() -> None:
    # 100s past :00 -> 1700s left until :30
    assert main.seconds_until_next_slot(3700.0, 1800, 0) == 1700.0


def test_seconds_until_next_slot_anti_phase_offset() -> None:
    # checker-2 at offset 900 is 15 min out of phase with checker-1
    assert main.seconds_until_next_slot(3600.0, 1800, 900) == 900.0
    assert main.seconds_until_next_slot(3600.0, 1800, 0) == 0.0


def test_detect_local_dns_problem_ok(monkeypatch, tmp_path) -> None:
    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text("nameserver 127.0.0.11\n", encoding="utf-8")
    monkeypatch.setenv("RESOLV_CONF_PATH", str(resolv_conf))

    async def fake_getaddrinfo(host, port):
        return []

    class FakeLoop:
        getaddrinfo = staticmethod(fake_getaddrinfo)

    monkeypatch.setattr(main.asyncio, "get_running_loop", lambda: FakeLoop())

    problem = asyncio.run(main.detect_local_dns_problem())

    assert problem is None
