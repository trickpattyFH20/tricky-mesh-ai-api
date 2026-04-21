import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Turn:
    role: str  # "user" or "assistant"
    content: str
    ts: float


class _SummarizerLike(Protocol):
    def schedule(self, sender: str, turns: list[Turn]) -> None: ...


class _NoopSummarizer:
    def schedule(self, sender: str, turns: list[Turn]) -> None:
        return


class ConversationMemory:
    """Per-sender conversation history with:
    - TTL eviction (entries older than `ttl_seconds` are pruned on read/write)
    - Soft cap via `summary_trigger_turns`: when the queue exceeds this, the
      oldest (len - summary_keep_turns) turns are handed to the summarizer
      and removed from the verbatim queue.
    - Hard cap via `max_turns`: safety ceiling if summarization is disabled
      or the summarizer is broken — oldest entries are force-dropped.

    `max_turns` = 0 disables memory entirely (stateless).
    """

    def __init__(
        self,
        max_turns: int,
        ttl_seconds: float,
        summary_trigger_turns: int = 0,
        summary_keep_turns: int = 0,
        summarizer: _SummarizerLike | None = None,
    ):
        self.max_turns = int(max_turns)
        self.ttl = float(ttl_seconds)
        self.summary_trigger_turns = int(summary_trigger_turns)
        self.summary_keep_turns = int(summary_keep_turns)
        self._summarizer: _SummarizerLike = summarizer or _NoopSummarizer()
        self._store: dict[str, deque[Turn]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.max_turns > 0

    @property
    def summary_active(self) -> bool:
        return (
            self.summary_trigger_turns > 0
            and self.summary_keep_turns > 0
            and self.summary_keep_turns < self.summary_trigger_turns
        )

    def _prune_ttl(self, dq: deque[Turn], now: float) -> None:
        while dq and now - dq[0].ts > self.ttl:
            dq.popleft()

    def get(self, sender: str) -> list[Turn]:
        if not self.enabled:
            return []
        now = time.time()
        with self._lock:
            dq = self._store.get(sender)
            if not dq:
                return []
            self._prune_ttl(dq, now)
            return list(dq)

    def append(self, sender: str, role: str, content: str) -> None:
        if not self.enabled:
            return
        now = time.time()
        turns_to_summarize: list[Turn] = []
        with self._lock:
            dq = self._store.setdefault(sender, deque())
            self._prune_ttl(dq, now)
            dq.append(Turn(role, content, now))

            # Summary soft cap: peel off oldest turns down to keep_turns.
            if self.summary_active and len(dq) > self.summary_trigger_turns:
                excess = len(dq) - self.summary_keep_turns
                for _ in range(excess):
                    turns_to_summarize.append(dq.popleft())

            # Hard safety ceiling — drops without summary.
            while len(dq) > self.max_turns:
                dq.popleft()

        if turns_to_summarize:
            self._summarizer.schedule(sender, turns_to_summarize)
