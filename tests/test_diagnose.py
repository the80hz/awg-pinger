from awg_client_side.diagnose import describe_failure
from shared.settings import ServerConfig


AWG_SERVER = ServerConfig(id="tr", name="AWG TR", config="tr.conf", ping_host="google.com")
AWG_SERVER_IP_PING = ServerConfig(id="fr", name="AWG FR", config="fr.conf", ping_host="1.1.1.1")
OVPN_SERVER = ServerConfig(
    id="naeb", name="AZ naeb", type="openvpn", config="naeb.ovpn", ping_host="1.0.0.1"
)

AWG_UP_DNS_OUTPUT = """\
[#] amneziawg-go vps-2-tr
[#] awg setconf vps-2-tr /dev/fd/63
Temporary failure in name resolution: `turk.gruz200.uk:55424'. Trying again in 1.00 seconds...
Temporary failure in name resolution: `turk.gruz200.uk:55424'. Trying again in 1.20 seconds...
"""

OVPN_DNS_OUTPUT = """\
2026-08-25 13:50:47 RESOLVE: Cannot resolve host address: naebalova.net:50443 (Temporary failure in name resolution)
2026-08-25 13:50:47 Could not determine IPv4/IPv6 protocol
2026-08-25 13:50:51 RESOLVE: Cannot resolve host address: naebalova.net:504 (Temporary failure in name resolution)
"""

OVPN_UNREACHABLE_OUTPUT = """\
2026-08-25 13:03:25 Server poll timeout, restarting
2026-08-25 13:03:35 TCP: connect to [AF_INET]109.73.204.170:50443 failed: Connection timed out
2026-08-25 13:03:46 TCP: connect to [AF_INET]109.73.204.170:443 failed: Connection timed out
"""


def test_tunnel_up_dns_failure_names_endpoint_and_blames_checker() -> None:
    comment = describe_failure(
        "tunnel-up", "command timed out: awg-quick up /app/data/tr.conf", AWG_UP_DNS_OUTPUT, AWG_SERVER
    )
    assert "cannot resolve VPN endpoint turk.gruz200.uk:55424" in comment
    assert "checker-side problem" in comment
    assert "command timed out: awg-quick up /app/data/tr.conf" in comment


def test_tunnel_up_leftover_interface() -> None:
    comment = describe_failure(
        "tunnel-up",
        "command failed: awg-quick up /app/data/tr.conf",
        "[#] ip link add vps-2-tr type wireguard\nRTNETLINK answers: File exists",
        AWG_SERVER,
    )
    assert "previous check left the interface" in comment


def test_ping_dns_hang_without_handshake_blames_server() -> None:
    comment = describe_failure(
        "ping",
        "command timed out: ping -c 3 -W 10 google.com",
        "",
        AWG_SERVER,
        no_handshake=True,
    )
    assert "no WireGuard handshake" in comment
    assert "DNS lookup of 'google.com' through the tunnel never answered" in comment
    assert "down, unreachable, or the keys are wrong" in comment


def test_ping_packet_loss_with_handshake() -> None:
    comment = describe_failure(
        "ping",
        "command failed: ping -c 3 -W 10 1.1.1.1",
        "PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.\n\n--- 1.1.1.1 ping statistics ---\n"
        "3 packets transmitted, 0 received, 100% packet loss, time 2036ms",
        AWG_SERVER_IP_PING,
        no_handshake=False,
    )
    assert "handshake with the VPN server succeeded" in comment
    assert "lost 100% of packets" in comment


def test_ping_failure_without_handshake_info() -> None:
    comment = describe_failure(
        "ping",
        "command failed: ping -c 3 -W 10 1.1.1.1",
        "PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.\n\n--- 1.1.1.1 ping statistics ---\n"
        "3 packets transmitted, 0 received, 100% packet loss, time 2036ms",
        AWG_SERVER_IP_PING,
    )
    assert comment.startswith("tunnel came up but no traffic goes through it")


def test_openvpn_dns_failure_names_host() -> None:
    comment = describe_failure(
        "openvpn", "openvpn tunnel startup timed out", OVPN_DNS_OUTPUT, OVPN_SERVER
    )
    assert "cannot resolve server address naebalova.net:50443, naebalova.net:504" in comment
    assert "DNS is broken on the checker" in comment


def test_openvpn_server_unreachable() -> None:
    comment = describe_failure(
        "openvpn", "openvpn tunnel startup timed out", OVPN_UNREACHABLE_OUTPUT, OVPN_SERVER
    )
    assert "TCP connection to 109.73.204.170:50443, 109.73.204.170:443 timed out" in comment


def test_unknown_stage_keeps_original_comment() -> None:
    comment = describe_failure(None, "config file not found: tr.conf", None, AWG_SERVER)
    assert comment == "config file not found: tr.conf"
