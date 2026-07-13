import asyncio

from awg_client_side import checker
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
