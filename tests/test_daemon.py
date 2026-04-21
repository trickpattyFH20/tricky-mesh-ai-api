import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meshcore import EventType

from tricky_mesh_ai.config import Config
from tricky_mesh_ai.daemon import Daemon


SENDER_PREFIX = "a1b2c3d4e5f6"
SENDER_PUBKEY = SENDER_PREFIX + "0" * 52  # 64-char hex full key
OTHER_PREFIX = "ffffffff0000"


def _cfg(tmp_path, **overrides) -> Config:
    base = Config(
        meshcore_host="m.local",
        llama_endpoint="http://x:1",
        allowed_pubkey_prefixes=[SENDER_PREFIX],
        dead_letter_log_path=str(tmp_path / "dl.jsonl"),
        system_prompt="test prompt",
        conversation_summary_enabled=False,
        ack_wait_timeout_seconds=0.05,  # keep tests fast
        traceroute_cooldown_seconds=60,
    )
    return replace(base, **overrides)


class FakeCommands:
    def __init__(self):
        self.sent: list[tuple[dict, str]] = []
        self.path_discoveries: list[dict] = []
        self.traces: int = 0
        self.next_expected_ack: bytes | None = b"\xde\xad\xbe\xef\xca\xfe"

    async def send_msg(self, dst, msg, timestamp=None, attempt=0):
        self.sent.append((dst, msg))
        if self.next_expected_ack is None:
            return SimpleNamespace(type=EventType.MSG_SENT, payload={})
        return SimpleNamespace(
            type=EventType.MSG_SENT,
            payload={"expected_ack": self.next_expected_ack},
        )

    async def send_path_discovery(self, dst):
        self.path_discoveries.append(dst)
        return SimpleNamespace(type=EventType.PATH_RESPONSE, payload={})

    async def send_trace(self, auth_code=0, tag=None, flags=None, path=None):
        self.traces += 1
        return SimpleNamespace(type=EventType.TRACE_DATA, payload={})


class FakeMeshCore:
    """Mocks the small slice of meshcore that the daemon touches."""

    def __init__(self, contacts=None, self_pubkey: str = "0" * 64):
        self._contacts: dict[str, dict] = {c["public_key"]: c for c in (contacts or [])}
        self.commands = FakeCommands()
        self.self_info = {"public_key": self_pubkey}
        self.ensure_calls = 0
        self._ack_result = None
        self.auto_fetch_started = False
        self.disconnected = False

    # Shape matches meshcore.MeshCore's public surface used by daemon.

    async def ensure_contacts(self, follow: bool = False) -> bool:
        self.ensure_calls += 1
        return True

    def get_contact_by_key_prefix(self, prefix: str):
        prefix = prefix.lower()
        for pk, c in self._contacts.items():
            if pk.lower().startswith(prefix):
                return c
        return None

    def subscribe(self, event_type, callback, attribute_filters=None):
        return None  # daemon doesn't use the return value

    async def start_auto_message_fetching(self):
        self.auto_fetch_started = True

    async def stop_auto_message_fetching(self):
        self.auto_fetch_started = False

    async def disconnect(self):
        self.disconnected = True

    async def wait_for_event(self, event_type, attribute_filters=None, timeout=None):
        return self._ack_result

    # Helpers used by tests.
    def set_ack_result(self, result):
        self._ack_result = result

    def add_contact(self, contact):
        self._contacts[contact["public_key"]] = contact


class FakeLLM:
    def __init__(self, reply="ok reply"):
        self.reply = reply
        self.calls: list[dict] = []

    async def acomplete(self, text, history=(), extra_system=None, system_override=None):
        self.calls.append(
            {
                "text": text,
                "history": list(history),
                "extra_system": extra_system,
                "system_override": system_override,
            }
        )
        return self.reply

    def complete(self, text, history=(), extra_system=None, system_override=None):
        return self.reply

    async def aclose(self):
        pass

    def close(self):
        pass


def _make_daemon(cfg, reply="hi there") -> tuple[Daemon, FakeMeshCore, FakeLLM]:
    d = Daemon(cfg)
    d.llm = FakeLLM(reply=reply)
    mc = FakeMeshCore(
        contacts=[{"public_key": SENDER_PUBKEY, "adv_name": "alice"}],
        self_pubkey="9" * 64,
    )
    d._mc = mc
    d._own_prefix = "9" * 12
    return d, mc, d.llm


