# llm-gateway

A self-hosted API gateway for [llama.cpp](https://github.com/ggerganov/llama.cpp) (or any OpenAI-compatible backend). Adds bearer token auth, concurrency limiting with queue tracking, and per-user token metrics — then exposes it publicly via [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) with no open router ports.

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/hcastc00/llm-gateway/main/install.sh)
```

The installer will:
1. Install Docker if needed (Linux only; macOS requires [Docker Desktop](https://docs.docker.com/desktop/))
2. Prompt for your upstream LLM URL, concurrency limit, API keys, and Cloudflare Tunnel token
3. Write `.env` and `users.json`
4. Build and start the containers

## How it works

```
Cloudflare Edge → cloudflared → gateway :8000 → llama.cpp :8001
                                    ↑
                              also on LAN at host-ip:8000
```

- **No open ports** — `cloudflared` connects outbound to Cloudflare. Configure your public hostname in the [Zero Trust dashboard](https://one.dash.cloudflare.com/): `Service: http://gateway:8000`
- **LAN access** — `http://<server-ip>:8000` works directly on your local network
- **Bearer auth** — every `/v1/*` request requires a valid API key from `users.json`
- **Concurrency queue** — `MAX_CONCURRENT` limits simultaneous upstream requests; streaming clients receive their queue position immediately via SSE

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
UPSTREAM_URL=http://192.168.1.46:8001   # your llama.cpp server
MAX_CONCURRENT=1                         # max parallel requests
TUNNEL_TOKEN=your_cloudflare_token_here  # from Zero Trust dashboard
```

`users.json` maps API keys to usernames:

```json
{"sk-abc123": "alice", "sk-xyz789": "bob"}
```

## Managing users

```bash
./users.sh list                   # show all users and keys
./users.sh add                    # add interactively
./users.sh add sk-newkey username # add inline
./users.sh remove username        # remove all keys for a user
```

Changes take effect immediately — no restart needed.

## Endpoints

| Endpoint | Auth | Description |
| -------- | ---- | ----------- |
| `GET /health` | No | Liveness check |
| `GET /metrics` | No | Queue depth, TPS, latency, per-user token totals |
| `/v1/*` | Bearer | Proxied to upstream |

## Running manually (without the installer)

```bash
# Start with Docker Compose
docker compose up --build

# Run locally for development
pip install -r gateway/requirements.txt
UPSTREAM_URL=http://localhost:8001 USERS_FILE=./users.json uvicorn gateway.main:app --reload --port 8000
```
