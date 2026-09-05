"""Run-scoped, observation-only repeated output detection.

The detector never owns a worker lifecycle.  It consumes output already
correlated by the execution harness and returns compact metadata only; raw
output is never included in an event.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, NamedTuple


_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def normalize(value: str) -> str:
    """Make formatting-only differences irrelevant without matching one word."""

    if not isinstance(value, str):
        raise TypeError("output must be a string")
    return "".join(_WORD.findall(value.casefold()))


class RepeatedSuffix(NamedTuple):
    repeat_count: int
    unit_chars: int


def repeated_suffix(
    value: str, *, min_unit_chars: int = 24, max_unit_chars: int = 800, min_repeats: int = 4,
) -> RepeatedSuffix | None:
    """Return the shortest sufficiently long block repeated at the text tail."""

    if min_unit_chars < 1 or max_unit_chars < min_unit_chars or min_repeats < 2:
        raise ValueError("invalid repeated suffix thresholds")
    text = normalize(value)
    largest = min(max_unit_chars, len(text) // min_repeats)
    for size in range(min_unit_chars, largest + 1):
        unit = text[-size:]
        count = 1
        cursor = len(text) - size
        while cursor >= size and text[cursor - size:cursor] == unit:
            count += 1
            cursor -= size
        if count >= min_repeats:
            return RepeatedSuffix(count, size)
    return None


@dataclass(frozen=True)
class RunScope:
    core_run_id: str
    openclaw_run_id: str
    session_key: str
    agent_id: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in self):
            raise ValueError("all run correlation fields are required")

    def __iter__(self):
        return iter((self.core_run_id, self.openclaw_run_id, self.session_key, self.agent_id))


@dataclass(frozen=True)
class LoopDetectorConfig:
    tail_chars: int = 12_000
    min_unit_chars: int = 24
    max_unit_chars: int = 800
    min_repeats: int = 4
    cooldown_seconds: float = 300.0
    exception_patterns: tuple[str, ...] = (
        "context compression", "compressing context", "model loading",
        "loading model", "initialization", "initializing runtime",
    )

    def __post_init__(self) -> None:
        if self.tail_chars < 1 or self.min_unit_chars < 1:
            raise ValueError("detector sizes must be positive")
        if self.max_unit_chars < self.min_unit_chars or self.min_repeats < 2:
            raise ValueError("invalid detector thresholds")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")


class RunScopedLoopDetector:
    """Keep isolated bounded tails and suppress duplicate observations."""

    def __init__(
        self, config: LoopDetectorConfig | None = None, *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or LoopDetectorConfig()
        self._clock = clock
        self._tails: dict[RunScope, str] = {}
        self._last_events: dict[tuple[RunScope, str], float] = {}
        self._lock = threading.RLock()

    def observe(self, scope: RunScope, output: str) -> dict[str, object] | None:
        if not isinstance(output, str):
            raise TypeError("output must be a string")
        with self._lock:
            tail = (self._tails.get(scope, "") + output)[-self.config.tail_chars:]
            self._tails[scope] = tail
            normalized = normalize(tail)
            match = repeated_suffix(
                normalized,
                min_unit_chars=self.config.min_unit_chars,
                max_unit_chars=self.config.max_unit_chars,
                min_repeats=self.config.min_repeats,
            )
            if match is None:
                return None
            repeated_unit = normalized[-match.unit_chars:]
            if any(normalize(pattern) in repeated_unit for pattern in self.config.exception_patterns):
                return None
            signature = hashlib.sha256(repeated_unit.encode("utf-8")).hexdigest()
            key = (scope, signature)
            now = self._clock()
            last = self._last_events.get(key)
            if last is not None and now - last < self.config.cooldown_seconds:
                return None
            self._last_events[key] = now
            return {
                "event": "LOOP_SUSPECTED",
                "core_run_id": scope.core_run_id,
                "openclaw_run_id": scope.openclaw_run_id,
                "sessionKey": scope.session_key,
                "agentId": scope.agent_id,
                "repeat_count": match.repeat_count,
                "unit_chars": match.unit_chars,
            }

    def clear(self, scope: RunScope) -> None:
        with self._lock:
            self._tails.pop(scope, None)
            for key in [key for key in self._last_events if key[0] == scope]:
                self._last_events.pop(key, None)
