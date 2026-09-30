import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._windows: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        window = self._windows[key]
        cutoff = current - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if not window:
            self._windows.pop(key, None)
            window = self._windows[key]
        if len(window) >= self.limit:
            return False
        window.append(current)
        return True

