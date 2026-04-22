"""End-to-end tests for the FastAPI /ingest-dm endpoint.

These exercise the full request path: HMAC verification, payload
filtering (PRIV-only, outgoing=False), and dispatch into the Daemon's
handle_inbound_dm. The Daemon's LLM and httpx reply-client are mocked
so the tests don't hit any external service.
"""

import hashlib
import hmac
import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from tricky_mesh_ai.config import Config
from tricky_mesh_ai.http_server import build_app


SENDER_PREFIX = "a1b2c3d4e5f6"
SENDER_KEY = SENDER_PREFIX + "0" * 52
HMAC_SECRET = "shared-webhook-secret"


def _cfg(tmp_path) -> Config:
    return Config(
        llama_endpoint="http://localhost:1",
        rt_base_url="http://localhost:8000",
        allowed_pubkey_prefixes=[SENDER_PREFIX],
        dead_letter_log_path=str(tmp_path / "dl.jsonl"),
        system_prompt="test prompt",
        conversation_summary_enabled=False,
        ingest_hmac_secret=HMAC_SECRET,
    )


def _sign(body: bytes, secret: str = HMAC_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class _FakeLlm:
    def __init__(self, reply: str = "pong"):
        self.reply = reply
        self.calls: list[str] = []

    async def acomplete(self, prompt, history=None, extra_system=None):
        self.calls.append(prompt)
        return self.reply

    async def aclose(self):
        pass

    def close(self):
        pass


def _build_client(cfg: Config, *, reply: str = "pong") -> tuple[TestClient, MagicMock]:
    """Build a TestClient against a fresh app, with daemon LLM + httpx mocked."""
    app = build_app(cfg)
    # The app was built with a fresh Daemon. Reach in and replace its LLM and
    # httpx client before TestClient's startup hook runs the real daemon.startup
    # (which would create a real httpx client — we want the mocked one).
    daemon = None
    for route in app.router.on_startup:
        closure = getattr(route, "__closure__", None) or ()
        for cell in closure:
            try:
                candidate = cell.cell_contents
            except ValueError:
                continue
            if candidate.__class__.__name__ == "Daemon":
                daemon = candidate
                break
        if daemon is not None:
            break
    assert daemon is not None, "failed to locate Daemon inside FastAPI app"

    daemon.llm = _FakeLlm(reply=reply)

    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(
        return_value=httpx.Response(status_code=202, request=httpx.Request("POST", "http://x/"))
    )
    mock_client.aclose = AsyncMock()
    # Override startup so it doesn't replace our mock.
    original_startup = daemon.startup

    async def _patched_startup() -> None:
        # Skip constructing a real httpx client; inject the mock instead.
        daemon._rt_client = mock_client

    daemon.startup = _patched_startup  # type: ignore[assignment]

    client = TestClient(app)
    return client, mock_client


def test_missing_signature_rejects(tmp_path):
    cfg = _cfg(tmp_path)
    client, _ = _build_client(cfg)
    with client:
        resp = client.post("/ingest-dm", json={"type": "PRIV", "text": "hi"})
    assert resp.status_code == 401


def test_bad_signature_rejects(tmp_path):
    cfg = _cfg(tmp_path)
    client, _ = _build_client(cfg)
    body = json.dumps({"type": "PRIV", "text": "hi"}).encode()
    with client:
        resp = client.post(
            "/ingest-dm",
            content=body,
            headers={
                "content-type": "application/json",
                "X-Webhook-Signature": "sha256=deadbeef",
            },
        )
    assert resp.status_code == 401


def test_channel_message_is_dropped(tmp_path):
    cfg = _cfg(tmp_path)
    client, mock_client = _build_client(cfg)
    body = json.dumps({"type": "CHAN", "text": "hello", "sender_key": SENDER_KEY}).encode()
    with client:
        resp = client.post(
            "/ingest-dm",
            content=body,
            headers={
                "content-type": "application/json",
                "X-Webhook-Signature": _sign(body),
            },
        )
    assert resp.status_code == 204
    mock_client.post.assert_not_called()


def test_outgoing_message_is_dropped(tmp_path):
    cfg = _cfg(tmp_path)
    client, mock_client = _build_client(cfg)
    body = json.dumps(
        {"type": "PRIV", "text": "our own send", "sender_key": SENDER_KEY, "outgoing": True}
    ).encode()
    with client:
        resp = client.post(
            "/ingest-dm",
            content=body,
            headers={
                "content-type": "application/json",
                "X-Webhook-Signature": _sign(body),
            },
        )
    assert resp.status_code == 204
    mock_client.post.assert_not_called()


def test_incoming_priv_dm_reaches_daemon_and_posts_reply(tmp_path):
    cfg = _cfg(tmp_path)
    client, mock_client = _build_client(cfg, reply="pong!")
    body = json.dumps(
        {
            "type": "PRIV",
            "text": "hello bot",
            "sender_key": SENDER_KEY,
            "sender_name": "Alice",
            "outgoing": False,
        }
    ).encode()
    with client:
        resp = client.post(
            "/ingest-dm",
            content=body,
            headers={
                "content-type": "application/json",
                "X-Webhook-Signature": _sign(body),
            },
        )
    assert resp.status_code == 202

    # After TestClient context exits, shutdown runs and drains tasks —
    # so the reply POST should have happened by this point.
    assert mock_client.post.await_count == 1
    call = mock_client.post.call_args
    assert call.args[0] == "/api/messages/direct"
    assert call.kwargs["json"] == {"destination": SENDER_KEY, "text": "pong!"}


def test_hmac_disabled_when_secret_empty(tmp_path):
    cfg = replace(_cfg(tmp_path), ingest_hmac_secret="")
    client, mock_client = _build_client(cfg, reply="ok")
    body = json.dumps(
        {"type": "PRIV", "text": "no sig", "sender_key": SENDER_KEY, "outgoing": False}
    ).encode()
    with client:
        resp = client.post(
            "/ingest-dm",
            content=body,
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 202
    assert mock_client.post.await_count == 1


def test_health_endpoint(tmp_path):
    cfg = _cfg(tmp_path)
    client, _ = _build_client(cfg)
    with client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
