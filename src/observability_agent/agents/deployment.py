"""DeploymentAgent — NL → GitHub API → LLM summary → Evidence."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from observability_agent.domain.models import Evidence, EvidenceSource
from observability_agent.infrastructure.llm_provider import LLMProvider
from observability_agent.mcp.deployment import DeploymentClient

logger = logging.getLogger(__name__)


class DeploymentQueryOutput(BaseModel):
    action: str = Field(description="recent_commits, compare_commits, or list_deployments")
    path: str = Field(default="", description="File path filter for commits")
    base_rev: str = Field(default="", description="Base revision for compare")
    head_rev: str = Field(default="", description="Head revision for compare")
    explanation: str = Field(description="Brief explanation of what this query does")


class DeploymentSummaryOutput(BaseModel):
    summary: str = Field(description="Natural language summary of deployment changes")
    recent_deployment: bool = Field(default=False, description="Whether a recent deployment was detected")
    changed_files: list[str] = Field(default_factory=list, description="Key files changed")


class DeploymentAgent:
    """Real DeploymentAgent backed by GitHub API."""

    def __init__(self, llm: LLMProvider, client: DeploymentClient | None = None) -> None:
        self.llm = llm
        self.client = client or DeploymentClient()

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence:
        try:
            return await self._execute(natural_language, hypothesis_id, extra_context)
        except Exception as exc:
            logger.warning("DeploymentAgent failed: %s — %s", natural_language[:80], exc)
            return Evidence(
                source=EvidenceSource.DEPLOYMENT,
                query=natural_language,
                summary=f"Error: DeploymentAgent failed — {exc}",
                raw_payload={"error": str(exc)},
                hypothesis_id=hypothesis_id,
                collected_by="deployment_agent",
            )

    async def _execute(
        self, nl: str, hypothesis_id: str | None, extra_context: dict[str, Any] | None,
    ) -> Evidence:
        ctx = extra_context or {}
        service = ctx.get("alert_labels", {}).get("service", "")

        query_output = await self.llm.generate_structured(
            prompt=f"Query: {nl}\nService: {service}\nDetermine the GitHub API action.",
            output_schema=DeploymentQueryOutput,
            system_prompt=(
                "You are a deployment/SRE expert. Determine the best GitHub API action. "
                "For recent changes, use recent_commits with the service path filter. "
                "For comparing deployments, use compare_commits with base and head revisions."
            ),
        )
        assert isinstance(query_output, DeploymentQueryOutput)

        try:
            raw = await self._execute_query(query_output)
        except Exception as exc:
            return Evidence(
                source=EvidenceSource.DEPLOYMENT,
                query=query_output.action,
                summary=f"Error: Deployment query failed — {exc}",
                raw_payload={"error": str(exc), "action": query_output.action},
                hypothesis_id=hypothesis_id,
                collected_by="deployment_agent",
            )

        summary_output = await self.llm.generate_structured(
            prompt=f"Original query: {nl}\nDeployment data:\n{str(raw)[:4000]}",
            output_schema=DeploymentSummaryOutput,
            system_prompt="Summarize deployment changes. Note if a recent deployment was detected and list key changed files.",
        )
        assert isinstance(summary_output, DeploymentSummaryOutput)

        return Evidence(
            source=EvidenceSource.DEPLOYMENT,
            query=query_output.action,
            summary=summary_output.summary,
            raw_payload={
                "action": query_output.action,
                "explanation": query_output.explanation,
                "result": raw,
                "recent_deployment": summary_output.recent_deployment,
                "changed_files": summary_output.changed_files,
            },
            hypothesis_id=hypothesis_id,
            collected_by="deployment_agent",
        )

    async def _execute_query(self, q: DeploymentQueryOutput) -> Any:
        if q.action == "recent_commits":
            return await self.client.recent_commits(path=q.path)
        elif q.action == "compare_commits":
            return await self.client.compare_commits(q.base_rev, q.head_rev)
        elif q.action == "list_deployments":
            return await self.client.list_deployments()
        return await self.client.recent_commits()