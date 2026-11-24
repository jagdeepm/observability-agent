"""Tests for the LogsAgent — NL → LogQL → Evidence.

Seam: agent invocation → Evidence. Assertions target evidence.signals (computed
in code) rather than evidence.summary (produced by the mock LLM).
"""

from __future__ import annotations

import pytest

from observability_agent.agents.logs import LogQLOutput, LogsAgent, LogsSummaryOutput
from observability_agent.domain.models import EvidenceSource
from observability_agent.infrastructure.llm_provider import MockLLMProvider


def make_llm() -> MockLLMProvider:
    llm = MockLLMProvider()
    llm.register(LogQLOutput, LogQLOutput(
        logql='{service="checkout"} |= "error"',
        explanation="Error lines for checkout",
    ))
    llm.register(LogsSummaryOutput, LogsSummaryOutput(
        summary="The checkout container is being OOM killed repeatedly.",
    ))
    return llm


def loki_payload(lines: list[str], level: str = "error") -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service": "checkout", "level": level},
                    "values": [[str(1700000000000000000 + i), line] for i, line in enumerate(lines)],
                }
            ],
        },
    }


class StubLoki:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def query_logql(self, logql: str, limit: int = 100) -> dict:
        return self.payload


@pytest.mark.asyncio
async def test_duplicate_lines_counted_exactly_and_deduplicated():
    """200 identical error lines → exact count, collapsed to one distinct message."""
    payload = loki_payload(["OOMKilled container checkout"] * 200)
    agent = LogsAgent(llm=make_llm(), loki=StubLoki(payload))

    evidence = await agent("why is checkout failing?", hypothesis_id="hyp-1")

    assert evidence.source == EvidenceSource.LOKI
    assert evidence.signals["error_count"] == 200
    assert evidence.signals["total_lines"] == 200
    assert evidence.signals["distinct_messages"] == 1


@pytest.mark.asyncio
async def test_error_count_excludes_non_error_lines():
    """Only error-level lines are counted, not every line."""
    payload = loki_payload(
        ["INFO request handled ok"] * 8 + ["ERROR upstream timeout"] * 3,
        level="info",
    )
    agent = LogsAgent(llm=make_llm(), loki=StubLoki(payload))

    evidence = await agent("recent logs")

    assert evidence.signals["total_lines"] == 11
    assert evidence.signals["error_count"] == 3


@pytest.mark.asyncio
async def test_repeated_lines_collapse_in_llm_prompt():
    """The prompt carries a counted group, not 200 repeated lines."""
    llm = make_llm()
    payload = loki_payload(["OOMKilled container checkout"] * 200)
    agent = LogsAgent(llm=llm, loki=StubLoki(payload))

    await agent("why is checkout failing?")

    summary_prompt = llm.calls[-1]["prompt"]
    assert summary_prompt.count("OOMKilled container checkout") == 1
    assert "200" in summary_prompt


@pytest.mark.asyncio
async def test_empty_payload_yields_no_signals():
    """No logs → no computed signals, no exception."""
    payload = {"status": "success", "data": {"resultType": "streams", "result": []}}
    agent = LogsAgent(llm=make_llm(), loki=StubLoki(payload))

    evidence = await agent("any logs?")

    assert evidence.signals == {}
    assert evidence.source == EvidenceSource.LOKI


@pytest.mark.asyncio
async def test_metric_logql_result_is_not_counted_as_log_lines():
    """rate()/count_over_time() return samples, not lines — no counts claimed."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"service": "checkout"},
                 "values": [[1700000000, "3"], [1700000015, "7"]]},
            ],
        },
    }
    agent = LogsAgent(llm=make_llm(), loki=StubLoki(payload))

    evidence = await agent("error rate?")

    assert evidence.signals == {}


@pytest.mark.asyncio
async def test_distinct_message_rendering_is_capped():
    """Bounded prompts are the point — many distinct messages must not stream through."""
    llm = make_llm()
    # Distinct wording, not distinct numbers — lines differing only by digits are
    # deliberately normalized into one group.
    payload = loki_payload(
        [f"failure in module {'ghijklmnopqrstuvwxyz'[i % 20]}{'z' * (i // 20)}" for i in range(200)]
    )
    agent = LogsAgent(llm=llm, loki=StubLoki(payload))

    evidence = await agent("what failed?")

    prompt = llm.calls[-1]["prompt"]
    assert evidence.signals["distinct_messages"] == 200
    assert prompt.count("[ERROR] ×") <= 20
    assert "more distinct messages" in prompt


@pytest.mark.asyncio
async def test_malformed_payload_does_not_raise():
    """An unexpected payload shape degrades to empty signals."""
    agent = LogsAgent(llm=make_llm(), loki=StubLoki({"unexpected": "shape"}))

    evidence = await agent("any logs?")

    assert evidence.signals == {}
    assert not evidence.summary.startswith("Error:")


@pytest.mark.asyncio
async def test_loki_unreachable_returns_error_evidence():
    """When Loki is down, return error Evidence with no signals."""
    class FailingLoki:
        async def query_logql(self, logql: str, limit: int = 100) -> dict:
            raise ConnectionError("connection refused")

    agent = LogsAgent(llm=make_llm(), loki=FailingLoki())

    evidence = await agent("any logs?")

    assert evidence.summary.startswith("Error: Loki query failed")
    assert evidence.signals == {}
