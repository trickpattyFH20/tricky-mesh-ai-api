"""Daemon: LLM-backed auto-reply for DMs delivered over HTTP from
Remote-Terminal-for-MeshCore.

This module used to hold a direct TCP connection to a MeshCore companion
radio and TX replies via ``mc.commands.send_msg``. That architecture
conflicted with Remote-Terminal also owning the radio (single TCP client
limit on the companion firmware's SerialWifiInterface) and prevented the
daemon from seeing DMs that arrive via MQTT downlink (those never touch
the physical radio). The current design:

- Remote-Terminal's webhook fanout POSTs inbound decoded DMs to this
  daemon's FastAPI ``/ingest-dm`` endpoint (see ``http_server.py``).
- The HTTP handler calls ``Daemon.handle_inbound_dm(payload)`` with a
  minimal event-shape dict (``text``, ``pubkey_prefix``, ``path_len``,
  ``timestamp``).
- After the LLM generates a reply, we POST it back to RT's
  ``/api/messages/direct`` route; RT owns the radio and publishes to
  MQTT, so the reply reaches both local-RF neighbors and out-of-range
  recipients that downlink via community brokers.
"""

import asyncio
import logging
import signal
import time
from http.server import ThreadingHTTPServer
from typing import Any

import httpx

from .config import Config
from .deadletter import DeadLetterLog
from .llm import LlamaClient
from .memory import ConversationMemory
from .metrics import Metrics, start_metrics_server
from .ratelimit import RateLimiter
from .summarizer import build_summarizer
from .truncate import truncate


