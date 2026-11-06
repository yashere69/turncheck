"""A simulated clock. Scenarios never read wall time, so a 25-hour gap costs zero seconds."""

from __future__ import annotations

import re

# 2026-01-01T10:00:00Z. Fixed so that recorded envelopes are byte-identical across runs.
DEFAULT_EPOCH = 1767261600

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")


def parse_duration(text: str | int) -> int:
    """Parse '90s', '15m', '25h', '2d' (or a bare int of seconds) into seconds."""
    if isinstance(text, int):
        return text
    m = _DURATION.match(str(text))
    if not m:
        raise ValueError(f"bad duration {text!r}; use e.g. 30s, 15m, 25h, 2d")
    return int(m.group(1)) * _UNITS[m.group(2)]


class SimClock:
    def __init__(self, start: int = DEFAULT_EPOCH, tick: int = 5):
        self.now = start
        self.tick = tick  # seconds that pass between consecutive customer messages

    def advance(self, seconds: int) -> int:
        if seconds < 0:
            raise ValueError("time only moves forward")
        self.now += seconds
        return self.now

    def step(self) -> int:
        return self.advance(self.tick)
