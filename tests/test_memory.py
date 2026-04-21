import time

from tricky_mesh_ai.memory import ConversationMemory, Turn


class FakeSummarizer:
    def __init__(self):
        self.calls: list[tuple[str, list[Turn]]] = []

    def schedule(self, sender, turns):
        self.calls.append((sender, list(turns)))


def test_disabled_returns_nothing():
    m = ConversationMemory(max_turns=0, ttl_seconds=900)
    m.append("!a", "user", "hi")
    assert m.get("!a") == []


def test_roundtrip_ordered():
    m = ConversationMemory(max_turns=100, ttl_seconds=900)
    m.append("!a", "user", "q1")
    m.append("!a", "assistant", "a1")
    m.append("!a", "user", "q2")
    got = m.get("!a")
    assert [t.content for t in got] == ["q1", "a1", "q2"]
    assert [t.role for t in got] == ["user", "assistant", "user"]


def test_ttl_prunes_on_read(monkeypatch):
    t = [1000.0]

    def fake_time():
        return t[0]

    monkeypatch.setattr(time, "time", fake_time)
    m = ConversationMemory(max_turns=10, ttl_seconds=60)
    m.append("!a", "user", "old")
    t[0] += 120
    m.append("!a", "user", "new")
    assert [x.content for x in m.get("!a")] == ["new"]


def test_per_sender_isolation():
    m = ConversationMemory(max_turns=100, ttl_seconds=900)
    m.append("!a", "user", "a-msg")
    m.append("!b", "user", "b-msg")
    assert [x.content for x in m.get("!a")] == ["a-msg"]
    assert [x.content for x in m.get("!b")] == ["b-msg"]


def test_summary_trigger_evicts_to_keep_level():
    summ = FakeSummarizer()
    m = ConversationMemory(
        max_turns=100,
        ttl_seconds=900,
        summary_trigger_turns=6,
        summary_keep_turns=4,
        summarizer=summ,
    )
    for i in range(6):
        m.append("!a", "user", f"msg{i}")
    assert summ.calls == []  # at threshold, not over yet
    assert len(m.get("!a")) == 6

    m.append("!a", "user", "msg6")  # this pushes us to 7, exceeds trigger=6
    # Evict 7 - keep(4) = 3 oldest turns → summarizer
    assert len(summ.calls) == 1
    sender, evicted = summ.calls[0]
    assert sender == "!a"
    assert [t.content for t in evicted] == ["msg0", "msg1", "msg2"]

    # Memory now has 4 verbatim entries (msg3..msg6)
    remaining = m.get("!a")
    assert [t.content for t in remaining] == ["msg3", "msg4", "msg5", "msg6"]


def test_hard_cap_evicts_without_summary_when_summarizer_noop():
    m = ConversationMemory(max_turns=5, ttl_seconds=900)
    for i in range(10):
        m.append("!a", "user", f"msg{i}")
    # With no summarizer + summary_trigger_turns=0, hard cap drops oldest.
    got = m.get("!a")
    assert len(got) == 5
    assert [t.content for t in got] == [f"msg{i}" for i in range(5, 10)]


def test_no_eviction_below_trigger():
    summ = FakeSummarizer()
    m = ConversationMemory(
        max_turns=100,
        ttl_seconds=900,
        summary_trigger_turns=50,
        summary_keep_turns=40,
        summarizer=summ,
    )
    for i in range(10):
        m.append("!a", "user", f"msg{i}")
    assert summ.calls == []
