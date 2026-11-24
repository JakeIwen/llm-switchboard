"""At most one log line a minute per key, for the relay's repeated refusals
and codes (docs/voice-desk.md: repeated identical refusals are logged at
most once a minute). Keys are wire codes or fixed diagnostics, so the table
stays as small as their sets. Standard library only.

`link_hub._RateLog` (Pi) is the same rule written before this module; it
stays private there until the Pi side can import this one.
"""

import threading
import time
from typing import Callable

MINUTE_SECONDS = 60.0


class RateLog:
    def __init__(self, clock: Callable[[], float] = time.monotonic, interval: float = MINUTE_SECONDS):
        self._clock = clock
        self._interval = interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def due(self, key: str) -> bool:
        """True (and the key's minute starts) when `key` wasn't logged in the last interval."""
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._interval:
                return False
            self._last[key] = now
            return True
