"""MetricsAgent — NL → PromQL → Prometheus → LLM summary → Evidence."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from observability_agent.domain.models import Evidence, EvidenceSource
from observability_agent.infrastructure.llm_provider import LLMProvider
from observability_agent.mcp.prometheus import PrometheusClient

logger = logging.getLogger(__name__)


class PromQLOutput(BaseModel):
    """LLM translates a natural language query into PromQL."""

    promql: str = Field(description="The PromQL query to execute")
    explanation: str = Field(description="Brief explanation of what this query does")


class MetricsSummaryOutput(BaseModel):
    """LLM summarizes Prometheus query results."""

    summary: str = Field(description="Natural language summary of the results")
    anomaly_detected: bool = Field(
        default=False,
        description="Whether the results indicate an anomaly from baseline",
    )


class MetricsAgent:
    """Real MetricsAgent backed by a Prometheus HTTP client.

    Satisfies the MetricsAgent protocol: callable(NL, hypothesis_id, extra_context) → Evidence.
    """

    def __init__(
        self,
        llm: LLMProvider,
        prometheus: PrometheusClient | None = None,
    ) -> None:
        self.llm = llm
        self.prometheus = prometheus or PrometheusClient()

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence:
        """Execute the full NL → PromQL → query → summarize pipeline."""
        try:
            return await self._execute(natural_language, hypothesis_id, extra_context)
        except Exception as exc:
            logger.warning("MetricsAgent failed: %s — %s", natural_language[:80], exc)
            return Evidence(
                source=EvidenceSource.PROMETHEUS,
                query=natural_language,
                summary=f"Error: MetricsAgent failed — {exc}",
                raw_payload={"error": str(exc)},
                hypothesis_id=hypothesis_id,
                collected_by="metrics_agent",
            )

    async def _execute(
        self,
        natural_language: str,
        hypothesis_id: str | None,
        extra_context: dict[str, Any] | None,
    ) -> Evidence:
        # Step 1: NL → PromQL
        promql_output = await self.llm.generate_structured(
            prompt=self._build_promql_prompt(natural_language, extra_context),
            output_schema=PromQLOutput,
            system_prompt=(
                "You are a Prometheus expert. Translate natural language queries "
                "into valid PromQL. Use rate() for counters, histogram_quantile() "
                "for histograms. Always include a service or namespace label filter "
                "when provided in the context."
            ),
        )
        assert isinstance(promql_output, PromQLOutput)

        # Step 2: Execute PromQL
        try:
            raw = await self.prometheus.query_promql(promql_output.promql)
        except Exception as exc:
            return Evidence(
                source=EvidenceSource.PROMETHEUS,
                query=promql_output.promql,
                summary=f"Error: Prometheus query failed — {exc}",
                raw_payload={"error": str(exc), "promql": promql_output.promql},
                hypothesis_id=hypothesis_id,
                collected_by="metrics_agent",
            )

        # Step 3: Summarize results
        summary_output = await self.llm.generate_structured(
            prompt=self._build_summary_prompt(natural_language, raw),
            output_schema=MetricsSummaryOutput,
            system_prompt=(
                "You are an SRE analyzing Prometheus metrics. Summarize the query "
                "results in 1-3 sentences. Note any anomalies, spikes, or deviations "
                "from expected baselines. Be specific about values and time ranges."
            ),
        )
        assert isinstance(summary_output, MetricsSummaryOutput)

        return Evidence(
            source=EvidenceSource.PROMETHEUS,
            query=promql_output.promql,
            summary=summary_output.summary,
            raw_payload={
                "promql": promql_output.promql,
                "explanation": promql_output.explanation,
                "result": raw,
                "anomaly_detected": summary_output.anomaly_detected,
            },
            hypothesis_id=hypothesis_id,
            collected_by="metrics_agent",
        )

    def _build_promql_prompt(
        self, nl: str, extra_context: dict[str, Any] | None
    ) -> str:
        ctx = extra_context or {}
        parts = [f"Natural language query: {nl}"]
        if "alert_labels" in ctx:
            labels = ctx["alert_labels"]
            parts.append(f"Alert labels: {labels}")
            service = labels.get("service", "")
            if service:
                parts.append(f"Filter by service=\"{service}\" where applicable.")
        if "incident_title" in ctx:
            parts.append(f"Incident: {ctx['incident_title']}")
        return "\n".join(parts)

    def _build_summary_prompt(
        self, nl: str, raw: dict[str, Any]
    ) -> str:
        # ponytail: truncate large results to keep prompt size reasonable
        result_str = str(raw)
        if len(result_str) > 4000:
            result_str = result_str[:4000] + "...(truncated)"
        return (
            f"Original query: {nl}\n\n"
            f"Prometheus API response:\n{result_str}\n\n"
            f"Summarize these results in 1-3 sentences."
        )