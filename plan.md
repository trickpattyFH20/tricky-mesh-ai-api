# tricky-mesh-ai-api — Implementation Plan

## Goal

A Python daemon running on this Linux host that:

1. Connects to the Meshtastic Heltec V4 radio over WiFi (TCP API, `meshtastic.local:4403`).
2. Listens for **direct-message** text packets addressed to this node.
3. Filters by an **authorization allowlist** of sender node IDs.
4. Forwards authorized messages to the local **llama.cpp server on `localhost:8090`**.
5. Caps the LLM response at **255 characters** — both by instructing the model in the system prompt, and by hard-truncating the output before sending.
6. Sends the response back to the originating node as a DM reply.

## Established context (do not rediscover)

- Mesh node: Heltec V4, node id `!b2a73f40` (num `2997305152`), short name `3f40`.
- Meshtastic TCP API: `meshtastic.local` (currently `192.168.40.224` via DHCP), port `4403`.
- Firmware: 2.7.15.567b8ea, VANILLA, `hasPKC: true`.
- llama.cpp: assumed pre-running on `http://localhost:8090` (started separately by the user).
- Meshtastic allows ~4 concurrent TCP clients — this daemon consumes one.

## Connecting to the mesh node

### Addressing

- Hostname: `meshtastic.local` (mDNS — resolved successfully on this host via `getent hosts meshtastic.local` → `192.168.40.224`).
- Fallback: hard-code IP `192.168.40.224`. **Recommended:** give the Heltec a DHCP reservation on the `essex-fc` router so the IP doesn't drift.
- Port: `4403` (Meshtastic protobuf API, TCP).
- Our node's identifiers (for `toId` filtering — messages *to us*):
  - Numeric: `2997305152`
  - Hex ID: `!b2a73f40`
  - MAC: `80:f1:b2:a7:3f:40`
  - Short name: `3f40`

### Python: receive loop

```python
from pubsub import pub
import meshtastic.tcp_interface

OUR_NODE_NUM = 2997305152  # aka !b2a73f40

def on_receive(packet, interface):
    decoded = packet.get('decoded') or {}
    if decoded.get('portnum') != 'TEXT_MESSAGE_APP':
        return
    if packet.get('to') != OUR_NODE_NUM:  # ignore broadcasts & messages to other nodes
        return
    sender_id = packet['fromId']   # e.g. "!abc12345"
    text      = decoded['text']
    # ... authz check, LLM call, reply ...

pub.subscribe(on_receive, "meshtastic.receive")
iface = meshtastic.tcp_interface.TCPInterface(hostname='meshtastic.local')
# Blocks here; pubsub callbacks run on the interface's reader thread.
# Wrap in try/except + reconnect loop for robustness.
```

Packet shape (what `on_receive` receives): dict with top-level keys `from` (int), `fromId` (str, `!hex`), `to` (int), `toId` (str), `id`, `channel`, `rxTime`, `rxSnr`, `rxRssi`, `hopLimit`, `decoded: { portnum, payload, text, ... }`, `publicKey`, etc. Broadcasts use `to: 4294967295` (`^all` / `0xFFFFFFFF`).

### Python: send reply

```python
iface.sendText(
    text="reply body (already ≤255 chars)",
    destinationId=sender_id,   # "!abc12345" — pass the string form, not the int
    wantAck=False,
    channelIndex=packet.get('channel', 0),  # reply on the same channel
)
```

### CLI (for manual testing / debugging)

```
meshtastic --host meshtastic.local --info                    # sanity check / show state
meshtastic --host meshtastic.local --nodes                   # list mesh nodes
meshtastic --host meshtastic.local --listen 2>/dev/null       # stream incoming packets
meshtastic --host meshtastic.local --sendtext "hi" --dest '!b2a73f40'   # send DM
```

### Environment gotchas

- **Don't reuse the global pipx meshtastic install** for the daemon's dependencies — give the project its own venv (`python -m venv .venv` + `pip install meshtastic httpx pyyaml`). Avoids version drift between the interactive CLI and the daemon.
- **On Arch with PEP 668**, `pip3 install meshtastic` directly will fail — either use the project venv or `pipx` (which we already have for the CLI use case).

### Quick connection sanity check before coding

```
ping -c 2 meshtastic.local
timeout 3 bash -c 'echo > /dev/tcp/meshtastic.local/4403' && echo OPEN || echo DOWN
meshtastic --host meshtastic.local --info | head -5
```

## Flow

```
Sender device (phone / other radio)
  ──LoRa DM──▶ Heltec V4
                  ──TCP 4403 (pubsub "meshtastic.receive")──▶ daemon
                                                                │
                                                                ├─ filter: TEXT_MESSAGE_APP + toId == our node
                                                                ├─ authz: fromId in allowlist?
                                                                ├─ HTTP POST /v1/chat/completions ──▶ llama.cpp :8090
                                                                ├─ truncate response to 255 chars (UTF-8-safe)
                                                                └─ iface.sendText(text, destinationId=fromId, wantAck=False)
  ◀──LoRa DM── Heltec V4
```

## Components

