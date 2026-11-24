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


# ---------------------------------------------------------------------------
# Computed signals — anomaly detection happens in code, not in the LLM
# ---------------------------------------------------------------------------


def matrix_payload(values: list[float]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"service": "checkout"},
                    "values": [[1700000000 + i * 15, str(v)] for i, v in enumerate(values)],
                }
            ],
        },
    }


def make_llm() -> MockLLMProvider:
    llm = MockLLMProvider()
    llm.register(PromQLOutput, PromQLOutput(promql="up", explanation="probe"))
    llm.register(MetricsSummaryOutput, MetricsSummaryOutput(summary="Latency looks elevated."))
    return llm


class StubPrometheus:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def query_promql(self, promql: str) -> dict:
        return self.payload


@pytest.mark.asyncio
async def test_steady_series_reports_no_anomaly():
    """A series with normal jitter is not flagged."""
    steady = [0.10, 0.11, 0.09, 0.10, 0.12, 0.10, 0.09, 0.11, 0.10, 0.10]
    agent = MetricsAgent(llm=make_llm(), prometheus=StubPrometheus(matrix_payload(steady)))

    evidence = await agent("latency?")

    assert evidence.signals["anomaly"] is False
    assert evidence.signals["spike_count"] == 0


@pytest.mark.asyncio
async def test_injected_spike_is_detected():
    """A single large outlier is detected from the full series."""
    spiky = [0.1] * 19 + [5.0]
    agent = MetricsAgent(llm=make_llm(), prometheus=StubPrometheus(matrix_payload(spiky)))

    evidence = await agent("latency?")

    assert evidence.signals["anomaly"] is True
    assert evidence.signals["spike_count"] >= 1
    assert evidence.signals["max"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_percentiles_computed_from_full_series():
    """p95 is computed, not guessed — and reflects data beyond the truncation budget."""
    values = [float(i) for i in range(1, 101)]
    agent = MetricsAgent(llm=make_llm(), prometheus=StubPrometheus(matrix_payload(values)))

    evidence = await agent("latency?")

    assert evidence.signals["p95"] == pytest.approx(95.0, abs=1.5)
    assert evidence.signals["mean"] == pytest.approx(50.5)


@pytest.mark.asyncio
async def test_large_series_not_truncated_in_prompt():
    """The prompt carries computed facts, so a long series survives intact."""
    llm = make_llm()
    values = [0.1] * 500 + [9.9]
    agent = MetricsAgent(llm=llm, prometheus=StubPrometheus(matrix_payload(values)))

    evidence = await agent("latency?")

    assert evidence.signals["max"] == pytest.approx(9.9)
    assert "9.9" in llm.calls[-1]["prompt"]


@pytest.mark.asyncio
async def test_percentiles_stay_within_observed_range():
    """A p95 above every recorded value is not a fact."""
    values = [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0]
    agent = MetricsAgent(llm=make_llm(), prometheus=StubPrometheus(matrix_payload(values)))

    evidence = await agent("latency?")

    assert evidence.signals["p95"] <= max(values)


@pytest.mark.asyncio
async def test_nan_gaps_do_not_poison_signals():
    """Prometheus emits NaN for gaps; it must not propagate into the statistics."""
    payload = matrix_payload([0.1, 0.2, 0.3])
    payload["data"]["result"][0]["values"].insert(1, [1700000005, "NaN"])
    agent = MetricsAgent(llm=make_llm(), prometheus=StubPrometheus(payload))

    evidence = await agent("latency?")

    assert evidence.signals["mean"] == pytest.approx(0.2)
    assert evidence.signals["max"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_spiking_series_is_not_masked_by_a_larger_flat_one():
    """Signals describe the anomalous series, not merely the largest-valued one."""
    payload = matrix_payload([0.1] * 19 + [5.0])
    payload["data"]["result"].append({
        "metric": {"service": "cart"},
        "values": [[1700000000 + i * 15, "100.0"] for i in range(20)],
    })
    agent = MetricsAgent(llm=make_llm(), prometheus=StubPrometheus(payload))

    evidence = await agent("latency across services?")

    assert evidence.signals["anomaly"] is True
    assert evidence.signals["series_count"] == 2


@pytest.mark.asyncio
async def test_empty_result_yields_no_signals():
    agent = MetricsAgent(llm=make_llm(), prometheus=StubPrometheus(matrix_payload([])))

    evidence = await agent("latency?")

    assert evidence.signals == {}


@pytest.mark.asyncio
async def test_malformed_payload_does_not_raise():
    agent = MetricsAgent(llm=make_llm(), prometheus=StubPrometheus({"unexpected": "shape"}))

    evidence = await agent("latency?")

    assert evidence.signals == {}
    assert not evidence.summary.startswith("Error:")
