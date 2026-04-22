"""Tests for the Daemon's HTTP-based inbound/outbound flow.

This rewrite replaces the old meshcore-TCP-based tests. The daemon is now
a pure consumer of HTTP webhooks (inbound DMs) and producer of HTTP POSTs
to Remote-Terminal (outbound replies). No meshcore connection, no ACK
tracking, no traceroute — RT owns the radio and handles all that.
"""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tricky_mesh_ai.config import Config
from tricky_mesh_ai.daemon import Daemon


SENDER_PREFIX = "a1b2c3d4e5f6"
SENDER_KEY = SENDER_PREFIX + "0" * 52  # 64-char hex full key
OTHER_PREFIX = "ffffffff0000"


def _cfg(tmp_path, **overrides) -> Config:
    base = Config(
        llama_endpoint="http://localhost:1",
        rt_base_url="http://localhost:8000",
        allowed_pubkey_prefixes=[SENDER_PREFIX],
        dead_letter_log_path=str(tmp_path / "dl.jsonl"),
        system_prompt="test prompt",
        conversation_summary_enabled=False,
    )
    return replace(base, **overrides)


class _FakeLlm:
    """Stand-in for LlamaClient.acomplete/aclose/close."""

    def __init__(self, reply: str = "ok"):
        self.reply = reply
        self.calls: list[tuple[str, list, str | None]] = []

    async def acomplete(self, prompt, history=None, extra_system=None):
        self.calls.append((prompt, list(history or []), extra_system))
        return self.reply

    async def aclose(self):
        pass

    def close(self):
        pass


def _make_daemon(cfg: Config, *, reply: str = "hello back") -> tuple[Daemon, MagicMock]:
    """Build a Daemon with its LLM and httpx client swapped for fakes."""
    d = Daemon(cfg)
    d.llm = _FakeLlm(reply=reply)
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(
        return_value=httpx.Response(status_code=202, request=httpx.Request("POST", "http://x/"))
    )
    client.aclose = AsyncMock()
    d._rt_client = client
    return d, client


async def _drain(daemon: Daemon) -> None:
    """Let handle_inbound_dm background tasks finish."""
    import asyncio
    for _ in range(5):
        await asyncio.sleep(0.02)
    await daemon._drain_tasks()


@pytest.mark.asyncio
async def test_allowed_sender_triggers_llm_and_http_reply(tmp_path):
    cfg = _cfg(tmp_path)
    daemon, client = _make_daemon(cfg, reply="sure thing")

    payload = {
        "text": "hello",
        "pubkey_prefix": SENDER_PREFIX,
        "sender_key": SENDER_KEY,
        "path_len": 1,
        "timestamp": 1,
    }
    await daemon.handle_inbound_dm(payload)
    await _drain(daemon)

    assert daemon.llm.calls == [("hello", [], None)]
    assert client.post.await_count == 1
    args, kwargs = client.post.call_args
    assert args[0] == "/api/messages/direct"
    assert kwargs["json"] == {"destination": SENDER_KEY, "text": "sure thing"}


@pytest.mark.asyncio
async def test_unallowlisted_sender_is_silently_dropped(tmp_path):
    cfg = _cfg(tmp_path, allowed_pubkey_prefixes=[SENDER_PREFIX])
    daemon, client = _make_daemon(cfg)

    payload = {
        "text": "spam",
        "pubkey_prefix": OTHER_PREFIX,
        "sender_key": OTHER_PREFIX + "1" * 52,
    }
    await daemon.handle_inbound_dm(payload)
    await _drain(daemon)

    assert daemon.llm.calls == []
    assert client.post.await_count == 0


@pytest.mark.asyncio
async def test_oversized_inbound_dropped(tmp_path):
    cfg = _cfg(tmp_path, max_inbound_bytes=16)
    daemon, client = _make_daemon(cfg)

    payload = {
        "text": "x" * 100,
        "pubkey_prefix": SENDER_PREFIX,
        "sender_key": SENDER_KEY,
    }
    await daemon.handle_inbound_dm(payload)
    await _drain(daemon)

    assert daemon.llm.calls == []
    assert client.post.await_count == 0


@pytest.mark.asyncio
async def test_empty_reply_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    daemon, client = _make_daemon(cfg, reply="")

    payload = {
        "text": "say nothing",
        "pubkey_prefix": SENDER_PREFIX,
        "sender_key": SENDER_KEY,
    }
    await daemon.handle_inbound_dm(payload)
    await _drain(daemon)

    assert daemon.llm.calls == [("say nothing", [], None)]
    assert client.post.await_count == 0


@pytest.mark.asyncio
async def test_reply_truncates_to_max_bytes(tmp_path):
    cfg = _cfg(tmp_path, max_reply_bytes=12)
    long = "abcdefghijklmnopqrstuvwxyz"
    daemon, client = _make_daemon(cfg, reply=long)

    await daemon.handle_inbound_dm(
        {
            "text": "hi",
            "pubkey_prefix": SENDER_PREFIX,
            "sender_key": SENDER_KEY,
        }
    )
    await _drain(daemon)

    assert client.post.await_count == 1
    sent_text = client.post.call_args.kwargs["json"]["text"]
    assert len(sent_text.encode("utf-8")) <= 12


@pytest.mark.asyncio
async def test_rt_http_error_records_deadletter(tmp_path):
    cfg = _cfg(tmp_path)
    daemon, client = _make_daemon(cfg)
    client.post = AsyncMock(
        return_value=httpx.Response(
            status_code=500,
            text="boom",
            request=httpx.Request("POST", "http://x/"),
        )
    )

    await daemon.handle_inbound_dm(
        {
            "text": "hi",
            "pubkey_prefix": SENDER_PREFIX,
            "sender_key": SENDER_KEY,
        }
    )
    await _drain(daemon)

    assert client.post.await_count == 1
    dl_path = tmp_path / "dl.jsonl"
    assert dl_path.exists()
    body = dl_path.read_text()
    assert "send_error" in body
    assert "500" in body


@pytest.mark.asyncio
async def test_rate_limit_drops_rapid_repeat(tmp_path):
    cfg = _cfg(tmp_path, rate_limit_per_sender_seconds=10.0)
    daemon, client = _make_daemon(cfg)

    payload = {
        "text": "one",
        "pubkey_prefix": SENDER_PREFIX,
        "sender_key": SENDER_KEY,
    }
    await daemon.handle_inbound_dm(payload)
    await _drain(daemon)
    payload2 = {**payload, "text": "two"}
    await daemon.handle_inbound_dm(payload2)
    await _drain(daemon)

    assert len(daemon.llm.calls) == 1
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_missing_sender_key_falls_back_to_prefix(tmp_path):
    cfg = _cfg(tmp_path)
    daemon, client = _make_daemon(cfg)

    await daemon.handle_inbound_dm(
        {
            "text": "hi",
            "pubkey_prefix": SENDER_PREFIX,
            # no sender_key
        }
    )
    await _drain(daemon)

    assert client.post.await_count == 1
    assert client.post.call_args.kwargs["json"]["destination"] == SENDER_PREFIX