1. **Meshtastic listener** — `meshtastic.tcp_interface.TCPInterface` + `pubsub.pub.subscribe(handler, "meshtastic.receive")`. Runs forever.
2. **DM filter** — accept only packets where `decoded.portnum == TEXT_MESSAGE_APP` AND `toId == <our node num>` (not broadcast, not other port types).
3. **Authorization** — static allowlist of sender node IDs loaded from config. **Silent drop** for non-allowlisted senders (don't leak presence).
4. **LLM client** — HTTP POST to `http://localhost:8090/v1/chat/completions`. System prompt enforces 255-char cap. `max_tokens ≈ 100` (safe headroom for 255 chars). Request timeout ~30s.
5. **Response sender** — `iface.sendText(truncated_text, destinationId=packet['fromId'], wantAck=False)`.
6. **Config** — YAML at e.g. `~/.config/tricky-mesh-ai-api/config.yaml`:
   - `meshtastic_host: meshtastic.local`
   - `llama_endpoint: http://localhost:8090`
   - `model: <name>` (optional — llama.cpp serves whatever is loaded)
   - `system_prompt: "..."` (overridable default)
   - `allowed_senders: ["!abc12345", ...]`
   - `rate_limit_per_sender_seconds: 30` (optional)
7. **systemd user service** — `~/.config/systemd/user/tricky-mesh-ai.service`, `Restart=on-failure`, logs to journal.

## Key design decisions / open questions

1. **Authorization mechanism.** Starting with a **static node-ID allowlist**. Node IDs are broadcast in the mesh (not secret), so this is moderate security — fine for home use, weak against an attacker in LoRa range who can spoof `fromId`. Alternatives for later:
   - Shared-secret token as a prefix in message body (e.g., `!tok abc123 what's the weather?`).
   - Private channel: sender must be on a channel we've joined with a shared PSK. Meshtastic already encrypts per-channel, so membership implies authorization.
   - PKI-based: device has `hasPKC=true`, but using it for app-layer auth isn't standardized in Meshtastic CLI — would need deeper integration.

   **MVP: allowlist. Document the threat model in README.**

2. **255-char limit vs. Meshtastic's actual payload cap.** Meshtastic's `MAX_TEXT_MESSAGE_APP_PAYLOAD_SIZE` is ~228 bytes for default channels (overhead varies with PSK length). 255 chars may auto-chunk into two packets, using more airtime. The meshtastic Python library handles chunking transparently.

   **MVP: enforce 255 in prompt AND truncate in code. Observe actual behavior; lower cap if chunking is excessive.**

3. **llama.cpp endpoint.** Use `/v1/chat/completions` (OpenAI-compatible) rather than the native `/completion` — cleaner fit for `system + user` message pattern, and swappable later if we move off llama.cpp.

4. **Rate limiting.** **MVP: none.** LoRa airtime is a shared resource; if abuse surfaces, add per-sender cooldown (e.g., 1/30s) and a global QPS cap.

5. **ACKs.** Send replies with `wantAck=False`. ACKs would retry on failure and flood the mesh; for a casual AI reply, one-shot is appropriate.

6. **Reconnect.** TCP to Heltec may drop on WiFi blips or Heltec reboots. Wrap the interface lifecycle in a retry loop with exponential backoff (cap ~60s).

7. **Conversation state.** **MVP: stateless.** Each DM is an independent prompt. Multi-turn memory per sender is a v2 feature (adds eviction policy, TTL, concurrency concerns).

8. **Truncation strategy.** Truncate at 255 UTF-8 *characters* (not bytes) at a clean boundary. If the truncated text looks mid-sentence, append `…` — total still ≤255.

## Phases

### Phase 1 — MVP daemon (stateless)

- [ ] Python project scaffold (`pyproject.toml`, src layout, own venv — don't reuse the pipx meshtastic install)
- [ ] Config loader (YAML + schema validation)
- [ ] Meshtastic TCP listener with pubsub handler
- [ ] DM filter + allowlist check
- [ ] llama.cpp client with timeout + retry once on transient failure
- [ ] Response truncation (UTF-8-safe, at 255 chars)
- [ ] `sendText` reply back to sender
- [ ] Structured logging (stderr, for journald consumption)
- [ ] End-to-end smoke test with a second Meshtastic node or phone app as sender

### Phase 2 — operational polish

- [ ] TCP reconnect loop with exponential backoff
- [ ] Graceful shutdown (SIGTERM → disconnect cleanly)
- [ ] systemd user unit
- [ ] Per-sender rate limit
- [ ] Optional: dead-letter log for dropped messages (authz fail, LLM error)

### Phase 3 — optional extensions

- [ ] Conversation memory per sender (bounded queue, 15-min TTL)
- [ ] Pluggable auth (shared-secret token, private channel)
- [ ] Simple metrics (message count, avg LLM latency, error count) — expose via local HTTP or Prometheus

## Risks / unknowns

- **Actual Meshtastic payload size limit.** First real send will tell us if 255 chars chunk or fit. Adjust cap accordingly.
- **LLM latency on 35B-A3B.** May be several seconds. Acceptable over LoRa (users expect slow), but worth timing — if >20s typically, reduce `max_tokens` or use a smaller model for this role.
- **Mesh airtime budget.** Reply traffic consumes shared airtime. Allowlist + future rate limit are the main guardrails. If the mesh grows, revisit.
- **llama.cpp binding.** llama.cpp `:8090` MUST bind to `127.0.0.1`, not `0.0.0.0` — no auth on that endpoint.
- **Heltec WiFi stability.** 2.4GHz LoRa+WiFi on the same chip can thermal-throttle under heavy load; monitor connection drops in Phase 1.
- **`fromId` spoofing.** LoRa packets' `fromId` is self-asserted (no signature at the Meshtastic app layer unless using PKC). In-range attackers could impersonate an allowlisted node. Acceptable for home-range, casual use; not for anything sensitive.

## Dependencies

- Python 3.13+ (user has 3.14 via pipx)
- `meshtastic` (Python library)
- `httpx` (async-friendly HTTP; alternative: `requests`)
- `pyyaml`
- `pubsub` (transitive via meshtastic)

## Out of scope for v1

- Web UI / dashboard
- Multi-radio / multi-gateway
- Model switching mid-conversation
- Persistent conversation history
- Mesh ↔ external IM bridge (e.g., Matrix, Signal)
