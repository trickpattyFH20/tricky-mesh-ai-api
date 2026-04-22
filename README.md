# tricky-mesh-ai-api

Async Python daemon that bridges [MeshCore](https://meshcore.co.uk/) LoRa
direct messages to a local [llama.cpp](https://github.com/ggml-org/llama.cpp)
(or any OpenAI-compatible) chat endpoint. **RT owns the radio; this
daemon is pure HTTP.** A user DMs the observer, Remote-Terminal-for-MeshCore
decrypts the DM and webhooks it here, the daemon runs it through the LLM,
and POSTs the reply back to RT for transmission.

## Architecture

```
    MeshCore mesh (LoRa + nhmesh MQTT downlink)
             │
             ▼
    Remote-Terminal-for-MeshCore
     (owns radio, decrypts DMs)
             │
             │ POST /ingest-dm   (webhook fanout, HMAC-signed)
             ▼
    ┌───────────────────────────────────────────────┐
    │  tricky-mesh-ai-api   (FastAPI on 127.0.0.1:8091)│
    │                                               │
    │  /ingest-dm → HMAC-verify → filter PRIV+inbound
    │       → per-sender rate limit + allowlist      │
    │       → memory + rolling summary               │
    │       → llama.cpp /v1/chat/completions         │
    │       → truncate to max_reply_bytes            │
    │       → POST /api/messages/direct on RT        │
    └───────────────────────────────────────────────┘
             │
             ▼
    RT.send_direct_message → radio TX → mesh + MQTT uplink
```

No direct radio access. No ACK tracking. No traceroute. RT handles all of
that.

## Features

- **FastAPI webhook receiver** for inbound DMs from RT, with optional
  HMAC-SHA256 signature verification (`X-Webhook-Signature` header).
- **Public-key-prefix allowlist** (empty = accept any sender). MeshCore
  DMs are AES-256 end-to-end encrypted by the protocol.
- **Per-sender conversation memory** with TTL eviction and a background
  rolling summarizer so long chats stay within the model's context.
- **Per-sender rate limiting**, **dead-letter JSONL log** of dropped
  messages, and **Prometheus counters** exposed over an optional HTTP
  endpoint.
- **UTF-8-safe byte-bounded reply truncation** — replies respect the
  mesh's small-payload reality (default 140 bytes, configurable).
- **Hardened systemd user unit** (read-only home, private /tmp,
  MemoryDenyWriteExecute, restricted namespaces).

## Requirements

- Python 3.13+
- Remote-Terminal-for-MeshCore reachable over HTTP (defaults to
  `http://127.0.0.1:8000`) with a webhook fanout pointed at this
  daemon's `/ingest-dm` endpoint.
- A running OpenAI-compatible chat endpoint (llama.cpp's
  `/v1/chat/completions`, ollama's `/v1`, etc.)

## Install

```bash
git clone git@github.com:trickpattyFH20/tricky-mesh-ai-api.git
cd tricky-mesh-ai-api
uv sync --extra dev
```

## Configure

```bash
mkdir -p ~/.config/tricky-mesh-ai-api
cp config.example.yaml ~/.config/tricky-mesh-ai-api/config.yaml
$EDITOR ~/.config/tricky-mesh-ai-api/config.yaml
```

Minimum required fields:

```yaml
rt_base_url: http://127.0.0.1:8000
llama_endpoint: http://localhost:8090
allowed_pubkey_prefixes: []        # empty = accept any sender

listen_host: 127.0.0.1
listen_port: 8091                  # RT webhook POSTs here

# Strongly recommended in production. Must match RT's webhook
# fanout config's `hmac_secret` field.
ingest_hmac_secret: "<random 64+ hex chars>"
```

See [`config.example.yaml`](./config.example.yaml) for the full schema
with per-field comments.

### RT webhook fanout config (the matching side)

In the Remote-Terminal UI → Settings → Fanout → Add → **Webhook**:

| Field | Value |
|---|---|
| URL | `http://127.0.0.1:8091/ingest-dm` |
| Method | `POST` |
| HMAC Secret | *same string as `ingest_hmac_secret` in this daemon's YAML* |
| Signature Header Name | `X-Webhook-Signature` (default) |
| Message Scope | `All messages` — the daemon filters internally |

The daemon's `/ingest-dm` handler drops non-PRIV and outgoing messages
with 204 before invoking the LLM, so "All messages" is safe; the cost
is some local-only HTTP round-trips for channel/outgoing traffic.

## Run

Directly:

```bash
.venv/bin/tricky-mesh-ai --config ~/.config/tricky-mesh-ai-api/config.yaml
```

As a systemd user service (recommended):

```bash
mkdir -p ~/.config/systemd/user
ln -sf "$PWD/systemd/tricky-mesh-ai.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tricky-mesh-ai
journalctl --user -u tricky-mesh-ai -f
```

The unit declares `After=llama-server.service Wants=llama-server.service`
so llama.cpp is started first. llama-server is `Type=simple` and doesn't
signal readiness, so the daemon may receive a DM during the ~30s model
load window and dead-letter it once; subsequent DMs work.

### Full observer service chain (for reference)

This daemon is usually run alongside RT and llama-server on the same host:

```
boot (system scope)
  multi-user.target
    └─ remoteterm.service          — RT, port :8000

user login (user scope)
  default.target
    └─ llama-server.service         — llama.cpp, port :8090
         └─ tricky-mesh-ai.service  — this daemon, port :8091
```

## Quick ops reference

| What | Command |
|---|---|
| Tail bot logs | `journalctl --user -u tricky-mesh-ai -f` |
| Tail llama.cpp logs | `journalctl --user -u llama-server -f` |
| Tail RT logs | `sudo journalctl -u remoteterm -f` |
| Restart bot | `systemctl --user restart tricky-mesh-ai` |
| Restart llama | `systemctl --user restart llama-server` |
| Restart RT | `sudo systemctl restart remoteterm` |
| Enable auto-start (bot) | `systemctl --user enable tricky-mesh-ai` |
| Enable auto-start (llama) | `systemctl --user enable llama-server` |
| Enable auto-start (RT) | `sudo systemctl enable remoteterm` |
| Bot health | `curl -s http://127.0.0.1:8091/health` |
| llama.cpp ready? | `curl -s http://localhost:8090/v1/models \| jq '.data[].id'` |
| RT health (inc. webhook status) | `curl -s http://localhost:8000/api/health \| jq` |
| Check dead-letter log | `tail -n 20 ~/.local/state/tricky-mesh-ai-api/dead-letter.jsonl` |
| Prometheus metrics (if enabled) | `curl -s http://127.0.0.1:9108/metrics` |

## Tests

```bash
uv run pytest
```

Coverage:
- `tests/test_config.py` — YAML schema, required keys, allowlist
  normalization
- `tests/test_daemon.py` — HTTP reply flow, allowlist, rate limit,
  oversize drop, empty-reply skip, truncation, RT-500 dead-letter,
  sender_key-to-prefix fallback
- `tests/test_http_server.py` — FastAPI `/ingest-dm` route: HMAC
  missing/bad, CHAN drop, outgoing drop, happy-path PRIV, HMAC
  disabled, `/health`
- Plus memory, summarizer, truncate, ratelimit, deadletter, metrics,
  llm unit tests

## Layout

| Path | Purpose |
|------|---------|
| `src/tricky_mesh_ai/daemon.py` | LLM pipeline + outbound HTTP reply to RT |
| `src/tricky_mesh_ai/http_server.py` | FastAPI app: `/ingest-dm`, `/health` |
| `src/tricky_mesh_ai/config.py` | YAML config dataclass with validation |
| `src/tricky_mesh_ai/llm.py` | OpenAI-compatible HTTP client |
| `src/tricky_mesh_ai/memory.py` | Per-sender bounded history with TTL |
| `src/tricky_mesh_ai/summarizer.py` | Background rolling summary worker |
| `src/tricky_mesh_ai/truncate.py` | UTF-8-safe byte-bounded truncation |
| `src/tricky_mesh_ai/ratelimit.py` | Per-key cooldown limiter |
| `src/tricky_mesh_ai/deadletter.py` | JSONL append log of dropped messages |
| `src/tricky_mesh_ai/metrics.py` | Prometheus counters + optional HTTP server |
| `src/tricky_mesh_ai/__main__.py` | uvicorn boot |
| `systemd/tricky-mesh-ai.service` | Hardened user-scope systemd unit |
| `tests/` | pytest + pytest-asyncio suite |
