"""KnowledgeAgent — NL → RAG search → LLM summary → Evidence."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from observability_agent.domain.models import Evidence, EvidenceSource
from observability_agent.infrastructure.llm_provider import LLMProvider
from observability_agent.mcp.documentation import DocSearchClient

logger = logging.getLogger(__name__)


class KnowledgeQueryOutput(BaseModel):
    search_query: str = Field(description="Optimized search query for the RAG system")
    explanation: str = Field(description="Brief explanation of the search strategy")


class KnowledgeSummaryOutput(BaseModel):
    summary: str = Field(description="Natural language summary of relevant documentation")
    relevant_runbook: str = Field(default="", description="Most relevant runbook or procedure found")
    suggested_steps: list[str] = Field(default_factory=list, description="Suggested remediation steps from docs")


class KnowledgeAgent:
    """Real KnowledgeAgent backed by a RAG/documentation search endpoint."""

    def __init__(self, llm: LLMProvider, docs: DocSearchClient | None = None) -> None:
        self.llm = llm
        self.docs = docs or DocSearchClient()

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence:
        try:
            return await self._execute(natural_language, hypothesis_id, extra_context)
        except Exception as exc:
            logger.warning("KnowledgeAgent failed: %s — %s", natural_language[:80], exc)
            return Evidence(
                source=EvidenceSource.DOCUMENTATION,
                query=natural_language,
                summary=f"Error: KnowledgeAgent failed — {exc}",
                raw_payload={"error": str(exc)},
                hypothesis_id=hypothesis_id,
                collected_by="knowledge_agent",
            )

    async def _execute(
        self, nl: str, hypothesis_id: str | None, extra_context: dict[str, Any] | None,
    ) -> Evidence:
        ctx = extra_context or {}
        incident_title = ctx.get("incident_title", "")

        query_output = await self.llm.generate_structured(
            prompt=(
                f"Query: {nl}\nIncident: {incident_title}\n"
                f"Optimize this for a documentation/runbook search."
            ),
            output_schema=KnowledgeQueryOutput,
            system_prompt=(
                "You are an SRE searching internal documentation and runbooks. "
                "Optimize natural language queries for semantic search. Focus on "
                "operational procedures, troubleshooting guides, and known issues."
            ),
        )
        assert isinstance(query_output, KnowledgeQueryOutput)

        try:
            raw = await self.docs.search(query_output.search_query)
        except Exception as exc:
            return Evidence(
                source=EvidenceSource.DOCUMENTATION,
                query=query_output.search_query,
                summary=f"Error: Documentation search failed — {exc}",
                raw_payload={"error": str(exc), "search_query": query_output.search_query},
                hypothesis_id=hypothesis_id,
                collected_by="knowledge_agent",
            )

        summary_output = await self.llm.generate_structured(
            prompt=f"Original query: {nl}\nSearch results:\n{str(raw)[:4000]}",
            output_schema=KnowledgeSummaryOutput,
            system_prompt=(
                "Summarize the relevant documentation in 1-3 sentences. "
                "Identify the most relevant runbook and suggest remediation steps."
            ),
        )
        assert isinstance(summary_output, KnowledgeSummaryOutput)

        return Evidence(
            source=EvidenceSource.DOCUMENTATION,
            query=query_output.search_query,
            summary=summary_output.summary,
            raw_payload={
                "search_query": query_output.search_query,
                "explanation": query_output.explanation,
                "results": raw,
                "relevant_runbook": summary_output.relevant_runbook,
                "suggested_steps": summary_output.suggested_steps,
            },
            hypothesis_id=hypothesis_id,
            collected_by="knowledge_agent",
        )