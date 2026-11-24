"""Tests for TokenBudget — pure arithmetic and clock comparisons, no LLM involved.

Spec: docs/specs/investigation-cost-guardrails.md, Part 2.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from observability_agent.infrastructure.llm_budget import (
    LLMBudgetExceededError,
    TokenBudget,
)


class FakeClock:
    """A settable clock for testing window rollover without real time passing."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


# A Wednesday, well clear of both a day and a week boundary.
WEDNESDAY = datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc)


def make_clock(start: datetime = WEDNESDAY) -> FakeClock:
    return FakeClock(start)


# ---------------------------------------------------------------------------
# Accumulation and ceilings
# ---------------------------------------------------------------------------


def test_records_accumulate():
    budget = TokenBudget(daily_limit=1000, weekly_limit=5000, now=make_clock())

    budget.record(input_tokens=100, output_tokens=50)
    budget.record(input_tokens=200, output_tokens=25)

    status = budget.status()
    assert status["daily"]["used"] == 375
    assert status["weekly"]["used"] == 375


def test_does_not_raise_below_ceiling():
    budget = TokenBudget(daily_limit=100, now=make_clock())
    budget.record(input_tokens=60, output_tokens=39)

    budget.raise_if_exceeded()  # does not raise


def test_boundary_is_inclusive():
    """Reaching the ceiling exactly raises — not only exceeding it."""
    budget = TokenBudget(daily_limit=100, now=make_clock())
    budget.record(input_tokens=60, output_tokens=40)  # exactly 100

    with pytest.raises(LLMBudgetExceededError):
        budget.raise_if_exceeded()


def test_raises_past_the_ceiling():
    budget = TokenBudget(daily_limit=100, now=make_clock())
    budget.record(input_tokens=80, output_tokens=30)  # 110

    with pytest.raises(LLMBudgetExceededError):
        budget.raise_if_exceeded()


def test_error_names_the_window_and_numbers():
    budget = TokenBudget(daily_limit=100, now=make_clock())
    budget.record(input_tokens=100, output_tokens=0)

    with pytest.raises(LLMBudgetExceededError) as exc_info:
        budget.raise_if_exceeded()

    err = exc_info.value
    assert err.window == "daily"
    assert err.used == 100
    assert err.limit == 100
    assert "daily" in str(err)
    assert "100" in str(err)


def test_weekly_ceiling_is_independent_of_daily():
    """A generous daily limit doesn't hide a tighter weekly one."""
    budget = TokenBudget(daily_limit=1_000_000, weekly_limit=100, now=make_clock())
    budget.record(input_tokens=100, output_tokens=0)

    with pytest.raises(LLMBudgetExceededError) as exc_info:
        budget.raise_if_exceeded()

    assert exc_info.value.window == "weekly"


def test_daily_ceiling_trips_independently_of_weekly():
    budget = TokenBudget(daily_limit=100, weekly_limit=1_000_000, now=make_clock())
    budget.record(input_tokens=100, output_tokens=0)

    with pytest.raises(LLMBudgetExceededError) as exc_info:
        budget.raise_if_exceeded()

    assert exc_info.value.window == "daily"


def test_unset_ceilings_never_raise():
    """Both env vars unset — no ceiling configured, current behavior preserved."""
    budget = TokenBudget(daily_limit=None, weekly_limit=None, now=make_clock())
    budget.record(input_tokens=10_000_000, output_tokens=10_000_000)

    budget.raise_if_exceeded()  # does not raise


def test_one_ceiling_unset_the_other_still_enforced():
    budget = TokenBudget(daily_limit=100, weekly_limit=None, now=make_clock())
    budget.record(input_tokens=100, output_tokens=0)

    with pytest.raises(LLMBudgetExceededError):
        budget.raise_if_exceeded()


# ---------------------------------------------------------------------------
# Read-only check
# ---------------------------------------------------------------------------


def test_checking_does_not_record_usage():
    budget = TokenBudget(daily_limit=100, now=make_clock())

    budget.raise_if_exceeded()
    budget.raise_if_exceeded()
    budget.raise_if_exceeded()

    assert budget.status()["daily"]["used"] == 0


# ---------------------------------------------------------------------------
# Window rollover — calendar-aligned, checked lazily
# ---------------------------------------------------------------------------


def test_daily_window_resets_at_midnight_utc():
    clock = make_clock(datetime(2026, 8, 12, 23, 59, 0, tzinfo=timezone.utc))
    budget = TokenBudget(daily_limit=100, now=clock)
    budget.record(input_tokens=90, output_tokens=0)

    clock.advance(timedelta(minutes=2))  # crosses into 2026-08-13
    budget.raise_if_exceeded()  # would have raised (90 < 100 but close) — still shouldn't here
    budget.record(input_tokens=90, output_tokens=0)

    # If the window hadn't rolled over, cumulative usage would be 180 > 100.
    assert budget.status()["daily"]["used"] == 90
    budget.raise_if_exceeded()  # does not raise — new day, fresh counter


def test_daily_window_does_not_reset_within_the_same_day():
    clock = make_clock(datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc))
    budget = TokenBudget(daily_limit=1000, now=clock)
    budget.record(input_tokens=50, output_tokens=0)

    clock.advance(timedelta(hours=5))
    budget.record(input_tokens=50, output_tokens=0)

    assert budget.status()["daily"]["used"] == 100


def test_weekly_window_resets_at_monday_midnight_utc():
    # 2026-08-16 is a Sunday; 2026-08-17 is the following Monday.
    clock = make_clock(datetime(2026, 8, 16, 23, 59, 0, tzinfo=timezone.utc))
    budget = TokenBudget(weekly_limit=100, now=clock)
    budget.record(input_tokens=90, output_tokens=0)

    clock.advance(timedelta(minutes=2))  # crosses into Monday, a new week
    budget.record(input_tokens=90, output_tokens=0)

    assert budget.status()["weekly"]["used"] == 90
    budget.raise_if_exceeded()  # does not raise — new week, fresh counter


def test_weekly_window_does_not_reset_within_the_same_week():
    # A Tuesday and a Thursday of the same ISO week.
    clock = make_clock(datetime(2026, 8, 11, 10, 0, 0, tzinfo=timezone.utc))
    budget = TokenBudget(weekly_limit=1000, now=clock)
    budget.record(input_tokens=50, output_tokens=0)

    clock.advance(timedelta(days=2))
    budget.record(input_tokens=50, output_tokens=0)

    assert budget.status()["weekly"]["used"] == 100


def test_status_reports_both_windows():
    budget = TokenBudget(daily_limit=100, weekly_limit=500, now=make_clock())
    budget.record(input_tokens=10, output_tokens=10)

    status = budget.status()

    assert status == {
        "daily": {"used": 20, "limit": 100},
        "weekly": {"used": 20, "limit": 500},
    }
