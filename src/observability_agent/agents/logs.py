"""LogsAgent — NL → LogQL → Loki → LLM summary → Evidence."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from observability_agent.analysis import analyze_logs
from observability_agent.domain.models import Evidence, EvidenceSource
from observability_agent.infrastructure.llm_provider import LLMProvider
from observability_agent.mcp.loki import LokiClient

logger = logging.getLogger(__name__)


class LogQLOutput(BaseModel):
    logql: str = Field(description="The LogQL query to execute")
    explanation: str = Field(description="Brief explanation of what this query does")


class LogsSummaryOutput(BaseModel):
    """Error counting is not asked of the LLM — it is computed in analysis.py."""

    summary: str = Field(description="Natural language summary of the log results")


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

    async def _label_names(self) -> list[str]:
        """The label names this Loki actually has, or [] if they can't be read.

        Label discovery must never fail the investigation — an unreachable Loki
        should degrade to a generic prompt, not to no evidence at all.
        """
        try:
            return sorted(await self.loki.list_labels())
        except Exception as exc:
            logger.debug("Loki label discovery failed: %s", exc)
            return []

    @staticmethod
    def _translate_system_prompt(labels: list[str]) -> str:
        """Build the NL→LogQL system prompt, naming the labels that exist.

        Without this the prompt suggested `{service="name"}` as the example
        selector. No Loki fed by Promtail has a `service` label — it labels by
        Kubernetes metadata — so the model dutifully wrote a selector matching
        nothing, and then reached for `| json | pattern ...` to compensate, which
        Loki rejects with a 500. Naming the real labels removes both failures.
        """
        available = (
            f"The only label names in this Loki are: {', '.join(labels)}. "
            "Use nothing else — a selector on a label that does not exist matches "
            "no streams."
            if labels
            else "Prefer Kubernetes labels such as namespace, pod, container and app."
        )
        return (
            "You are a Loki/LogQL expert. Translate natural language queries into "
            f"valid LogQL. {available} "
            "Use |= for substring search and |~ for regex. Keep the pipeline simple: "
            "a label selector plus at most one line filter. Do not use json, pattern, "
            "unwrap or label_format. Do not put a time range in the query itself."
        )

    async def _execute(
        self, nl: str, hypothesis_id: str | None, extra_context: dict[str, Any] | None,
    ) -> Evidence:
        ctx = extra_context or {}
        service = ctx.get("alert_labels", {}).get("service", "")

        logql_output = await self.llm.generate_structured(
            prompt=f"Query: {nl}\nService: {service}\nTranslate to LogQL.",
            output_schema=LogQLOutput,
            system_prompt=self._translate_system_prompt(await self._label_names()),
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

        analysis = analyze_logs(raw)

        summary_output = await self.llm.generate_structured(
            prompt=f"Original query: {nl}\n{analysis.text}",
            output_schema=LogsSummaryOutput,
            system_prompt=(
                "Summarize these log results in 1-3 sentences. Counts are already "
                "computed — cite them, do not recount."
            ),
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
            },
            signals=analysis.signals,
            hypothesis_id=hypothesis_id,
            collected_by="logs_agent",
        )
