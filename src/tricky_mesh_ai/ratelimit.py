import threading
import time


class RateLimiter:
    """Per-key cooldown. Records the timestamp of each accepted request;
    subsequent requests within `cooldown_seconds` are rejected."""

    def __init__(self, cooldown_seconds: float):
        self.cooldown = float(cooldown_seconds)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.cooldown > 0

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds). When disabled, always allows."""
        if not self.enabled:
            return True, 0.0
        t = time.monotonic() if now is None else now
        with self._lock:
            last = self._last.get(key)
            if last is not None and (elapsed := t - last) < self.cooldown:
                return False, self.cooldown - elapsed
            self._last[key] = t
            return True, 0.0
