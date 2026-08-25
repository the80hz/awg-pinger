# VPN Pinger (AmneziaWG + OpenVPN)

Two small services for periodic AmneziaWG and OpenVPN tunnel checks.

## Services

`awg-client-side` runs near the VPN configs. It periodically:

1. loads `data/settings.json`;
2. brings each tunnel up with `awg-quick` or `openvpn`;
3. pings the configured host through the tunnel;
4. brings the tunnel down;
5. posts the result to `api-side`.

`api-side` can run elsewhere. It receives check results and exposes the latest status.

## API Clients

Create `api-data/clients.json` next to `compose.yml` on the API host:

```json
{
  "clients": [
    {
      "client_id": "vps-checker-1",
      "secret": "change-me-to-a-long-random-secret-at-least-32-chars",
      "enabled": true
    }
  ]
}
```

Each client should have a unique `client_id` and a unique random secret. To revoke one checker, set `enabled` to `false` or remove it.

## Client Configuration

Create `data/settings.json` next to `compose.yml`:

```json
{
  "client_id": "vps-checker-1",
  "client_secret": "change-me-to-a-long-random-secret-at-least-32-chars",
  "api_base_url": "https://api.example.com",
  "interval_seconds": 1800,
  "request_timeout_seconds": 30,
  "servers": [
    {
      "id": "vps-1",
      "name": "VPS 1",
      "type": "awg",
      "config": "vps-1.conf",
      "ping_host": "10.8.0.1",
      "ping_count": 3,
      "timeout_seconds": 20
    },
    {
      "id": "openvpn-1",
      "name": "OpenVPN 1",
      "type": "openvpn",
      "config": "openvpn-1.ovpn",
      "ping_host": "1.1.1.1",
      "ping_count": 3,
      "timeout_seconds": 60
    }
  ]
}
```

Put matching tunnel configs in the same `data/` directory:

```text
data/
  settings.json
  vps-1.conf
  openvpn-1.ovpn
```

Cycles are aligned to wall-clock slots: with `interval_seconds` of 1800 a checker
runs at :00/:30. `schedule_offset_seconds` shifts those slots, so two checkers can
run in anti-phase — leave it `0` on one checker and set `900` on the other to get
:00/:30 and :15/:45. It defaults to `0`, so existing settings keep running on :00/:30.

`type` can be `awg` (the default, so existing settings remain valid) or `openvpn`.
AWG entries use `.conf`; OpenVPN entries use `.ovpn`. Config paths must be file names
inside `data/`—absolute paths are intentionally rejected because the directory is
mounted into the container as `/app/data`.

For example, copy a local profile into the private `data/` directory before starting:

```bash
cp '/Users/the80hz/Library/Mobile Documents/com~apple~CloudDocs/az-gen1.ovpn' data/az-gen1.ovpn
```

Then configure it with `"type": "openvpn"` and `"config": "az-gen1.ovpn"`.
Both `data/*.ovpn` and `data/*.conf` are ignored by git because they commonly contain
private keys.

## Run Together Locally

```bash
docker compose up --build
```

With the default example `api_base_url`, the client reports to `http://api-side:8000`.

## Run Separately

Run only the API:

```bash
docker compose up --build api-side
```

Run only the client after setting `api_base_url` in `data/settings.json` to the remote API URL:

```bash
docker compose up --build awg-client-side
```

The API service needs `api-data/clients.json`.
The client container needs `/dev/net/tun`, `NET_ADMIN`, and host kernel support for AmneziaWG.
The image includes OpenVPN, builds `awg` and `awg-quick` from `amnezia-vpn/amneziawg-tools`, and includes `amneziawg-go` as a userspace fallback when the host kernel does not support `type amneziawg`.

## Failure Reasons

Failed checks report a classified, human-readable reason instead of the raw
command error: the checker distinguishes its own broken DNS (e.g. an empty
`/etc/resolv.conf` left behind by a tunnel teardown), an unresolvable endpoint
hostname, a server that never completes the WireGuard handshake, a tunnel that
comes up but forwards no traffic (100% packet loss or DNS dead inside the
tunnel), and OpenVPN-specific causes (unresolvable server address, TCP/UDP
connection timeouts, AUTH/TLS failures).

Before each cycle the client verifies its own DNS; when it is broken, every
failure report is prefixed with a note that the checker itself — not the VPN
server — is unhealthy. The client also snapshots `resolv.conf` before each
check and restores it if a tunnel teardown leaves it without nameservers, and
cleans up tunnel interfaces left behind by interrupted checks.

Related environment variables (all optional):

- `RESOLV_CONF_PATH` — resolv.conf location to guard (default `/etc/resolv.conf`);
- `DNS_PROBE_HOST` — hostname resolved during the pre-cycle DNS self-check (default `google.com`);
- `AWG_CMD` — `awg` binary used to query handshake state after a failed ping (default `awg`).

## Client Logs

`awg-client-side` logs each tunnel check step. For detailed diagnostics, run with:

```bash
LOG_LEVEL=DEBUG docker compose up awg-client-side
```

This prints `awg-quick` or OpenVPN lifecycle events, `ping`, API response status, and command output on failures.

When both services run in the same compose project, set client `api_base_url` to `http://api-side:8000`. Inside a container, `localhost` points to that same container, not to `api-side`.

## Telegram Alerts

`api-side` can send Telegram notifications when a received check result has `ok: false`.

Create `api-data/telegram.json` on the API host:

```json
{
  "bot_token": "123456:telegram-bot-token",
  "chat_id": "123456789"
}
```

The file is mounted read-only into `api-side` and is ignored by git. You can also set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as environment variables; if both are present, they override `api-data/telegram.json`.

If no Telegram config is present, notifications are disabled. Telegram delivery errors are logged, but they do not reject incoming check results.

## Request Security

`POST /checks` requires HMAC-SHA256 request signing. The client signs the exact JSON body with its `client_secret`.

Required headers:

```text
X-Client-Id: vps-checker-1
X-Timestamp: 2026-05-27T10:00:00Z
X-Nonce: random-value-used-once
X-Signature: hmac_sha256_hex(secret, timestamp + "\n" + nonce + "\n" + body)
```

The API checks:

- `client_id` exists and is enabled in `api-data/clients.json`;
- `X-Client-Id` matches `client_id` in the JSON body;
- timestamp is within a 2 minute window;
- nonce was not used before in that window;
- signature matches the body exactly;
- request body is at most 64 KiB.

Use HTTPS in front of `api-side` when it runs outside a private Docker network. The HMAC prevents body tampering and replay, but HTTPS still protects metadata and secrets from network observers.

## API Endpoints

Health:

```bash
curl http://localhost:8000/health
```

Receive check result:

```bash
curl -X POST http://localhost:8000/checks \
  -H 'Content-Type: application/json' \
  -H 'X-Client-Id: vps-checker-1' \
  -H 'X-Timestamp: 2026-05-27T10:00:00Z' \
  -H 'X-Nonce: unique-random-value-123' \
  -H 'X-Signature: <signature>' \
  -d '{"client_id":"vps-checker-1","server_id":"vps-1","ok":true,"comment":"tunnel is reachable"}'
```

List latest results:

```bash
curl http://localhost:8000/checks/latest
```

Get latest result for one server:

```bash
curl http://localhost:8000/checks/latest/vps-checker-1/vps-1
```
