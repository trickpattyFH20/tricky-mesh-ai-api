import logging
import queue
import threading
from typing import Protocol

from .memory import Turn


log = logging.getLogger(__name__)


SUMMARY_SYSTEM_PROMPT = (
    "You maintain a compact running summary of a conversation between a user "
    "and an AI assistant over a LoRa mesh radio. Update the summary by "
    "incorporating the new exchanges provided. Focus on: stable facts about "
    "the user (name, location, node IDs, role), topics they care about, "
    "preferences, and any open threads. Drop small-talk and trivia. Write in "
    "plain prose, third-person. Do not address the user. Do not include a "
    "preamble. Return ONLY the updated summary text."
)


class _LLMLike(Protocol):
    def complete(
        self,
        user_text: str,
        history=(),
        extra_system: str | None = None,
        system_override: str | None = None,
    ) -> str: ...


_SENTINEL = object()


class ConversationSummarizer:
    """Per-sender running conversation summary maintained by a background
    worker thread. Memory calls schedule(sender, evicted_turns) when the
    verbatim window overflows; the worker folds those turns into the summary
    via an LLM call. The daemon reads the current summary via get(sender) and
    injects it into subsequent LLM calls as a second system message.

    Thread safety: schedule() is non-blocking (puts on a queue). get() takes
    a short lock. The worker runs concurrently and updates the summary dict
    under the same lock.
    """

    def __init__(self, llm_client: _LLMLike, max_chars: int = 800):
        self._llm = llm_client
        self._max_chars = int(max_chars)
        self._summaries: dict[str, str] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="conv-summarizer", daemon=True
        )
        self._thread.start()

    def get(self, sender: str) -> str | None:
        with self._lock:
            return self._summaries.get(sender)

    def schedule(self, sender: str, turns: list[Turn]) -> None:
        if not turns:
            return
        self._queue.put((sender, list(turns)))

    def stop(self) -> None:
        """Signal the worker to exit. Does not join — worker is daemon."""
        self._queue.put(_SENTINEL)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until the queue drains or timeout. For tests."""
        try:
            self._queue.join()
            return True
        except Exception:
            return False

    def _set_summary(self, sender: str, summary: str) -> None:
        with self._lock:
            self._summaries[sender] = summary

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SENTINEL:
                    return
                sender, turns = item
                self._process(sender, turns)
            except Exception:
                log.exception("summarizer worker error (continuing)")
            finally:
                self._queue.task_done()

    def _process(self, sender: str, turns: list[Turn]) -> None:
        existing = self.get(sender) or ""
        user_text = self._build_prompt(existing, turns)
        try:
            new_summary = self._llm.complete(
                user_text,
                history=(),
                extra_system=None,
                system_override=SUMMARY_SYSTEM_PROMPT,
            )
        except Exception as e:
            log.warning("summarizer LLM call failed for %s: %s", sender, e)
            return

        new_summary = (new_summary or "").strip()
        if not new_summary:
            return
        # Respect the configured cap (soft clip — prefer model compression).
        if len(new_summary) > self._max_chars * 2:
            new_summary = new_summary[: self._max_chars * 2].rstrip() + "…"

        self._set_summary(sender, new_summary)
        log.info(
            "updated summary for %s (%d chars, folded %d turns)",
            sender,
            len(new_summary),
            len(turns),
        )

    def _build_prompt(self, existing_summary: str, turns: list[Turn]) -> str:
        parts: list[str] = []
        if existing_summary:
            parts.append("EXISTING SUMMARY:")
            parts.append(existing_summary)
            parts.append("")
        parts.append("NEW EXCHANGES (oldest first):")
        for t in turns:
            label = "USER" if t.role == "user" else "ASSISTANT"
            parts.append(f"{label}: {t.content}")
        parts.append("")
        parts.append(
            f"Produce the updated summary. Keep it under {self._max_chars} "
            "characters. Return only the summary text, no preamble."
        )
        return "\n".join(parts)


class NullSummarizer:
    """No-op used when summarization is disabled."""

    def get(self, sender: str) -> str | None:
        return None

    def schedule(self, sender: str, turns) -> None:
        return

    def stop(self) -> None:
        return


def build_summarizer(
    enabled: bool, llm_client: _LLMLike, max_chars: int
):
    if enabled:
        return ConversationSummarizer(llm_client=llm_client, max_chars=max_chars)
    return NullSummarizer()
