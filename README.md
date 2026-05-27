# AWG Pinger

Two small services for periodic AWG tunnel checks.

## Services

`awg-client-side` runs near the AWG configs. It periodically:

1. loads `data/settings.json`;
2. brings each tunnel up with `awg-quick`;
3. pings the configured host through the tunnel;
4. brings the tunnel down;
5. posts the result to `api-side`.

`api-side` can run elsewhere. It receives check results and exposes the latest status.

## Client Configuration

Create `data/settings.json` next to `compose.yml`:

```json
{
  "client_id": "vps-checker-1",
  "api_base_url": "https://api.example.com",
  "interval_seconds": 1800,
  "request_timeout_seconds": 30,
  "servers": [
    {
      "id": "vps-1",
      "name": "VPS 1",
      "config": "vps-1.conf",
      "ping_host": "10.8.0.1",
      "ping_count": 3,
      "timeout_seconds": 20
    }
  ]
}
```

Put matching tunnel configs in the same `data/` directory:

```text
data/
  settings.json
  vps-1.conf
```

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

The client container needs `/dev/net/tun`, `NET_ADMIN`, and host kernel support for AmneziaWG.
The image builds `awg` and `awg-quick` from `amnezia-vpn/amneziawg-tools`.

## Optional API Token

Set the same `API_TOKEN` environment variable on both services. If `api-side` has `API_TOKEN`, it requires:

```text
Authorization: Bearer <token>
```

## API Endpoints

Health:

```bash
curl http://localhost:8000/health
```

Receive check result:

```bash
curl -X POST http://localhost:8000/checks \
  -H 'Content-Type: application/json' \
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