def _event(text="hello?", prefix=SENDER_PREFIX, path_len=2, timestamp=123):
    return SimpleNamespace(
        type=EventType.CONTACT_MSG_RECV,
        payload={
            "text": text,
            "pubkey_prefix": prefix,
            "path_len": path_len,
            "timestamp": timestamp,
        },
    )


async def _run_handler(d: Daemon, event) -> None:
    await d._handle(event)
    # Let any spawned ACK task run to completion.
    await asyncio.sleep(0)
    pending = list(d._tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_happy_path_replies_and_sends(tmp_path):
    d, mc, llm = _make_daemon(_cfg(tmp_path), reply="hi!")
    await _run_handler(d, _event())
    assert len(mc.commands.sent) == 1
    dst, text = mc.commands.sent[0]
    assert dst["public_key"] == SENDER_PUBKEY
    assert text == "hi!"
    assert llm.calls[0]["text"] == "hello?"


async def test_allowlist_reject(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path))
    mc.add_contact({"public_key": OTHER_PREFIX + "0" * 52, "adv_name": "mallory"})
    await _run_handler(d, _event(prefix=OTHER_PREFIX))
    assert mc.commands.sent == []
    dl = (tmp_path / "dl.jsonl").read_text().splitlines()
    assert json.loads(dl[0])["reason"] == "not_allowed"
    assert d.metrics._counters["messages_dropped_authz_total"] == 1


async def test_empty_allowlist_accepts_any_known_sender(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path, allowed_pubkey_prefixes=[]))
    await _run_handler(d, _event())
    assert len(mc.commands.sent) == 1


async def test_unknown_sender_triggers_ensure_then_drops(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path, allowed_pubkey_prefixes=[]))
    await _run_handler(d, _event(prefix=OTHER_PREFIX))
    assert mc.commands.sent == []
    assert mc.ensure_calls == 1  # refresh attempted
    assert d.metrics._counters["messages_dropped_unknown_sender_total"] == 1


async def test_unknown_sender_resolves_after_refresh(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path, allowed_pubkey_prefixes=[]))
    new_pk = OTHER_PREFIX + "0" * 52

    async def refresh_adds(follow=False):
        mc.ensure_calls += 1
        if mc.ensure_calls == 1:
            mc.add_contact({"public_key": new_pk, "adv_name": "bob"})
        return True

    mc.ensure_contacts = refresh_adds  # type: ignore[assignment]
    await _run_handler(d, _event(prefix=OTHER_PREFIX))
    assert len(mc.commands.sent) == 1
    assert mc.commands.sent[0][0]["public_key"] == new_pk


async def test_inbound_too_large_dropped(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path, max_inbound_bytes=20))
    await _run_handler(d, _event(text="x" * 100))
    assert mc.commands.sent == []
    assert d.metrics._counters["messages_dropped_inbound_too_large_total"] == 1


async def test_rate_limit(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path, rate_limit_per_sender_seconds=60))
    await _run_handler(d, _event())
    await _run_handler(d, _event())
    assert len(mc.commands.sent) == 1
    assert d.metrics._counters["messages_dropped_ratelimit_total"] == 1


async def test_reply_truncated(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path, max_reply_bytes=30), reply="x" * 600)
    await _run_handler(d, _event())
    _, reply = mc.commands.sent[0]
    assert len(reply.encode("utf-8")) <= 30


async def test_empty_reply_drops(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path), reply="   ")
    await _run_handler(d, _event())
    assert mc.commands.sent == []


async def test_llm_error_dead_lettered(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path))

    class Boom:
        async def acomplete(self, *a, **kw):
            raise RuntimeError("llm down")

        def complete(self, *a, **kw):
            raise RuntimeError("llm down")

        async def aclose(self):
            pass

        def close(self):
            pass

    d.llm = Boom()
    await _run_handler(d, _event())
    assert mc.commands.sent == []
    entry = json.loads((tmp_path / "dl.jsonl").read_text().splitlines()[0])
    assert entry["reason"] == "llm_error"
    assert "llm down" in entry["error"]
    assert d.metrics._counters["llm_errors_total"] == 1


