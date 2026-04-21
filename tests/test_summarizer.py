import threading
import time

import pytest

from tricky_mesh_ai.memory import Turn
from tricky_mesh_ai.summarizer import (
    ConversationSummarizer,
    NullSummarizer,
    SUMMARY_SYSTEM_PROMPT,
    build_summarizer,
)


class FakeLLM:
    def __init__(self, reply="a summary of the chat"):
        self.reply = reply
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def complete(self, user_text, history=(), extra_system=None, system_override=None):
        with self._lock:
            self.calls.append(
                {
                    "user_text": user_text,
                    "history": list(history),
                    "extra_system": extra_system,
                    "system_override": system_override,
                }
            )
        return self.reply


def _turns(*contents):
    return [Turn(role=("user" if i % 2 == 0 else "assistant"), content=c, ts=0.0)
            for i, c in enumerate(contents)]


def _wait_for(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_get_returns_none_before_first_summary():
    s = ConversationSummarizer(llm_client=FakeLLM(), max_chars=800)
    try:
        assert s.get("!abc") is None
    finally:
        s.stop()


def test_schedule_calls_llm_with_summary_prompt():
    llm = FakeLLM(reply="User is Otto in Portsmouth.")
    s = ConversationSummarizer(llm_client=llm, max_chars=800)
    try:
        s.schedule("!abc", _turns("hi", "hello", "my name is Otto"))
        assert _wait_for(lambda: llm.calls), "LLM was never called"

        call = llm.calls[0]
        assert call["system_override"] == SUMMARY_SYSTEM_PROMPT
        assert "NEW EXCHANGES" in call["user_text"]
        assert "Otto" in call["user_text"]

        assert _wait_for(lambda: s.get("!abc") is not None)
        assert s.get("!abc") == "User is Otto in Portsmouth."
    finally:
        s.stop()


def test_summary_updates_on_repeated_schedule():
    llm = FakeLLM(reply="first summary")
    s = ConversationSummarizer(llm_client=llm, max_chars=800)
    try:
        s.schedule("!abc", _turns("x", "y"))
        assert _wait_for(lambda: s.get("!abc") == "first summary")

        llm.reply = "second summary incorporating prior"
        s.schedule("!abc", _turns("more", "stuff"))
        assert _wait_for(lambda: s.get("!abc") == "second summary incorporating prior")

        # Second call's user_text should reference the existing summary.
        assert len(llm.calls) == 2
        assert "EXISTING SUMMARY" in llm.calls[1]["user_text"]
        assert "first summary" in llm.calls[1]["user_text"]
    finally:
        s.stop()


def test_empty_turns_does_not_call_llm():
    llm = FakeLLM()
    s = ConversationSummarizer(llm_client=llm, max_chars=800)
    try:
        s.schedule("!abc", [])
        time.sleep(0.1)
        assert llm.calls == []
        assert s.get("!abc") is None
    finally:
        s.stop()


def test_worker_survives_llm_exception():
    class BoomLLM:
        def __init__(self):
            self.count = 0

        def complete(self, user_text, history=(), extra_system=None, system_override=None):
            self.count += 1
            if self.count == 1:
                raise RuntimeError("llm down")
            return "recovered summary"

    llm = BoomLLM()
    s = ConversationSummarizer(llm_client=llm, max_chars=800)
    try:
        s.schedule("!abc", _turns("a", "b"))
        # Wait briefly for the first (failed) call
        _wait_for(lambda: llm.count >= 1)
        assert s.get("!abc") is None  # no summary after failure

        # Second schedule should work — worker is still alive
        s.schedule("!abc", _turns("c", "d"))
        assert _wait_for(lambda: s.get("!abc") == "recovered summary")
    finally:
        s.stop()


def test_empty_llm_reply_does_not_overwrite():
    llm = FakeLLM(reply="good summary")
    s = ConversationSummarizer(llm_client=llm, max_chars=800)
    try:
        s.schedule("!abc", _turns("a"))
        assert _wait_for(lambda: s.get("!abc") == "good summary")

        llm.reply = "   "  # whitespace-only → treated as empty
        s.schedule("!abc", _turns("b"))
        # Give worker time to process
        time.sleep(0.1)
        # Old summary preserved
        assert s.get("!abc") == "good summary"
    finally:
        s.stop()


def test_null_summarizer_is_safe():
    n = NullSummarizer()
    assert n.get("!abc") is None
    n.schedule("!abc", _turns("x"))
    n.stop()


def test_build_summarizer_respects_enabled_flag():
    llm = FakeLLM()
    real = build_summarizer(enabled=True, llm_client=llm, max_chars=800)
    try:
        assert isinstance(real, ConversationSummarizer)
    finally:
        real.stop()

    null = build_summarizer(enabled=False, llm_client=llm, max_chars=800)
    assert isinstance(null, NullSummarizer)
