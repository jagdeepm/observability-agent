"""KubernetesAgent — NL → kubectl/K8s API → LLM summary → Evidence."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from observability_agent.domain.models import Evidence, EvidenceSource
from observability_agent.infrastructure.llm_provider import LLMProvider
from observability_agent.mcp.kubernetes import KubernetesClient

logger = logging.getLogger(__name__)


class K8sQueryOutput(BaseModel):
    """LLM translates a natural language query into a K8s resource query."""

    resource: str = Field(description="Resource type: pods, events, deployment, logs")
    namespace: str = Field(default="default", description="K8s namespace")
    name: str = Field(default="", description="Resource name, if specific")
    label_selector: str = Field(default="", description="Label selector for filtering")
    explanation: str = Field(description="Brief explanation of what this query does")


class K8sSummaryOutput(BaseModel):
    summary: str = Field(description="Natural language summary of the K8s state")
    restart_count: int = Field(default=0, description="Total pod restarts found")
    issues_found: list[str] = Field(default_factory=list, description="Issues detected")


class KubernetesAgent:
    """Real KubernetesAgent backed by the K8s API."""

    def __init__(self, llm: LLMProvider, k8s: KubernetesClient | None = None) -> None:
        self.llm = llm
        self.k8s = k8s or KubernetesClient()

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence:
        try:
            return await self._execute(natural_language, hypothesis_id, extra_context)
        except Exception as exc:
            logger.warning("KubernetesAgent failed: %s — %s", natural_language[:80], exc)
            return Evidence(
                source=EvidenceSource.KUBERNETES,
                query=natural_language,
                summary=f"Error: KubernetesAgent failed — {exc}",
                raw_payload={"error": str(exc)},
                hypothesis_id=hypothesis_id,
                collected_by="kubernetes_agent",
            )

    async def _execute(
        self, nl: str, hypothesis_id: str | None, extra_context: dict[str, Any] | None,
    ) -> Evidence:
        ctx = extra_context or {}
        namespace = ctx.get("alert_labels", {}).get("namespace", "default")
        service = ctx.get("alert_labels", {}).get("service", "")

        query_output = await self.llm.generate_structured(
            prompt=(
                f"Query: {nl}\nNamespace: {namespace}\nService: {service}\n"
                f"Determine the K8s resource to query."
            ),
            output_schema=K8sQueryOutput,
            system_prompt=(
                "You are a Kubernetes expert. Translate natural language queries into "
                "K8s resource queries. Choose from: pods, events, deployment, logs. "
                f"Default namespace is '{namespace}'. Use label selectors like "
                "'app=servicename' for the given service."
            ),
        )
        assert isinstance(query_output, K8sQueryOutput)

        try:
            raw = await self._execute_k8s_query(query_output)
        except Exception as exc:
            return Evidence(
                source=EvidenceSource.KUBERNETES,
                query=f"{query_output.resource}/{query_output.name or query_output.namespace}",
                summary=f"Error: K8s query failed — {exc}",
                raw_payload={"error": str(exc), "resource": query_output.resource},
                hypothesis_id=hypothesis_id,
                collected_by="kubernetes_agent",
            )

        summary_output = await self.llm.generate_structured(
            prompt=f"Original query: {nl}\nK8s results:\n{str(raw)[:4000]}",
            output_schema=K8sSummaryOutput,
            system_prompt="Summarize K8s state in 1-3 sentences. Note restarts, issues, and anomalies.",
        )
        assert isinstance(summary_output, K8sSummaryOutput)

        return Evidence(
            source=EvidenceSource.KUBERNETES,
            query=f"{query_output.resource}/{query_output.name or query_output.namespace}",
            summary=summary_output.summary,
            raw_payload={
                "resource": query_output.resource,
                "namespace": query_output.namespace,
                "explanation": query_output.explanation,
                "result": raw,
                "restart_count": summary_output.restart_count,
                "issues_found": summary_output.issues_found,
            },
            hypothesis_id=hypothesis_id,
            collected_by="kubernetes_agent",
        )

    async def _execute_k8s_query(self, q: K8sQueryOutput) -> Any:
        ns = q.namespace or "default"
        if q.resource == "pods":
            return await self.k8s.list_pods(ns, labels=q.label_selector or None)
        elif q.resource == "events":
            return await self.k8s.list_events(ns)
        elif q.resource == "deployment":
            return await self.k8s.get_deployment(q.name or "", ns)
        elif q.resource == "logs":
            return await self.k8s.get_pod_logs(q.name or "", ns)
        else:
            return await self.k8s.list_pods(ns, labels=q.label_selector or None)