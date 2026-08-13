"""Tests for the TraceAgent — NL → TraceQL → Evidence.

Seam: agent invocation → Evidence. Assertions target evidence.signals.
"""

from __future__ import annotations

import pytest

from observability_agent.agents.traces import TraceAgent, TraceQLOutput, TracesSummaryOutput
from observability_agent.domain.models import EvidenceSource
from observability_agent.infrastructure.llm_provider import MockLLMProvider


def make_llm() -> MockLLMProvider:
    llm = MockLLMProvider()
    llm.register(TraceQLOutput, TraceQLOutput(
        traceql='{ .service.name = "checkout" } | duration > 100ms',
        explanation="Slow checkout spans",
    ))
    llm.register(TracesSummaryOutput, TracesSummaryOutput(
        summary="Most of the request time is spent in the payment call.",
    ))
    return llm


def tempo_payload() -> dict:
    """A three-level span tree: checkout → payment → db, db slowest."""
    return {
        "traces": [
            {
                "traceID": "abc123",
                "rootServiceName": "checkout",
                "rootTraceName": "POST /checkout",
                "durationMs": 1200,
                "spanSet": {
                    "spans": [
                        {
                            "spanID": "s1",
                            "name": "POST /checkout",
                            "durationNanos": "1200000000",
                            "attributes": {"service.name": "checkout"},
                        },
                        {
                            "spanID": "s2",
                            "parentSpanID": "s1",
                            "name": "charge card",
                            "durationNanos": "1100000000",
                            "attributes": {"service.name": "payment"},
                        },
                        {
                            "spanID": "s3",
                            "parentSpanID": "s2",
                            "name": "SELECT accounts",
                            "durationNanos": "1050000000",
                            "attributes": {"service.name": "postgres"},
                        },
                    ]
                },
            }
        ]
    }


class StubTempo:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def query_traceql(self, traceql: str, limit: int = 20) -> dict:
        return self.payload


@pytest.mark.asyncio
async def test_slowest_span_identified_by_self_time():
    """The bottleneck is found by self time, not total duration.

    Ranking by total duration always returns the root span, whose duration
    contains every child's. Here the root is 1200ms but spends 100ms of it
    itself; the database leaf is the real bottleneck at 1050ms.
    """
    agent = TraceAgent(llm=make_llm(), tempo=StubTempo(tempo_payload()))

    evidence = await agent("where is checkout spending time?", hypothesis_id="hyp-1")

    assert evidence.source == EvidenceSource.TEMPO
    assert evidence.signals["slowest_span"] == "SELECT accounts"
    assert evidence.signals["slowest_service"] == "postgres"
    assert evidence.signals["slowest_self_ms"] == pytest.approx(1050.0)
    assert evidence.signals["span_count"] == 3


@pytest.mark.asyncio
async def test_span_hierarchy_preserved_in_prompt():
    """Parent/child structure survives into the LLM prompt as indentation."""
    llm = make_llm()
    agent = TraceAgent(llm=llm, tempo=StubTempo(tempo_payload()))

    await agent("where is checkout spending time?")

    prompt = llm.calls[-1]["prompt"]
    root_indent = prompt.index("POST /checkout") - prompt.rindex("\n", 0, prompt.index("POST /checkout"))
    child_indent = prompt.index("charge card") - prompt.rindex("\n", 0, prompt.index("charge card"))
    leaf_indent = prompt.index("SELECT accounts") - prompt.rindex("\n", 0, prompt.index("SELECT accounts"))
    assert root_indent < child_indent < leaf_indent


@pytest.mark.asyncio
async def test_flat_spans_without_parents_still_ranked():
    """Spans with no parent links still yield a slowest span."""
    payload = {
        "traces": [
            {
                "traceID": "t1",
                "rootServiceName": "checkout",
                "spanSet": {
                    "spans": [
                        {"spanID": "a", "name": "fast", "durationNanos": "1000000",
                         "attributes": {"service.name": "checkout"}},
                        {"spanID": "b", "name": "slow", "durationNanos": "9000000",
                         "attributes": {"service.name": "cart"}},
                    ]
                },
            }
        ]
    }
    agent = TraceAgent(llm=make_llm(), tempo=StubTempo(payload))

    evidence = await agent("slow spans?")

    assert evidence.signals["slowest_span"] == "slow"
    assert evidence.signals["slowest_service"] == "cart"
    assert evidence.signals["span_count"] == 2


@pytest.mark.asyncio
async def test_otlp_attribute_lists_are_understood():
    """Real Tempo returns attributes as an OTLP key/value list, not a mapping."""
    payload = {
        "traces": [
            {
                "traceID": "t1",
                "rootServiceName": "checkout",
                "spanSets": [
                    {
                        "spans": [
                            {
                                "spanID": "a",
                                "name": "GET /cart",
                                "durationNanos": "50000000",
                                "attributes": [
                                    {"key": "service.name",
                                     "value": {"stringValue": "cartservice"}},
                                ],
                            },
                        ]
                    }
                ],
            }
        ]
    }
    agent = TraceAgent(llm=make_llm(), tempo=StubTempo(payload))

    evidence = await agent("slow spans?")

    assert evidence.signals["slowest_service"] == "cartservice"
    assert evidence.signals["slowest_span"] == "GET /cart"
    assert evidence.signals["slowest_self_ms"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_empty_payload_yields_no_signals():
    agent = TraceAgent(llm=make_llm(), tempo=StubTempo({"traces": []}))

    evidence = await agent("any traces?")

    assert evidence.signals == {}


@pytest.mark.asyncio
async def test_malformed_payload_does_not_raise():
    agent = TraceAgent(llm=make_llm(), tempo=StubTempo({"unexpected": "shape"}))

    evidence = await agent("any traces?")

    assert evidence.signals == {}
    assert not evidence.summary.startswith("Error:")
