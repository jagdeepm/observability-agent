"""Tests for the MetricsAgent — NL → PromQL → Evidence pipeline."""

from __future__ import annotations

import pytest

from observability_agent.agents.metrics import MetricsAgent, MetricsSummaryOutput, PromQLOutput
from observability_agent.domain.models import EvidenceSource
from observability_agent.infrastructure.llm_provider import MockLLMProvider


@pytest.mark.asyncio
async def test_metrics_agent_nl_to_promql():
    """NL query is translated to PromQL and executed."""
    mock_llm = MockLLMProvider()

    # Register LLM responses: PromQL translation + summary
    mock_llm.register(PromQLOutput, PromQLOutput(
        promql='rate(http_request_duration_seconds_sum{service="checkout"}[5m])',
        explanation="Rate of request duration sum for checkout service over 5 minutes",
    ))
    mock_llm.register(MetricsSummaryOutput, MetricsSummaryOutput(
        summary="p95 latency is 800ms, up from 200ms baseline. Anomaly detected.",
        anomaly_detected=True,
    ))

    # Use a mock Prometheus client
    class MockPrometheus:
        async def query_promql(self, promql: str) -> dict:
            return {"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"service": "checkout"}, "value": [1234567890, "0.8"]}
            ]}}

    agent = MetricsAgent(llm=mock_llm, prometheus=MockPrometheus())

    evidence = await agent(
        "what is the p95 latency for checkout service?",
        hypothesis_id="hyp-123",
        extra_context={"alert_labels": {"service": "checkout"}},
    )

    assert evidence.source == EvidenceSource.PROMETHEUS
    assert "800ms" in evidence.summary
    assert evidence.hypothesis_id == "hyp-123"
    assert evidence.collected_by == "metrics_agent"
    assert "promql" in evidence.raw_payload
    assert evidence.raw_payload["anomaly_detected"] is True


@pytest.mark.asyncio
async def test_metrics_agent_prometheus_unreachable():
    """When Prometheus is down, return error Evidence."""
    mock_llm = MockLLMProvider()
    mock_llm.register(PromQLOutput, PromQLOutput(
        promql='up',
        explanation="Check if targets are up",
    ))

    class FailingPrometheus:
        async def query_promql(self, promql: str) -> dict:
            raise ConnectionError("connection refused")

    agent = MetricsAgent(llm=mock_llm, prometheus=FailingPrometheus())

    evidence = await agent("is prometheus up?")

    assert evidence.source == EvidenceSource.PROMETHEUS
    assert evidence.summary.startswith("Error: Prometheus query failed")
    assert "connection refused" in evidence.summary


@pytest.mark.asyncio
async def test_metrics_agent_llm_failure():
    """When the LLM fails, return error Evidence — graceful degradation."""
    mock_llm = MockLLMProvider()
    # No PromQLOutput registered → KeyError

    agent = MetricsAgent(llm=mock_llm)

    evidence = await agent("what is the latency?")

    assert evidence.source == EvidenceSource.PROMETHEUS
    assert evidence.summary.startswith("Error: MetricsAgent failed")


@pytest.mark.asyncio
async def test_metrics_agent_promql_in_result():
    """The PromQL query is included in the Evidence."""
    mock_llm = MockLLMProvider()
    mock_llm.register(PromQLOutput, PromQLOutput(
        promql='histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))',
        explanation="p95 latency",
    ))
    mock_llm.register(MetricsSummaryOutput, MetricsSummaryOutput(
        summary="p95 is 500ms",
    ))

    class MockPrometheus:
        async def query_promql(self, promql: str) -> dict:
            return {"status": "success", "data": {"result": []}}

    agent = MetricsAgent(llm=mock_llm, prometheus=MockPrometheus())

    evidence = await agent("p95 latency?")

    assert "histogram_quantile" in evidence.query
    assert evidence.raw_payload["promql"] == evidence.query