async def test_ack_success(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path))
    mc.set_ack_result(SimpleNamespace(type=EventType.ACK, payload={"code": "deadbeefcafe"}))
    await _run_handler(d, _event())
    assert d.metrics._counters["reply_acks_success_total"] == 1
    assert d.metrics._counters["reply_acks_failed_total"] == 0
    assert mc.commands.path_discoveries == []


async def test_ack_timeout_triggers_traceroute(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path))
    mc.set_ack_result(None)
    await _run_handler(d, _event())
    assert d.metrics._counters["reply_acks_failed_total"] == 1
    assert len(mc.commands.path_discoveries) == 1
    assert mc.commands.traces == 1
    dl_entries = [json.loads(l) for l in (tmp_path / "dl.jsonl").read_text().splitlines()]
    assert any(e["reason"] == "ack_failed" for e in dl_entries)


async def test_traceroute_disabled_by_config(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path, traceroute_on_failure=False))
    mc.set_ack_result(None)
    await _run_handler(d, _event())
    assert d.metrics._counters["reply_acks_failed_total"] == 1
    assert mc.commands.path_discoveries == []
    assert mc.commands.traces == 0


async def test_traceroute_cooldown_dedup(tmp_path):
    d, mc, _ = _make_daemon(
        _cfg(tmp_path, traceroute_cooldown_seconds=60, rate_limit_per_sender_seconds=0)
    )
    mc.set_ack_result(None)
    await _run_handler(d, _event())
    await _run_handler(d, _event())
    assert len(mc.commands.path_discoveries) == 1


async def test_summary_prepended_to_llm_call(tmp_path):
    d, mc, llm = _make_daemon(_cfg(tmp_path, conversation_summary_enabled=True))
    try:
        d.summarizer._summaries[SENDER_PUBKEY] = "Otto lives in Portsmouth, NH."
        await _run_handler(d, _event(text="remember me?"))
        assert llm.calls[0]["extra_system"] is not None
        assert "Otto lives in Portsmouth" in llm.calls[0]["extra_system"]
    finally:
        d.summarizer.stop()


async def test_memory_builds_history(tmp_path):
    d, mc, llm = _make_daemon(_cfg(tmp_path, conversation_memory_turns=4), reply="r1")
    await _run_handler(d, _event(text="q1"))
    llm.reply = "r2"
    await _run_handler(d, _event(text="q2"))
    assert llm.calls[0]["history"] == []
    assert llm.calls[1]["history"] == [("user", "q1"), ("assistant", "r1")]


async def test_self_dm_ignored(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path))
    # Our own prefix is set in _make_daemon to "9"*12 — a DM from that prefix
    # should be silently dropped before counters/allowlist even run.
    await _run_handler(d, _event(prefix="9" * 12))
    assert mc.commands.sent == []
    assert d.metrics._counters["messages_received_total"] == 0


async def test_send_error_records_dead_letter(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path))

    async def boom(dst, msg, timestamp=None, attempt=0):
        raise RuntimeError("radio unplugged")

    mc.commands.send_msg = boom  # type: ignore[assignment]
    await _run_handler(d, _event())
    assert d.metrics._counters["send_errors_total"] == 1
    entry = json.loads((tmp_path / "dl.jsonl").read_text().splitlines()[0])
    assert entry["reason"] == "send_error"


async def test_missing_expected_ack_skips_ack_tracking(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path))
    mc.commands.next_expected_ack = None
    await _run_handler(d, _event())
    # Reply was sent, memory updated, reply counter bumped, but no ACK
    # wait happened so success/fail counters stay at zero.
    assert d.metrics._counters["messages_replied_total"] == 1
    assert d.metrics._counters["reply_acks_success_total"] == 0
    assert d.metrics._counters["reply_acks_failed_total"] == 0


async def test_metrics_happy_path(tmp_path):
    d, mc, _ = _make_daemon(_cfg(tmp_path))
    mc.set_ack_result(SimpleNamespace(type=EventType.ACK, payload={"code": "deadbeefcafe"}))
    await _run_handler(d, _event())
    assert d.metrics._counters["messages_received_total"] == 1
    assert d.metrics._counters["messages_replied_total"] == 1
    assert d.metrics._llm_latency_count == 1
