import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
import yaml


DEFAULT_SYSTEM_PROMPT = (
    "You are {model}, an AI assistant reachable by direct message over a "
    "MeshCore LoRa mesh radio. Users DM you from their radios or the "
    "MeshCore phone app, and you answer in a private 1-to-1 conversation. "
    "You are not a relay, gateway, or mesh service — you only answer the "
    "sender's question. "
    "\n\n"
    "If asked what this project is: it is tricky-mesh-ai-api, a small Python "
    "daemon that bridges MeshCore DMs to a local llama.cpp server running "
    "you. Incoming DMs are end-to-end AES-256 encrypted by the MeshCore "
    "protocol, and we gate them against an allowlist of public-key prefixes. "
    "\n\n"
    "Keep replies very short (well under 140 bytes). Prefer one or two short "
    "sentences. No lists, no markdown."
)


def _normalize_prefix(raw: str) -> str:
    """Normalize a user-supplied pubkey prefix: lowercase, strip `0x`,
    reject anything that isn't exactly 12 hex chars."""
    s = raw.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 12 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError(
            f"allowed_pubkey_prefixes entries must be 12 hex chars (6 bytes); got {raw!r}"
        )
    return s


@dataclass
class Config:
    # Required
    llama_endpoint: str
    # URL of the Remote-Terminal-for-MeshCore instance that will deliver
    # inbound DMs (webhook POSTs to us) and receive our outbound replies
    # (we POST to /api/messages/direct there). RT owns the radio; this
    # daemon no longer holds a TCP connection to meshcore itself.
    rt_base_url: str

    # HTTP server: where RT's webhook fanout should POST inbound DMs.
    listen_host: str = "127.0.0.1"
    listen_port: int = 8090

    # HMAC secret shared with RT's webhook fanout config. Empty = skip
    # verification (discouraged). When set, the X-Webhook-Signature
    # header (sha256=<hex> of the JSON body) is required and validated.
    ingest_hmac_secret: str = ""

    # Optional HTTP Basic auth forwarded to RT when posting replies.
    # Only needed if RT is launched with MESHCORE_BASIC_AUTH_USERNAME /
    # MESHCORE_BASIC_AUTH_PASSWORD set. Leave empty otherwise.
    rt_basic_auth_username: str = ""
    rt_basic_auth_password: str = ""

    # Allowlist of 12-char hex pubkey prefixes permitted to DM the bot.
    # Empty list = accept any sender (MeshCore DMs are always end-to-end
    # encrypted, but "any encrypted sender" still means anyone can spam).
    allowed_pubkey_prefixes: list[str] = field(default_factory=list)

    # LLM
    model: str | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Wire-format cap for the reply sent over LoRa, measured in UTF-8 bytes.
    # MeshCore docs cite a 133-char channel cap; 140 is a safe default for DMs.
    max_reply_bytes: int = 140
    # Sanity cap on inbound DM size — don't shove absurdly large text at the LLM.
    max_inbound_bytes: int = 500
    llm_timeout_seconds: float = 60.0
    # Cap on completion tokens. None = unbounded (let reasoning models think).
    llm_max_tokens: int | None = None

    # Timeout for the HTTP POST to RT when sending a reply.
    rt_reply_timeout_seconds: float = 30.0

    # Operational
    rate_limit_per_sender_seconds: float = 0.0  # 0 disables
    dead_letter_log_path: str | None = None

    # Conversation memory (per-sender). 0 disables entirely.
    conversation_memory_turns: int = 100
    conversation_ttl_seconds: float = 86400.0  # 24h

    # Rolling summary.
    conversation_summary_enabled: bool = True
    conversation_summary_trigger_turns: int = 60
    conversation_summary_keep_turns: int = 40
    conversation_summary_max_chars: int = 800

    # Metrics
    metrics_http_enabled: bool = False
    metrics_http_host: str = "127.0.0.1"
    metrics_http_port: int = 9108

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"config {path} must be a YAML mapping")

        required = ("rt_base_url", "llama_endpoint")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError(f"config {path} missing required keys: {missing}")

        prefixes = data.get("allowed_pubkey_prefixes") or []
        if not isinstance(prefixes, list) or not all(isinstance(s, str) for s in prefixes):
            raise ValueError("allowed_pubkey_prefixes must be a list of strings")
        data["allowed_pubkey_prefixes"] = [_normalize_prefix(s) for s in prefixes]

        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"config {path} has unknown keys: {unknown}")

        return cls(**data)

    @property
    def dead_letter_path(self) -> Path | None:
        if not self.dead_letter_log_path:
            return None
        return Path(self.dead_letter_log_path).expanduser()

    @property
    def allowed_prefix_set(self) -> set[str]:
        return set(self.allowed_pubkey_prefixes)
