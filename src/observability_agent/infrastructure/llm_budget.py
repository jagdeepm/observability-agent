"""Token budget guardrail — an in-memory ceiling on cumulative LLM token usage.

See docs/specs/investigation-cost-guardrails.md, Part 2. Pure arithmetic and
clock comparisons — no dependency on pydantic-ai or httpx — so it is testable
without a real LLM call. Wiring into the actual provider lives in
infrastructure/llm_provider.py.

In-memory only: state resets on process restart. Named here as a deliberate
boundary, the same stance this project already takes on
InMemoryIncidentRepository, not fixed by this module.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone


class LLMBudgetExceededError(Exception):
    """Raised when a configured token ceiling has been reached or passed.

    Carries which window tripped and the numbers, so the message alone is
    actionable without digging into logs: "LLM daily token budget exceeded:
    503,412 / 500,000 tokens used since 2026-08-14T00:00:00+00:00".
    """

    def __init__(self, window: str, used: int, limit: int, window_start: datetime) -> None:
        self.window = window
        self.used = used
        self.limit = limit
        self.window_start = window_start
        super().__init__(
            f"LLM {window} token budget exceeded: {used:,} / {limit:,} tokens "
            f"used since {window_start.isoformat()}"
        )


class TokenBudget:
    """Tracks cumulative input+output tokens against two independent ceilings.

    Both ceilings are optional; a ceiling left None never raises regardless of
    usage — this is what lets the feature ship with no default, since a number
    picked without knowing an operator's actual tolerance is a guess wearing a
    default's clothes.

    Windows are calendar-aligned (UTC day; UTC week starting Monday), checked
    lazily — record() and raise_if_exceeded() roll a window's counter to zero
    if its boundary has passed since the last check, rather than running a
    background timer. A precise rate limiter is not the goal; a coarse,
    explicable safety net is.
    """

    def __init__(
        self,
        daily_limit: int | None = None,
        weekly_limit: int | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.daily_limit = daily_limit
        self.weekly_limit = weekly_limit
        self._now = now or (lambda: datetime.now(timezone.utc))
        clock_now = self._now()
        self._day_start = _day_start(clock_now)
        self._week_start = _week_start(clock_now)
        self._daily_used = 0
        self._weekly_used = 0

    def record(self, input_tokens: int, output_tokens: int) -> None:
        """Add usage from a completed call to both windows."""
        self._roll_windows()
        total = input_tokens + output_tokens
        self._daily_used += total
        self._weekly_used += total

    def raise_if_exceeded(self) -> None:
        """Raise LLMBudgetExceededError if either window is at or past its ceiling.

        Read-only — never records usage and never resets a window early, so
        calling this repeatedly with no intervening record() is free.
        """
        self._roll_windows()
        if self.daily_limit is not None and self._daily_used >= self.daily_limit:
            raise LLMBudgetExceededError("daily", self._daily_used, self.daily_limit, self._day_start)
        if self.weekly_limit is not None and self._weekly_used >= self.weekly_limit:
            raise LLMBudgetExceededError("weekly", self._weekly_used, self.weekly_limit, self._week_start)

    def status(self) -> dict[str, dict[str, int | None]]:
        """Snapshot for /health — used/limit per window. Never raises."""
        self._roll_windows()
        return {
            "daily": {"used": self._daily_used, "limit": self.daily_limit},
            "weekly": {"used": self._weekly_used, "limit": self.weekly_limit},
        }

    def _roll_windows(self) -> None:
        now = self._now()
        day_start = _day_start(now)
        if day_start > self._day_start:
            self._day_start = day_start
            self._daily_used = 0
        week_start = _week_start(now)
        if week_start > self._week_start:
            self._week_start = week_start
            self._weekly_used = 0


def _day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(dt: datetime) -> datetime:
    """Monday 00:00 of dt's week — datetime.weekday() is 0 for Monday."""
    return _day_start(dt) - timedelta(days=dt.weekday())
