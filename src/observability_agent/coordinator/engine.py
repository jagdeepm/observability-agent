"""Coordinator — the investigation engine that drives the Incident through its lifecycle.

Rule-based state machine with LLM-filled content. The Coordinator is the sole
driver of the Incident state machine. Specialized agents are called as needed
but never mutate the Incident directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from observability_agent.agents.protocols import (
    DeploymentAgent,
    KnowledgeAgent,
    KubernetesAgent,
    LogsAgent,
    MetricsAgent,
    TraceAgent,
)
from observability_agent.coordinator.outputs import (
    DeepenHypothesesOutput,
    EvaluateHypothesesOutput,
    GenerateHypothesesOutput,
    HypothesisProposal,
    RCAOutput,
)
from observability_agent.coordinator import prompts
from observability_agent.domain.models import (
    Evidence,
    EvidenceSource,
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentStatus,
)
from observability_agent.infrastructure.llm_provider import LLMProvider
from observability_agent.infrastructure.repository import IncidentRepository

logger = logging.getLogger(__name__)

# ponytail: global default, configurable per-deployment if needed
MAX_DEPTH = 3

# Breadth caps — the same shape of decision as MAX_DEPTH, made for breadth
# instead of depth. A real LLM does not share a mock's restraint: asked to
# "propose hypotheses" or "drill deeper" with no count given, a live run
# reached 177 hypotheses with no ceiling anywhere. Growth compounds across
# MAX_DEPTH levels, so worst-case total is roughly
# MAX_HYPOTHESES_PER_GENERATE + MAX_DEPTH * (MAX_DEEPEN_PER_LEVEL *
# MAX_CHILDREN_PER_DEEPEN) — with these values, 8 + 3*(5*3) = 53. Recalibrate
# here, not in the algorithm. See docs/specs/investigation-cost-guardrails.md.
MAX_HYPOTHESES_PER_GENERATE = 8
MAX_CHILDREN_PER_DEEPEN = 3
MAX_DEEPEN_PER_LEVEL = 5


class Coordinator:
    """Drives an Incident through its investigation lifecycle.

    Dependencies are injected — all LLM calls and agent calls go through
    interfaces that can be mocked for testing.
    """

    def __init__(
        self,
        llm: LLMProvider,
        repo: IncidentRepository,
        metrics_agent: MetricsAgent,
        logs_agent: LogsAgent,
        trace_agent: TraceAgent,
        kubernetes_agent: KubernetesAgent,
        deployment_agent: DeploymentAgent,
        knowledge_agent: KnowledgeAgent,
        max_depth: int = MAX_DEPTH,
    ) -> None:
        self.llm = llm
        self.repo = repo
        self.metrics = metrics_agent
        self.logs = logs_agent
        self.traces = trace_agent
        self.kubernetes = kubernetes_agent
        self.deployment = deployment_agent
        self.knowledge = knowledge_agent
        self.max_depth = max_depth

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, incident_id: str) -> Incident:
        """Run the full investigation loop for an incident.

        Returns the incident in its terminal state (resolved, false_positive, or closed).
        Saves after each step so a crash mid-investigation doesn't lose everything.
        """
        incident = await self._load(incident_id)

        if incident.status == IncidentStatus.OPEN:
            incident.acknowledge()
            await self._save(incident)

        # ── Phase: INVESTIGATING ──
        incident = await self._phase_explore(incident)
        incident = await self._phase_generate_hypotheses(incident)
        incident = await self._phase_investigate_hypotheses(incident)

        # ── Phase: RCA ──
        incident = await self._phase_produce_rca(incident)

        return incident

    # ------------------------------------------------------------------
    # Phase: Explore
    # ------------------------------------------------------------------

    async def _phase_explore(self, incident: Incident) -> Incident:
        """Gather exploratory evidence from all sources. No hypothesis yet."""
        logger.info("Gathering exploratory evidence for %s", incident.id)

        alert_labels = self._alert_labels(incident)
        service = alert_labels.get("service", "unknown")

        tasks = [
            self._safe_call(self.metrics,
                f"what is anomalous for service {service} in the last 10 minutes?",
                source=EvidenceSource.PROMETHEUS,
                extra_context={"alert_labels": alert_labels}),
            self._safe_call(self.logs,
                f"show error and warning patterns for service {service} in the last 10 minutes",
                source=EvidenceSource.LOKI,
                extra_context={"alert_labels": alert_labels}),
            self._safe_call(self.traces,
                f"show slow traces and error spans for service {service}",
                source=EvidenceSource.TEMPO,
                extra_context={"alert_labels": alert_labels}),
            self._safe_call(self.kubernetes,
                f"what is the state of pods, events, and resources for service {service}?",
                source=EvidenceSource.KUBERNETES,
                extra_context={"alert_labels": alert_labels}),
            self._safe_call(self.deployment,
                f"any recent deployments or config changes for service {service} in the last hour?",
                source=EvidenceSource.DEPLOYMENT,
                extra_context={"alert_labels": alert_labels}),
            self._safe_call(self.knowledge,
                f"runbooks and operational procedures for {service} latency incidents",
                source=EvidenceSource.DOCUMENTATION,
                extra_context={"alert_labels": alert_labels}),
        ]

        results = await asyncio.gather(*tasks)
        for evidence in results:
            incident.add_evidence(evidence)

        await self._save(incident)
        return incident

    async def _safe_call(
        self,
        agent: Any,
        query: str,
        source: EvidenceSource,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence:
        """Call an agent and return Evidence. On failure, return error Evidence."""
        try:
            return await agent(query, hypothesis_id=hypothesis_id, extra_context=extra_context)
        except Exception as exc:
            logger.warning("Agent call failed: %s — %s", query[:80], exc)
            return Evidence(
                source=source,
                query=query,
                summary=f"Error: agent call failed — {exc}",
                raw_payload={"error": str(exc)},
                collected_by="coordinator",
            )

    # ------------------------------------------------------------------
    # Phase: Generate Hypotheses
    # ------------------------------------------------------------------

    async def _phase_generate_hypotheses(self, incident: Incident) -> Incident:
        """LLM reviews exploratory evidence and proposes root hypotheses."""
        logger.info("Generating hypotheses for %s", incident.id)

        exploratory = self._exploratory_evidence(incident)
        system_prompt, user_prompt = prompts.generate_hypotheses_prompt(
            alert_labels=self._alert_labels(incident),
            exploratory_evidence=exploratory,
            max_count=MAX_HYPOTHESES_PER_GENERATE,
        )
        output = await self.llm.generate_structured(
            prompt=user_prompt,
            output_schema=GenerateHypothesesOutput,
            system_prompt=system_prompt,
        )
        assert isinstance(output, GenerateHypothesesOutput)

        proposals = self._truncate_proposals(
            output.hypotheses, MAX_HYPOTHESES_PER_GENERATE, f"hypothesis generation for {incident.id}",
        )

        for proposal in proposals:
            incident.add_hypothesis(
                statement=proposal.statement,
                parent_id=proposal.parent_id,
                created_by="coordinator",
            )

        # ponytail: resolve parent_id references. The LLM may return
        # placeholder IDs (e.g., "will-be-set") that reference earlier
        # hypotheses in the same batch. Resolve by position.
        self._resolve_parent_refs(incident)
        await self._save(incident)
        return incident

    def _resolve_parent_refs(self, incident: Incident) -> None:
        """Resolve parent_id placeholders to actual hypothesis IDs."""
        for h in incident.hypotheses:
            if h.parent_id is None:
                continue
            # Check if parent_id references an actual hypothesis
            if any(p.id == h.parent_id for p in incident.hypotheses):
                continue
            # ponytail: try to resolve by position. If parent_id is a
            # placeholder like "will-be-set", resolve to the root hypothesis
            # (the first hypothesis with no parent).
            roots = incident.get_root_hypotheses()
            if roots and h.parent_id not in {p.id for p in incident.hypotheses}:
                # Assign to the last root hypothesis (the one most recently added
                # before this hypothesis)
                for root in reversed(roots):
                    if incident.hypotheses.index(root) < incident.hypotheses.index(h):
                        h.parent_id = root.id
                        break

    @staticmethod
    def _truncate_proposals(
        proposals: list[HypothesisProposal], cap: int, what: str
    ) -> list[HypothesisProposal]:
        """Slice a generate/deepen call's output to the cap, keeping the
        model's own first N — logging only when it actually truncates
        something, so the cap engaging is visible without being noisy."""
        if len(proposals) > cap:
            logger.info("Truncated %s: %d proposed, kept first %d", what, len(proposals), cap)
        return proposals[:cap]

    # ------------------------------------------------------------------
    # Phase: Investigate Hypotheses (loop)
    # ------------------------------------------------------------------

    async def _phase_investigate_hypotheses(self, incident: Incident) -> Incident:
        """Gather targeted evidence → evaluate → deepen. Loop until convergence."""
        depth = 0

        while depth < self.max_depth:
            unresolved = self._unresolved_hypotheses(incident)
            if not unresolved:
                logger.info("All hypotheses resolved at depth %d", depth)
                break

            # Gather targeted evidence for each unresolved hypothesis
            await self._gather_targeted_evidence(incident, unresolved)

            # Evaluate each hypothesis
            await self._evaluate_all(incident, unresolved)

            # Prune refuted branches
            incident.prune_refuted_branches()

            # Deepen: generate child hypotheses for confirmed leaf hypotheses
            confirmed = [h for h in incident.hypotheses if h.status == HypothesisStatus.CONFIRMED]
            if not confirmed:
                logger.info("No confirmed hypotheses at depth %d — stopping", depth)
                break
            # Only deepen leaves — skip hypotheses that already have children
            leaves = [h for h in confirmed if not incident.get_children(h)]
            if not leaves:
                logger.info("No deepenable leaves at depth %d — converged", depth)
                break

            # Deepen only the most-confident leaves. This is the more
            # consequential of the two breadth caps: it bounds the number of
            # deepen *calls* (one per leaf, not batched), not just the size of
            # one call's output. Prioritizing by confidence also means the
            # branches given the remaining budget are the ones most likely
            # to be true, not just the ones the model happened to list first.
            to_deepen = sorted(leaves, key=lambda h: h.confidence, reverse=True)
            to_deepen = to_deepen[:MAX_DEEPEN_PER_LEVEL]
            if len(leaves) > MAX_DEEPEN_PER_LEVEL:
                logger.info(
                    "Depth %d for %s: %d confirmed leaves, deepening the %d most confident",
                    depth, incident.id, len(leaves), MAX_DEEPEN_PER_LEVEL,
                )

            new_children = await self._deepen(incident, to_deepen)
            if not new_children:
                logger.info("No new child hypotheses at depth %d — converged", depth)
                break

            depth += 1
            await self._save(incident)

        return incident

    async def _gather_targeted_evidence(
        self, incident: Incident, hypotheses: list[Hypothesis]
    ) -> None:
        """Ask the most relevant agent to investigate each hypothesis, concurrently.

        One hypothesis's evidence gathering does not depend on another's, so this
        mirrors the exploratory-evidence phase (six agents in parallel) rather than
        awaiting them one at a time. A wide hypothesis set previously meant a wide
        multiple of sequential network+LLM round trips — live-observed reaching
        100+ hypotheses and tens of minutes of wall clock. `_safe_call` never
        raises, so `asyncio.gather` here can't be short-circuited by one agent's
        failure, exactly as exploratory evidence already relies on.
        """
        tasks = []
        for hyp in hypotheses:
            source = self._pick_source(hyp.statement)
            agent = self._agent_for_source(source)
            if agent is None:
                continue
            tasks.append(self._safe_call(
                agent,
                query=f"investigate: {hyp.statement}",
                source=source,
                hypothesis_id=hyp.id,
                extra_context={"incident_title": incident.title},
            ))

        if not tasks:
            return

        for evidence in await asyncio.gather(*tasks):
            incident.add_evidence(evidence)

    async def _evaluate_all(
        self, incident: Incident, hypotheses: list[Hypothesis]
    ) -> None:
        """LLM evaluates all unresolved hypotheses in one batch."""
        if not hypotheses:
            return

        # Build a batch prompt with all hypotheses
        entries = []
        for h in hypotheses:
            summaries = "\n".join(f"  {prompts.format_evidence(e)}" for e in h.evidence)
            entries.append(
                f"Hypothesis [{h.id}]: {h.statement}\nEvidence:\n{summaries}"
            )
        batch_text = "\n\n".join(entries)

        system_prompt = (
            "You are an AI Site Reliability Engineer. Evaluate each hypothesis "
            "below as CONFIRMED or REFUTED based on the evidence. Return one "
            "evaluation per hypothesis, matching by hypothesis_id."
        )
        user_prompt = f"Evaluate these hypotheses:\n\n{batch_text}"

        output = await self.llm.generate_structured(
            prompt=user_prompt,
            output_schema=EvaluateHypothesesOutput,
            system_prompt=system_prompt,
        )
        assert isinstance(output, EvaluateHypothesesOutput)

        # Match evaluations to hypotheses by ID, fall back to position
        for hyp in hypotheses:
            eval_ = self._find_evaluation(output, hyp, hypotheses)
            if eval_ is None:
                continue
            if eval_.status == "confirmed":
                hyp.confirm(eval_.confidence)
            else:
                hyp.refute()

    def _find_evaluation(
        self,
        output: EvaluateHypothesesOutput,
        hyp: Hypothesis,
        all_hyps: list[Hypothesis],
    ) -> Any:
        """Find the evaluation matching this hypothesis. Falls back to position."""
        real_ids = {h.id for h in all_hyps}
        for i, eval_ in enumerate(output.evaluations):
            if eval_.hypothesis_id == hyp.id:
                return eval_
            # ponytail: if the LLM returns a non-matching ID (mock scenario),
            # fall back to position-based matching
            if eval_.hypothesis_id not in real_ids:
                idx = all_hyps.index(hyp)
                if i == idx:
                    return eval_
        return None

    async def _deepen(
        self, incident: Incident, to_deepen: list[Hypothesis]
    ) -> list[Hypothesis]:
        """Generate child hypotheses for the given (already breadth-capped) parents."""
        new_children: list[Hypothesis] = []

        for hyp in to_deepen:
            chain = hyp.ancestor_chain(incident.hypotheses)
            summaries = [prompts.format_evidence(e) for e in hyp.evidence]
            system_prompt, user_prompt = prompts.deepen_hypotheses_prompt(
                hyp, chain, summaries, max_count=MAX_CHILDREN_PER_DEEPEN,
            )
            output = await self.llm.generate_structured(
                prompt=user_prompt,
                output_schema=DeepenHypothesesOutput,
                system_prompt=system_prompt,
            )
            assert isinstance(output, DeepenHypothesesOutput)

            children = self._truncate_proposals(
                output.child_hypotheses, MAX_CHILDREN_PER_DEEPEN, f"deepen for hypothesis {hyp.id}",
            )

            for proposal in children:
                child = incident.add_hypothesis(
                    statement=proposal.statement,
                    parent_id=hyp.id,
                    created_by="coordinator",
                )
                new_children.append(child)

        return new_children

    # ------------------------------------------------------------------
    # Phase: Produce RCA
    # ------------------------------------------------------------------

    async def _phase_produce_rca(self, incident: Incident) -> Incident:
        """LLM produces the final RCA from the confirmed hypothesis path."""
        logger.info("Producing RCA for %s", incident.id)

        confirmed = [h for h in incident.hypotheses if h.status == HypothesisStatus.CONFIRMED]
        if not confirmed:
            # No hypotheses confirmed — produce a "no findings" RCA
            incident.produce_rca()
            # Still call the LLM for an RCA with no confirmed path
            system_prompt, user_prompt = prompts.produce_rca_prompt([], [])
            output = await self.llm.generate_structured(
                prompt=user_prompt,
                output_schema=RCAOutput,
                system_prompt=system_prompt,
            )
            assert isinstance(output, RCAOutput)
            incident.propose_remediation(output.remediation_action)
            await self._save(incident)
            return incident

        # Find the best confirmed path
        best_leaf = max(confirmed, key=lambda h: h.confidence)
        path = best_leaf.confirmed_path(incident.hypotheses) or [best_leaf]
        all_evidence = [e for h in path for e in h.evidence]

        system_prompt, user_prompt = prompts.produce_rca_prompt(path, all_evidence)
        output = await self.llm.generate_structured(
            prompt=user_prompt,
            output_schema=RCAOutput,
            system_prompt=system_prompt,
        )
        assert isinstance(output, RCAOutput)

        incident.produce_rca()
        incident.propose_remediation(output.remediation_action)
        await self._save(incident)
        return incident

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load(self, incident_id: str) -> Incident:
        incident = await self.repo.get(incident_id)
        if incident is None:
            raise ValueError(f"Incident {incident_id} not found")
        return incident

    async def _save(self, incident: Incident) -> None:
        await self.repo.save(incident)

    def _alert_labels(self, incident: Incident) -> dict[str, str]:
        if incident.linked_alerts:
            return incident.linked_alerts[0].labels
        return {}

    def _exploratory_evidence(self, incident: Incident) -> list[Evidence]:
        return list(incident.exploratory_evidence)

    def _unresolved_hypotheses(self, incident: Incident) -> list[Hypothesis]:
        return [
            h
            for h in incident.hypotheses
            if h.status in (HypothesisStatus.PROPOSED, HypothesisStatus.INVESTIGATING)
        ]

    def _pick_source(self, statement: str) -> EvidenceSource:
        """Heuristic: pick the most relevant observability source for a hypothesis."""
        lower = statement.lower()
        if any(kw in lower for kw in ("deploy", "release", "rollout", "commit", "config change")):
            return EvidenceSource.DEPLOYMENT
        if any(kw in lower for kw in ("log", "error", "exception", "crash")):
            return EvidenceSource.LOKI
        if any(kw in lower for kw in ("trace", "span", "latency", "bottleneck", "slow")):
            return EvidenceSource.TEMPO
        if any(kw in lower for kw in ("pod", "container", "node", "restart", "crashloop", "oom")):
            return EvidenceSource.KUBERNETES
        if any(kw in lower for kw in ("runbook", "procedure", "documentation", "known issue")):
            return EvidenceSource.DOCUMENTATION
        return EvidenceSource.PROMETHEUS  # default: metrics for everything else

    def _agent_for_source(self, source: EvidenceSource) -> Any:
        return {
            EvidenceSource.PROMETHEUS: self.metrics,
            EvidenceSource.LOKI: self.logs,
            EvidenceSource.TEMPO: self.traces,
            EvidenceSource.KUBERNETES: self.kubernetes,
            EvidenceSource.DEPLOYMENT: self.deployment,
            EvidenceSource.DOCUMENTATION: self.knowledge,
        }[source]