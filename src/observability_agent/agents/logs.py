"""LogsAgent — NL → LogQL → Loki → LLM summary → Evidence."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from observability_agent.domain.models import Evidence, EvidenceSource
from observability_agent.infrastructure.llm_provider import LLMProvider
from observability_agent.mcp.loki import LokiClient

logger = logging.getLogger(__name__)


class LogQLOutput(BaseModel):
    logql: str = Field(description="The LogQL query to execute")
    explanation: str = Field(description="Brief explanation of what this query does")


class LogsSummaryOutput(BaseModel):
    summary: str = Field(description="Natural language summary of the log results")
    error_count: int = Field(default=0, description="Number of error-level log lines found")


class LogsAgent:
    """Real LogsAgent backed by a Loki HTTP client."""

    def __init__(self, llm: LLMProvider, loki: LokiClient | None = None) -> None:
        self.llm = llm
        self.loki = loki or LokiClient()

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence:
        try:
            return await self._execute(natural_language, hypothesis_id, extra_context)
        except Exception as exc:
            logger.warning("LogsAgent failed: %s — %s", natural_language[:80], exc)
            return Evidence(
                source=EvidenceSource.LOKI,
                query=natural_language,
                summary=f"Error: LogsAgent failed — {exc}",
                raw_payload={"error": str(exc)},
                hypothesis_id=hypothesis_id,
                collected_by="logs_agent",
            )

    async def _execute(
        self, nl: str, hypothesis_id: str | None, extra_context: dict[str, Any] | None,
    ) -> Evidence:
        ctx = extra_context or {}
        service = ctx.get("alert_labels", {}).get("service", "")

        logql_output = await self.llm.generate_structured(
            prompt=f"Query: {nl}\nService: {service}\nTranslate to LogQL.",
            output_schema=LogQLOutput,
            system_prompt=(
                "You are a Loki/LogQL expert. Translate natural language queries into "
                "valid LogQL. Use label selectors like {service=\"name\"} when a service "
                "is specified. Use |= for text search, |~ for regex. Include a time range "
                "via the query parameters, not in the LogQL itself."
            ),
        )
        assert isinstance(logql_output, LogQLOutput)

        try:
            raw = await self.loki.query_logql(logql_output.logql)
        except Exception as exc:
            return Evidence(
                source=EvidenceSource.LOKI,
                query=logql_output.logql,
                summary=f"Error: Loki query failed — {exc}",
                raw_payload={"error": str(exc), "logql": logql_output.logql},
                hypothesis_id=hypothesis_id,
                collected_by="logs_agent",
            )

        summary_output = await self.llm.generate_structured(
            prompt=f"Original query: {nl}\nLoki results:\n{str(raw)[:4000]}",
            output_schema=LogsSummaryOutput,
            system_prompt="Summarize log query results in 1-3 sentences. Count error-level lines.",
        )
        assert isinstance(summary_output, LogsSummaryOutput)

        return Evidence(
            source=EvidenceSource.LOKI,
            query=logql_output.logql,
            summary=summary_output.summary,
            raw_payload={
                "logql": logql_output.logql,
                "explanation": logql_output.explanation,
                "result": raw,
                "error_count": summary_output.error_count,
            },
            hypothesis_id=hypothesis_id,
            collected_by="logs_agent",
        )