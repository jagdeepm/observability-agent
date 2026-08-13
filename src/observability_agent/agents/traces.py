"""TraceAgent — NL → TraceQL → Tempo → LLM summary → Evidence."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from observability_agent.domain.models import Evidence, EvidenceSource
from observability_agent.infrastructure.llm_provider import LLMProvider
from observability_agent.mcp.tempo import TempoClient

logger = logging.getLogger(__name__)


class TraceQLOutput(BaseModel):
    traceql: str = Field(description="The TraceQL query to execute")
    explanation: str = Field(description="Brief explanation of what this query does")


class TracesSummaryOutput(BaseModel):
    summary: str = Field(description="Natural language summary of the trace results")
    slowest_service: str = Field(default="", description="Service with the highest latency span")
    bottleneck_span: str = Field(default="", description="The span causing the bottleneck")


class TraceAgent:
    """Real TraceAgent backed by a Tempo HTTP client."""

    def __init__(self, llm: LLMProvider, tempo: TempoClient | None = None) -> None:
        self.llm = llm
        self.tempo = tempo or TempoClient()

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence:
        try:
            return await self._execute(natural_language, hypothesis_id, extra_context)
        except Exception as exc:
            logger.warning("TraceAgent failed: %s — %s", natural_language[:80], exc)
            return Evidence(
                source=EvidenceSource.TEMPO,
                query=natural_language,
                summary=f"Error: TraceAgent failed — {exc}",
                raw_payload={"error": str(exc)},
                hypothesis_id=hypothesis_id,
                collected_by="trace_agent",
            )

    async def _execute(
        self, nl: str, hypothesis_id: str | None, extra_context: dict[str, Any] | None,
    ) -> Evidence:
        ctx = extra_context or {}
        service = ctx.get("alert_labels", {}).get("service", "")

        traceql_output = await self.llm.generate_structured(
            prompt=f"Query: {nl}\nService: {service}\nTranslate to TraceQL.",
            output_schema=TraceQLOutput,
            system_prompt=(
                "You are a Tempo/TraceQL expert. Translate natural language queries into "
                "valid TraceQL. Use filters like { .service.name = \"name\" } and "
                "selectors like | span.http.status_code >= 500. "
                "Use duration comparisons like | duration > 100ms for slow spans."
            ),
        )
        assert isinstance(traceql_output, TraceQLOutput)

        try:
            raw = await self.tempo.query_traceql(traceql_output.traceql)
        except Exception as exc:
            return Evidence(
                source=EvidenceSource.TEMPO,
                query=traceql_output.traceql,
                summary=f"Error: Tempo query failed — {exc}",
                raw_payload={"error": str(exc), "traceql": traceql_output.traceql},
                hypothesis_id=hypothesis_id,
                collected_by="trace_agent",
            )

        summary_output = await self.llm.generate_structured(
            prompt=f"Original query: {nl}\nTempo results:\n{str(raw)[:4000]}",
            output_schema=TracesSummaryOutput,
            system_prompt="Summarize trace results in 1-3 sentences. Identify the slowest service and bottleneck span.",
        )
        assert isinstance(summary_output, TracesSummaryOutput)

        return Evidence(
            source=EvidenceSource.TEMPO,
            query=traceql_output.traceql,
            summary=summary_output.summary,
            raw_payload={
                "traceql": traceql_output.traceql,
                "explanation": traceql_output.explanation,
                "result": raw,
                "slowest_service": summary_output.slowest_service,
                "bottleneck_span": summary_output.bottleneck_span,
            },
            hypothesis_id=hypothesis_id,
            collected_by="trace_agent",
        )