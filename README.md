# tricky-mesh-ai-api

Async Python daemon that bridges [MeshCore](https://meshcore.co.uk/) LoRa
direct messages to a local [llama.cpp](https://github.com/ggml-org/llama.cpp)
(or any OpenAI-compatible) chat endpoint. A user DMs the bot from the
MeshCore app or a radio, the daemon runs the message through the LLM, and
replies over the mesh with end-to-end ACK tracking.

## Features

- **MeshCore TCP companion-radio transport** with an explicit reconnect
  loop and bounded exponential backoff.
- **Public-key-prefix allowlist** (empty = accept any sender). MeshCore
  DMs are AES-256 end-to-end encrypted by the protocol.
- **Per-sender conversation memory** with TTL eviction and a background
  rolling summarizer so long chats stay within the model's context.
- **Per-sender rate limiting**, **dead-letter JSONL log** of dropped
  messages, and **Prometheus counters** exposed over an optional HTTP
  endpoint.
- **UTF-8-safe byte-bounded reply truncation** — replies respect the
  mesh's small-payload reality (default 140 bytes, configurable).
- **ACK tracking** via MeshCore's `wait_for_event(ACK, code=...)`; on
  timeout, automatically fires `send_path_discovery` + `send_trace` to
  diagnose marginal links.
- **Systemd user unit** included, with read-only home, private /tmp,
  MemoryDenyWriteExecute, and restricted namespaces.

## Requirements

- Python 3.13+
- A MeshCore companion-radio device reachable over TCP (WiFi firmware,
  default port 5000)
- A running OpenAI-compatible chat endpoint (llama.cpp's `/v1/chat/completions`)

## Install

```bash
git clone git@github.com:trickpattyFH20/tricky-mesh-ai-api.git
cd tricky-mesh-ai-api
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Configure

Copy the example and edit:

```bash
mkdir -p ~/.config/tricky-mesh-ai-api
cp config.example.yaml ~/.config/tricky-mesh-ai-api/config.yaml
$EDITOR ~/.config/tricky-mesh-ai-api/config.yaml
```

Minimum required fields:

```yaml
meshcore_host: 192.168.40.69      # IP of the companion radio on your LAN
meshcore_port: 5000                # WiFi companion firmware default
llama_endpoint: http://localhost:8090
allowed_pubkey_prefixes: []        # empty = accept any sender
```

See [`config.example.yaml`](./config.example.yaml) for the full schema
with per-field comments (memory, summarizer, metrics, traceroute, rate
limit, reconnect, etc.).

## Run

Directly:

```bash
.venv/bin/tricky-mesh-ai --config ~/.config/tricky-mesh-ai-api/config.yaml
```

As a systemd user service:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/tricky-mesh-ai.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tricky-mesh-ai
journalctl --user -u tricky-mesh-ai -f
```

## Single-TCP-client caveat

The MeshCore WiFi companion firmware currently permits **one** TCP
client at a time. While the daemon is connected, the MeshCore phone/web
app cannot also be connected to the same radio via WiFi. Use BLE to the
same radio for the app, or give the app its own companion radio.

## Tests

```bash
.venv/bin/pytest
```

65 tests cover config loading, the async daemon happy path, allowlist
and contact-refresh logic, rate limiting, ACK success/timeout paths,
path-discovery + trace cooldown, self-DM filtering, conversation memory,
rolling summarization, truncation, dead-letter logging, and metrics.

## Architecture

```
MeshCore radio (LoRa mesh)
  |
  | TCP 5000 (single client)
  v
meshcore.MeshCore(TCP) ---subscribe(CONTACT_MSG_RECV)---> Daemon._on_msg
                                                              |
                                                              v
                                                 asyncio.create_task(_handle)
                                                              |
                                            prefix -> contact (cached, refresh on miss)
                                            allowlist + rate limit + inbound size
                                            memory + rolling summary
                                                              |
                                                              v
                                              httpx.AsyncClient -> llama.cpp
                                                              |
                                                  truncate to max_reply_bytes
                                                              |
                                                              v
                                              commands.send_msg(contact, reply)
                                                              |
                                                 wait_for_event(ACK, code=...)
                                                  - success -> metric
                                                  - timeout -> metric + dead-letter
                                                              + send_path_discovery
                                                              + send_trace
```

The daemon's event handler is a thin dispatcher — long work (LLM call,
ACK wait) runs as tracked `asyncio.Task` so MeshCore event delivery is
never blocked. Tasks are drained on shutdown.

## Layout

| Path | Purpose |
|------|---------|
| `src/tricky_mesh_ai/daemon.py` | Async MeshCore event loop + message handler |
| `src/tricky_mesh_ai/config.py` | YAML config dataclass with validation |
| `src/tricky_mesh_ai/llm.py` | OpenAI-compatible HTTP client (async + sync) |
| `src/tricky_mesh_ai/memory.py` | Per-sender bounded history with TTL |
| `src/tricky_mesh_ai/summarizer.py` | Background rolling summary worker |
| `src/tricky_mesh_ai/truncate.py` | UTF-8-safe byte-bounded truncation |
| `src/tricky_mesh_ai/ratelimit.py` | Per-key cooldown limiter |
| `src/tricky_mesh_ai/deadletter.py` | JSONL append log of dropped messages |
| `src/tricky_mesh_ai/metrics.py` | Prometheus counters + optional HTTP server |
| `systemd/tricky-mesh-ai.service` | Hardened user-scope systemd unit |
| `tests/` | pytest + pytest-asyncio suite (65 tests) |
