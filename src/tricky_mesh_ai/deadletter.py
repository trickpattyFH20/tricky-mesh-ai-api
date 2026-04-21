import json
import logging
import threading
import time
from pathlib import Path


log = logging.getLogger(__name__)


class DeadLetterLog:
    """Append-only JSONL log of dropped / failed messages. Path=None disables."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def record(
        self,
        reason: str,
        from_id: str,
        text: str,
        extra: dict | None = None,
    ) -> None:
        if self.path is None:
            return
        entry = {
            "ts": time.time(),
            "reason": reason,
            "fromId": from_id,
            "text": text,
        }
        if extra:
            entry.update(extra)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
        except OSError as e:
            log.warning("dead-letter write to %s failed: %s", self.path, e)
