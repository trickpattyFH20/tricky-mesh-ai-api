import asyncio
import logging
import signal
import time
from http.server import ThreadingHTTPServer
from typing import Any

from meshcore import MeshCore, EventType

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
        self._disconnected = asyncio.Event()
        self._mc: MeshCore | None = None
        self._own_prefix: str | None = None
        self._tasks: set[asyncio.Task] = set()
        self._contact_lock = asyncio.Lock()
        self._last_traceroute: dict[str, float] = {}
        self._traceroute_lock = asyncio.Lock()

    # --- public entry ------------------------------------------------------

    async def run(self) -> None:
        self._install_signal_handlers()
        if self.config.metrics_http_enabled:
            self._metrics_server = start_metrics_server(
                self.metrics,
                self.config.metrics_http_host,
                self.config.metrics_http_port,
            )

        backoff = self.config.reconnect_initial_backoff
        first = True
        try:
            while not self._stop.is_set():
                try:
                    if not first:
                        self.metrics.inc("meshcore_reconnects_total")
                    first = False
                    await self._connect_and_serve()
                    backoff = self.config.reconnect_initial_backoff
                except Exception as e:
                    log.error("meshcore connection error: %s", e, exc_info=True)
                finally:
                    await self._teardown_iface()

                if self._stop.is_set():
                    break
                log.info("reconnecting in %.1fs", backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break  # stop signaled during backoff
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self.config.reconnect_max_backoff)
        finally:
            await self._drain_tasks()
            await self.llm.aclose()
            self.llm.close()
            if hasattr(self.summarizer, "stop"):
                self.summarizer.stop()
            if self._metrics_server is not None:
                self._metrics_server.shutdown()
                self._metrics_server.server_close()
            log.info("daemon stopped")

    # --- connection lifecycle ---------------------------------------------

    async def _connect_and_serve(self) -> None:
        log.info(
            "connecting to meshcore at %s:%d",
            self.config.meshcore_host,
            self.config.meshcore_port,
        )
        self._disconnected.clear()
        self._mc = await MeshCore.create_tcp(
            self.config.meshcore_host, self.config.meshcore_port
        )
        if self._mc is None:
            raise RuntimeError("meshcore node did not respond to connection handshake")

        await self._mc.ensure_contacts()
        self._own_prefix = self._resolve_own_prefix()

        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_msg)
        self._mc.subscribe(EventType.DISCONNECTED, self._on_disconnect)
        await self._mc.start_auto_message_fetching()

        auth_desc = (
            f"allowlist={sorted(self.allowed)}" if self.allowed else "any sender"
        )
        log.info("connected; listening for DMs (%s)", auth_desc)

        # Block until shutdown OR the meshcore library reports the link dropped.
        stop_task = asyncio.create_task(self._stop.wait())
        disc_task = asyncio.create_task(self._disconnected.wait())
        try:
            await asyncio.wait(
                {stop_task, disc_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop_task.cancel()
            disc_task.cancel()

        if self._disconnected.is_set():
            log.warning("meshcore connection lost; triggering reconnect")

    async def _teardown_iface(self) -> None:
        mc, self._mc = self._mc, None
        if mc is None:
            return
        try:
            await mc.stop_auto_message_fetching()
        except Exception:
            pass
        try:
            await mc.disconnect()
        except Exception:
            log.exception("error disconnecting meshcore interface")

    def _resolve_own_prefix(self) -> str | None:
        """Extract our own 12-char pubkey prefix from meshcore.self_info so
        we can drop any stray self-DMs."""
        assert self._mc is not None
        info = self._mc.self_info or {}
        pk = info.get("public_key") or ""
        if isinstance(pk, bytes):
            pk = pk.hex()
        pk = (pk or "").lower()
        return pk[:12] if len(pk) >= 12 else None

    def _on_disconnect(self, event) -> None:
        # Subscribe callbacks are invoked as coroutines by meshcore — but this
        # one is cheap and we want to be sync-safe. Define as plain function:
        # meshcore's dispatcher will handle either shape.
        self._disconnected.set()

    # --- signal handling --------------------------------------------------

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except (NotImplementedError, RuntimeError):
                # Non-unix fallback (tests on odd platforms)
                signal.signal(sig, lambda *_: self._request_stop())

    def _request_stop(self) -> None:
        log.info("shutdown requested")
        self._stop.set()

    # --- message handling -------------------------------------------------

    async def _on_msg(self, event) -> None:
        # Thin dispatcher — long work spawns a task so it doesn't block
        # other event delivery (notably ACK events we're awaiting).
        t = asyncio.create_task(self._handle(event))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

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

        # Self-DM guard.
        if self._own_prefix and prefix == self._own_prefix:
            return

        self.metrics.inc("messages_received_total")

        if len(text.encode("utf-8")) > self.config.max_inbound_bytes:
            self.metrics.inc("messages_dropped_inbound_too_large_total")
            self.deadletter.record(
                "inbound_too_large", prefix, text[:200], {"bytes": len(text.encode("utf-8"))}
            )
            log.info("dropped oversized DM from %s (%d bytes)", prefix, len(text.encode("utf-8")))
            return

        if self.allowed and prefix not in self.allowed:
            self.metrics.inc("messages_dropped_authz_total")
            self.deadletter.record("not_allowed", prefix, text, {})
            log.info("dropped DM from non-allowlisted sender %s", prefix)
            return

        contact = await self._resolve_contact(prefix)
        if contact is None:
            self.metrics.inc("messages_dropped_unknown_sender_total")
            self.deadletter.record("unknown_sender", prefix, text, {})
            log.info("dropped DM from unknown sender %s (not in radio contacts)", prefix)
            return

        # Rate-limit by full public key — stable + unforgeable.
        contact_key = (contact.get("public_key") or prefix).lower()
        allowed_rl, retry_after = self.ratelimit.allow(contact_key)
        if not allowed_rl:
            self.metrics.inc("messages_dropped_ratelimit_total")
            self.deadletter.record(
                "rate_limited", prefix, text, {"retry_after_s": retry_after}
            )
            log.info("rate-limited DM from %s (retry in %.1fs)", prefix, retry_after)
            return

        path_len = payload.get("path_len")
        log.info(
            "DM from %s (%s) path_len=%s: %r",
            prefix,
            contact.get("adv_name") or "?",
            path_len if path_len is not None else "?",
            text,
        )

        history = [(t.role, t.content) for t in self.memory.get(contact_key)]
        summary = self.summarizer.get(contact_key)
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

        assert self._mc is not None
        try:
            sent = await self._mc.commands.send_msg(contact, reply)
        except Exception as e:
            self.metrics.inc("send_errors_total")
            self.deadletter.record("send_error", prefix, reply, {"error": str(e)})
            log.error("send_msg to %s failed: %s", prefix, e)
            return

        # Update memory before spawning ACK-wait so history is consistent
        # for a rapid follow-up from the same sender.
        self.memory.append(contact_key, "user", text)
        self.memory.append(contact_key, "assistant", reply)
        self.metrics.inc("messages_replied_total")

        expected_ack = None
        if sent is not None and getattr(sent, "type", None) == EventType.MSG_SENT:
            ack_bytes = (sent.payload or {}).get("expected_ack")
            if ack_bytes:
                expected_ack = ack_bytes.hex() if isinstance(ack_bytes, (bytes, bytearray)) else str(ack_bytes)

        if expected_ack is not None:
            t = asyncio.create_task(self._await_ack(contact, prefix, expected_ack))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        else:
            log.warning("send_msg to %s returned no expected_ack; skipping ACK tracking", prefix)

    async def _resolve_contact(self, prefix: str) -> dict | None:
        assert self._mc is not None
        contact = self._mc.get_contact_by_key_prefix(prefix)
        if contact is not None:
            return contact
        # Miss — refresh the address book once, then retry. Serialized so a
        # burst of DMs from a new sender doesn't storm ensure_contacts.
        async with self._contact_lock:
            contact = self._mc.get_contact_by_key_prefix(prefix)
            if contact is not None:
                return contact
            try:
                await self._mc.ensure_contacts(follow=True)
            except Exception as e:
                log.warning("ensure_contacts failed: %s", e)
                return None
            return self._mc.get_contact_by_key_prefix(prefix)

    async def _await_ack(self, contact: dict, prefix: str, expected_ack: str) -> None:
        assert self._mc is not None
        try:
            result = await self._mc.wait_for_event(
                EventType.ACK,
                attribute_filters={"code": expected_ack},
                timeout=self.config.ack_wait_timeout_seconds,
            )
        except Exception as e:
            log.warning("error waiting for ACK from %s: %s", prefix, e)
            return

        if result is not None:
            self.metrics.inc("reply_acks_success_total")
            log.info("reply to %s ACKed", prefix)
            return

        self.metrics.inc("reply_acks_failed_total")
        log.warning("reply to %s not ACKed within %.1fs", prefix, self.config.ack_wait_timeout_seconds)
        self.deadletter.record("ack_failed", prefix, "", {"expected_ack": expected_ack})
        await self._maybe_trigger_traceroute(contact, prefix)

    async def _maybe_trigger_traceroute(self, contact: dict, prefix: str) -> None:
        if not self.config.traceroute_on_failure:
            return
        mc = self._mc
        if mc is None:
            return
        key = (contact.get("public_key") or prefix).lower()
        now = time.monotonic()
        async with self._traceroute_lock:
            last = self._last_traceroute.get(key, 0.0)
            if now - last < self.config.traceroute_cooldown_seconds:
                return
            self._last_traceroute[key] = now

        try:
            await mc.commands.send_path_discovery(contact)
            log.info("path discovery sent to %s", prefix)
        except Exception:
            log.exception("send_path_discovery to %s failed", prefix)
            return
        try:
            await mc.commands.send_trace()
            log.info("trace sent after path discovery to %s", prefix)
        except Exception:
            log.exception("send_trace after path discovery to %s failed", prefix)

    async def _drain_tasks(self) -> None:
        if not self._tasks:
            return
        log.info("draining %d pending tasks", len(self._tasks))
        await asyncio.gather(*self._tasks, return_exceptions=True)
