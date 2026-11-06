# ported from deal-terms-desk service/limits.py on 2026-11-05, unchanged
import ipaddress
import threading
import time
from collections import deque

PRUNE_EVERY = 1024  # calls between sweeps of keys whose window has emptied


def client_key(host: str) -> str:
    """The address a rate limit counts: IPv4 as is; IPv6 by its /64 (one host routinely holds a whole /64);
    an IPv4-mapped IPv6 address as its IPv4; anything unparseable unchanged."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class Buckets:
    """At most `per_window` calls per key in any `window_s` seconds (a sliding log). allow() is 0.0 when the call
    may go ahead, else the seconds until the oldest call leaves the window (for Retry-After)."""

    def __init__(self, per_window: int, window_s: float, clock=time.monotonic):
        if per_window < 0 or window_s <= 0:
            raise ValueError("a bucket needs per_window >= 0 and window_s > 0")
        self.per_window, self.window_s, self.clock = per_window, float(window_s), clock
        self._log: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def allow(self, key: str) -> float:
        if self.per_window == 0:  # a closed limit: never allows
            return self.window_s
        with self._lock:
            now = self.clock()
            self._calls += 1
            if self._calls % PRUNE_EVERY == 0:
                self._prune(now)
            log = self._log.setdefault(key, deque())
            while log and log[0] <= now - self.window_s:
                log.popleft()
            if len(log) < self.per_window:
                log.append(now)
                return 0.0
            return max(log[0] + self.window_s - now, 0.001)

    def _prune(self, now: float) -> None:
        for k in [k for k, log in self._log.items() if not log or log[-1] <= now - self.window_s]:
            del self._log[k]


class Slots:
    """At most n calls at once; a full set refuses at once instead of queueing (the service answers `busy`).
    Slots(0) never acquires."""

    def __init__(self, n: int):
        self._sem = threading.BoundedSemaphore(n)

    def try_acquire(self) -> bool:
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        self._sem.release()  # BoundedSemaphore: releasing more than was taken raises ValueError