log = logging.getLogger(__name__)


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.allowed = config.allowed_prefix_set
        self.llm = LlamaClient(
            endpoint=config.llama_endpoint,
            model=config.model,
            system_prompt=config.system_prompt,
            timeout=config.llm_timeout_seconds,
            max_tokens=config.llm_max_tokens,
        )
        self.ratelimit = RateLimiter(config.rate_limit_per_sender_seconds)
        self.summarizer = build_summarizer(
            enabled=(
                config.conversation_summary_enabled
                and config.conversation_memory_turns > 0
            ),
            llm_client=self.llm,
            max_chars=config.conversation_summary_max_chars,
        )
        self.memory = ConversationMemory(
            max_turns=config.conversation_memory_turns,
            ttl_seconds=config.conversation_ttl_seconds,
            summary_trigger_turns=config.conversation_summary_trigger_turns,
            summary_keep_turns=config.conversation_summary_keep_turns,
            summarizer=self.summarizer,
        )
        self.deadletter = DeadLetterLog(config.dead_letter_path)
        self.metrics = Metrics()
        self._metrics_server: ThreadingHTTPServer | None = None
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._rt_client: httpx.AsyncClient | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Create long-lived resources (httpx client, metrics server)."""
        self._install_signal_handlers()
        if self.config.metrics_http_enabled:
            self._metrics_server = start_metrics_server(
                self.metrics,
                self.config.metrics_http_host,
                self.config.metrics_http_port,
            )
        auth = None
        if self.config.rt_basic_auth_username and self.config.rt_basic_auth_password:
            auth = httpx.BasicAuth(
                self.config.rt_basic_auth_username,
                self.config.rt_basic_auth_password,
            )
        self._rt_client = httpx.AsyncClient(
            base_url=self.config.rt_base_url.rstrip("/"),
            timeout=self.config.rt_reply_timeout_seconds,
            auth=auth,
        )
        auth_desc = f"allowlist={sorted(self.allowed)}" if self.allowed else "any sender"
        log.info(
            "daemon ready; RT=%s, %s",
            self.config.rt_base_url,
            auth_desc,
        )

    async def shutdown(self) -> None:
        """Drain pending tasks and close long-lived resources."""
        await self._drain_tasks()
        if self._rt_client is not None:
            await self._rt_client.aclose()
            self._rt_client = None
        await self.llm.aclose()
        self.llm.close()
        if hasattr(self.summarizer, "stop"):
            self.summarizer.stop()
        if self._metrics_server is not None:
            self._metrics_server.shutdown()
            self._metrics_server.server_close()
        log.info("daemon stopped")

    # ── Signal handling (best-effort; uvicorn usually owns signals) ─────

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda *_: self._request_stop())

    def _request_stop(self) -> None:
        log.info("shutdown requested")
        self._stop.set()

    # ── Inbound DM handling ─────────────────────────────────────────────

    async def handle_inbound_dm(self, payload: dict[str, Any]) -> None:
        """Public entry point called by the HTTP server for each ``/ingest-dm``
        POST. Spawns a background task so the HTTP handler can return 202
        quickly.
        """
        t = asyncio.create_task(self._handle_wrap(payload))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _handle_wrap(self, payload: dict[str, Any]) -> None:
        try:
            await self._handle(_SyntheticEvent(payload))
        except Exception:
            log.exception("error handling inbound DM")

    # NOTE: _handle and _handle_inner keep the SyntheticEvent indirection so
    # existing unit tests (tests/test_daemon.py) that feed MagicMock-style
    # events keep working unchanged.
    async def _handle(self, event) -> None:
        try:
            await self._handle_inner(event)
        except Exception:
            log.exception("error handling incoming DM")

    async def _handle_inner(self, event) -> None:
        payload: dict[str, Any] = event.payload or {}
        prefix = (payload.get("pubkey_prefix") or "").lower()
        text = (payload.get("text") or "").strip()
        if not prefix or not text:
            return

        self.metrics.inc("messages_received_total")

        if len(text.encode("utf-8")) > self.config.max_inbound_bytes:
            self.metrics.inc("messages_dropped_inbound_too_large_total")
            self.deadletter.record(
                "inbound_too_large",
                prefix,
                text[:200],
                {"bytes": len(text.encode("utf-8"))},
            )
            log.info("dropped oversized DM from %s (%d bytes)", prefix, len(text.encode("utf-8")))
            return

        if self.allowed and prefix not in self.allowed:
            self.metrics.inc("messages_dropped_authz_total")
            self.deadletter.record("not_allowed", prefix, text, {})
            log.info("dropped DM from non-allowlisted sender %s", prefix)
            return

        # Rate-limit by the full sender pubkey if we have it, else by prefix.
        # RT's webhook body includes the full sender_key for PRIV messages;
        # fall back to prefix if the caller only supplied a short id.
        rate_key = (payload.get("sender_key") or prefix).lower()
        allowed_rl, retry_after = self.ratelimit.allow(rate_key)
        if not allowed_rl:
            self.metrics.inc("messages_dropped_ratelimit_total")
            self.deadletter.record(
                "rate_limited",
                prefix,
                text,
                {"retry_after_s": retry_after},
            )
            log.info("rate-limited DM from %s (retry in %.1fs)", prefix, retry_after)
            return

        path_len = payload.get("path_len")
        log.info(
            "DM from %s path_len=%s: %r",
            prefix,
            path_len if path_len is not None else "?",
            text,
        )

        history = [(t.role, t.content) for t in self.memory.get(rate_key)]
        summary = self.summarizer.get(rate_key)
        extra_system = None
        if summary:
            extra_system = f"PRIOR CONVERSATION SUMMARY:\n{summary}"

        start = time.monotonic()
        try:
            raw = await self.llm.acomplete(text, history=history, extra_system=extra_system)
        except Exception as e:
            self.metrics.inc("llm_errors_total")
            self.metrics.observe_llm_latency(time.monotonic() - start)
            self.deadletter.record("llm_error", prefix, text, {"error": str(e)})
            log.error("LLM call failed for %s: %s", prefix, e)
            return
        self.metrics.observe_llm_latency(time.monotonic() - start)

        reply = truncate(raw, self.config.max_reply_bytes)
        if not reply:
            self.deadletter.record("empty_reply", prefix, text, {})
            log.warning("empty LLM reply for %s; skipping send", prefix)
            return

        log.info("reply to %s (%d bytes): %r", prefix, len(reply.encode("utf-8")), reply)

        # Determine destination for the HTTP reply. We prefer the full
        # sender_key from RT's webhook body; fall back to prefix.
        destination = (payload.get("sender_key") or prefix).lower()
        if not destination:
            self.deadletter.record("no_destination", prefix, reply, {})
            log.warning("no sender identity available; dropping reply to %s", prefix)
            return

        ok = await self._send_reply_http(destination, reply)
        if not ok:
            return

        # Update memory only on successful send.
        self.memory.append(rate_key, "user", text)
        self.memory.append(rate_key, "assistant", reply)
        self.metrics.inc("messages_replied_total")

    # ── Outbound reply via RT's REST API ────────────────────────────────

    async def _send_reply_http(self, destination: str, text: str) -> bool:
        """POST a DM reply to Remote-Terminal's /api/messages/direct endpoint.

        Returns True on HTTP 2xx, False otherwise (metrics + dead-letter
        captured for the failure case).
        """
        if self._rt_client is None:
            log.error("httpx client not initialized; call startup() first")
            return False
        try:
            resp = await self._rt_client.post(
                "/api/messages/direct",
                json={"destination": destination, "text": text},
            )
        except Exception as e:
            self.metrics.inc("send_errors_total")
            self.deadletter.record("send_error", destination, text, {"error": str(e)})
            log.error("POST /api/messages/direct to RT failed for %s: %s", destination, e)
            return False
        if resp.status_code >= 400:
            self.metrics.inc("send_errors_total")
            self.deadletter.record(
                "send_error",
                destination,
                text,
                {"status": resp.status_code, "body": resp.text[:500]},
            )
            log.error(
                "RT rejected reply to %s: HTTP %d %s",
                destination,
                resp.status_code,
                resp.text[:200],
            )
            return False
        return True

    # ── Internals ───────────────────────────────────────────────────────

    async def _drain_tasks(self) -> None:
        if not self._tasks:
            return
        log.info("draining %d pending tasks", len(self._tasks))
        await asyncio.gather(*self._tasks, return_exceptions=True)


class _SyntheticEvent:
    """Tiny adapter so _handle_inner keeps its original ``event.payload``
    access pattern regardless of whether the caller is the HTTP server
    (dict payload) or legacy tests (MagicMock with ``payload`` attribute).
    """

    __slots__ = ("payload",)

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
