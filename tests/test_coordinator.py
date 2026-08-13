"""Tests for the Coordinator investigation engine — mock-everything approach.

All LLM calls and agent calls are mocked. These tests verify the Coordinator's
mechanics: does it call the right agents in the right order, apply LLM outputs
correctly to the Incident, and handle the full lifecycle?
"""

from __future__ import annotations

import pytest

from observability_agent.agents.protocols import MockAgent
from observability_agent.coordinator.engine import Coordinator
from observability_agent.coordinator.outputs import (
    DeepenHypothesesOutput,
    EvaluateHypothesesOutput,
    GenerateHypothesesOutput,
    HypothesisEvaluation,
    HypothesisProposal,
    RCAOutput,
)
from observability_agent.domain.models import (
    Alert,
    AlertSource,
    Evidence,
    EvidenceSource,
    HypothesisStatus,
    Incident,
    IncidentStatus,
)
from observability_agent.infrastructure.llm_provider import MockLLMProvider
from observability_agent.infrastructure.repository import InMemoryIncidentRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_incident() -> Incident:
    """Create a fresh incident in open state with a linked alert."""
    source = AlertSource(name="prod-alertmanager", cluster_id="us-east-1")
    alert = Alert(
        source=source,
        fingerprint="checkout-latency-high",
        labels={"service": "checkout", "namespace": "prod", "severity": "critical"},
        severity="critical",
        cluster_id="us-east-1",
    )
    inc = Incident(
        cluster_id="us-east-1",
        title="Checkout latency > 500ms",
    )
    inc.add_alert(alert)
    return inc


def make_coordinator(
    llm: MockLLMProvider | None = None,
    repo: InMemoryIncidentRepository | None = None,
    **agent_overrides,
) -> tuple[Coordinator, MockLLMProvider, InMemoryIncidentRepository, dict]:
    """Create a Coordinator with all mock dependencies."""
    mock_llm = llm or MockLLMProvider()
    mock_repo = repo or InMemoryIncidentRepository()

    agents = {
        "metrics": MockAgent(),
        "logs": MockAgent(),
        "traces": MockAgent(),
        "kubernetes": MockAgent(),
        "deployment": MockAgent(),
        "knowledge": MockAgent(),
    }
    agents.update(agent_overrides)

    coord = Coordinator(
        llm=mock_llm,
        repo=mock_repo,
        metrics_agent=agents["metrics"],
        logs_agent=agents["logs"],
        trace_agent=agents["traces"],
        kubernetes_agent=agents["kubernetes"],
        deployment_agent=agents["deployment"],
        knowledge_agent=agents["knowledge"],
        max_depth=2,
    )
    return coord, mock_llm, mock_repo, agents


def register_exploratory(agents: dict) -> None:
    """Register one exploratory evidence response per agent."""
    sources = [
        ("metrics", EvidenceSource.PROMETHEUS),
        ("logs", EvidenceSource.LOKI),
        ("traces", EvidenceSource.TEMPO),
        ("kubernetes", EvidenceSource.KUBERNETES),
        ("deployment", EvidenceSource.DEPLOYMENT),
        ("knowledge", EvidenceSource.DOCUMENTATION),
    ]
    for key, source in sources:
        agents[key].register(Evidence(
            source=source, query="exploratory", summary=f"{key} ok", collected_by=key,
        ))


# ---------------------------------------------------------------------------
# Full happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_happy_path():
    """Full lifecycle: open → investigating → pending_approval."""
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)
    register_exploratory(agents)

    # Hypothesis statements without keywords → default to metrics agent
    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[
            HypothesisProposal(statement="database connections are exhausted"),
            HypothesisProposal(statement="cpu pressure is high"),
        ]
    ))

    # Targeted evidence for 2 hypotheses (both route to metrics by default)
    agents["metrics"].register(Evidence(
        source=EvidenceSource.PROMETHEUS, query="q", summary="DB connections near max",
        collected_by="metrics_agent",
    ))
    agents["metrics"].register(Evidence(
        source=EvidenceSource.PROMETHEUS, query="q", summary="CPU usage normal",
        collected_by="metrics_agent",
    ))

    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=0.9, reasoning="ok"),
            HypothesisEvaluation(hypothesis_id="will-be-set", status="refuted", confidence=0.0, reasoning="nope"),
        ]
    ))
    mock_llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(child_hypotheses=[]))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="DB connections exhausted",
        confidence=0.9,
        remediation_action="scale checkout deployment to 5 replicas",
    ))

    result = await coord.run(inc.id)

    assert result.status == IncidentStatus.PENDING_APPROVAL
    assert result.remediation is not None
    assert result.remediation.action == "scale checkout deployment to 5 replicas"
    assert len(result.hypotheses) == 2
    assert repo.save_count > 0


# ---------------------------------------------------------------------------
# Exploratory evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exploratory_evidence_gathered():
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)
    register_exploratory(agents)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(hypotheses=[]))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="no findings", confidence=0.0, remediation_action="none",
    ))

    result = await coord.run(inc.id)

    assert len(result.exploratory_evidence) == 6
    sources = {e.source for e in result.exploratory_evidence}
    assert sources == {
        EvidenceSource.PROMETHEUS, EvidenceSource.LOKI, EvidenceSource.TEMPO,
        EvidenceSource.KUBERNETES, EvidenceSource.DEPLOYMENT, EvidenceSource.DOCUMENTATION,
    }


# ---------------------------------------------------------------------------
# Hypothesis generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hypothesis_generation():
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)
    register_exploratory(agents)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[
            HypothesisProposal(statement="root A"),
            HypothesisProposal(statement="child of A", parent_id="will-be-set"),
        ]
    ))
    # Targeted evidence for both hypotheses (defaults to metrics)
    agents["metrics"].register(Evidence(
        source=EvidenceSource.PROMETHEUS, query="q", summary="ok", collected_by="metrics",
    ))
    agents["metrics"].register(Evidence(
        source=EvidenceSource.PROMETHEUS, query="q", summary="ok", collected_by="metrics",
    ))
    # Evaluate
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=0.5, reasoning="ok"),
            HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=0.5, reasoning="ok"),
        ]
    ))
    mock_llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(child_hypotheses=[]))
    mock_llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(child_hypotheses=[]))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="done", confidence=0.5, remediation_action="restart deployment",
    ))

    result = await coord.run(inc.id)

    assert len(result.hypotheses) == 2
    root = result.hypotheses[0]
    child = result.hypotheses[1]
    assert root.is_root
    assert not child.is_root
    assert child.parent_id == root.id


# ---------------------------------------------------------------------------
# Hypothesis evaluation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hypothesis_evaluation():
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)
    register_exploratory(agents)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[HypothesisProposal(statement="something is wrong")]
    ))
    agents["metrics"].register(Evidence(
        source=EvidenceSource.PROMETHEUS, query="q", summary="evidence text",
        collected_by="metrics_agent",
    ))
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=0.85, reasoning="good"),
        ]
    ))
    mock_llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(child_hypotheses=[]))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="done", confidence=0.85, remediation_action="restart deployment",
    ))

    result = await coord.run(inc.id)

    assert result.hypotheses[0].status == HypothesisStatus.CONFIRMED
    assert result.hypotheses[0].confidence == 0.85


# ---------------------------------------------------------------------------
# Pruning refuted branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pruning_refuted():
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)
    register_exploratory(agents)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[
            HypothesisProposal(statement="parent issue"),
            HypothesisProposal(statement="child issue 1", parent_id="will-be-set"),
            HypothesisProposal(statement="child issue 2", parent_id="will-be-set"),
        ]
    ))
    # 3 targeted evidence for 3 hypotheses
    for _ in range(3):
        agents["metrics"].register(Evidence(
            source=EvidenceSource.PROMETHEUS, query="q", summary="ok", collected_by="metrics",
        ))

    # Evaluate: parent refuted, children confirmed (LLM doesn't know parent is refuted)
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="refuted", confidence=0.0, reasoning="wrong"),
            HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=0.5, reasoning="meh"),
            HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=0.5, reasoning="meh"),
        ]
    ))
    # After pruning, children are refuted — no confirmed hypotheses remain
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="no findings", confidence=0.0, remediation_action="none",
    ))

    result = await coord.run(inc.id)

    # All three should be refuted after pruning
    for h in result.hypotheses:
        assert h.status == HypothesisStatus.REFUTED


# ---------------------------------------------------------------------------
# Agent failure — graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_failure_graceful():
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()

    # 5 agents succeed, 1 fails
    agents["metrics"].register(Evidence(
        source=EvidenceSource.PROMETHEUS, query="q", summary="metrics ok", collected_by="metrics",
    ))
    agents["logs"].register(Evidence(
        source=EvidenceSource.LOKI, query="q", summary="logs ok", collected_by="logs",
    ))
    agents["traces"].register(Evidence(
        source=EvidenceSource.TEMPO, query="q", summary="traces ok", collected_by="traces",
    ))
    agents["kubernetes"].register(Evidence(
        source=EvidenceSource.KUBERNETES, query="q", summary="k8s ok", collected_by="k8s",
    ))

    async def failing_agent(*args, **kwargs):
        raise RuntimeError("MCP connection refused")

    agents["deployment"] = failing_agent
    agents["knowledge"].register(Evidence(
        source=EvidenceSource.DOCUMENTATION, query="q", summary="docs ok", collected_by="docs",
    ))

    await repo.save(inc)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(hypotheses=[]))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="partial data", confidence=0.0, remediation_action="none",
    ))

    result = await coord.run(inc.id)

    assert len(result.exploratory_evidence) == 6
    errors = [e for e in result.exploratory_evidence if e.summary.startswith("Error:")]
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# All agents fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_agents_fail():
    inc = make_incident()

    async def always_fail(*args, **kwargs):
        raise RuntimeError("unavailable")

    coord, mock_llm, repo, _ = make_coordinator(
        metrics_agent=always_fail,
        logs_agent=always_fail,
        traces_agent=always_fail,
        kubernetes_agent=always_fail,
        deployment_agent=always_fail,
        knowledge_agent=always_fail,
    )
    await repo.save(inc)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(hypotheses=[]))
    # Need 2 RCAOutput: one for no-confirmed path, one for the actual RCA
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="insufficient data — all sources unavailable",
        confidence=0.0,
        remediation_action="none",
    ))

    result = await coord.run(inc.id)

    assert len(result.exploratory_evidence) == 6
    assert all(e.summary.startswith("Error:") for e in result.exploratory_evidence)
    assert result.status == IncidentStatus.PENDING_APPROVAL


# ---------------------------------------------------------------------------
# Save after each step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saves_after_each_step():
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)
    initial_saves = repo.save_count
    register_exploratory(agents)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(hypotheses=[]))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="done", confidence=0.0, remediation_action="none",
    ))

    await coord.run(inc.id)

    # Saves: acknowledge, exploratory, generate hypotheses, produce RCA, propose remediation
    assert repo.save_count > initial_saves + 3


# ---------------------------------------------------------------------------
# No confirmed hypotheses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_confirmed_hypotheses():
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)
    register_exploratory(agents)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[HypothesisProposal(statement="something")]
    ))
    agents["metrics"].register(Evidence(
        source=EvidenceSource.PROMETHEUS, query="q", summary="ok", collected_by="metrics",
    ))
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="refuted", confidence=0.0, reasoning="nope"),
        ]
    ))
    # RCA for the no-confirmed path
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="no root cause found", confidence=0.0, remediation_action="none",
    ))

    result = await coord.run(inc.id)

    assert result.status == IncidentStatus.PENDING_APPROVAL
    assert result.hypotheses[0].status == HypothesisStatus.REFUTED


# ---------------------------------------------------------------------------
# Demo — full integration scenario with mock LLM and mock agents
# ---------------------------------------------------------------------------


def demo():
    """Run the full checkout-latency investigation with mock LLM and mock agents."""
    import asyncio

    async def _demo():
        print("=" * 60)
        print("DEMO: Coordinator-Driven Investigation")
        print("=" * 60)

        source = AlertSource(name="prod-alertmanager", cluster_id="us-east-1")
        alert = Alert(
            source=source,
            fingerprint="checkout-latency-high",
            labels={"service": "checkout", "namespace": "prod", "severity": "critical"},
            severity="critical",
            cluster_id="us-east-1",
        )
        inc = Incident(cluster_id="us-east-1", title="Checkout latency > 500ms")
        inc.add_alert(alert)

        repo = InMemoryIncidentRepository()
        await repo.save(inc)

        mock_llm = MockLLMProvider()
        agents = {
            "metrics": MockAgent(),
            "logs": MockAgent(),
            "traces": MockAgent(),
            "kubernetes": MockAgent(),
            "deployment": MockAgent(),
            "knowledge": MockAgent(),
        }

        coord = Coordinator(
            llm=mock_llm, repo=repo,
            metrics_agent=agents["metrics"],
            logs_agent=agents["logs"],
            trace_agent=agents["traces"],
            kubernetes_agent=agents["kubernetes"],
            deployment_agent=agents["deployment"],
            knowledge_agent=agents["knowledge"],
        )

        # Exploratory evidence
        register_exploratory(agents)

        # Hypothesis generation: single root hypothesis
        mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
            hypotheses=[
                HypothesisProposal(statement="database connections are exhausted"),
            ]
        ))

        # Targeted evidence for the hypothesis
        agents["metrics"].register(Evidence(
            source=EvidenceSource.PROMETHEUS, query="q", summary="DB connections: 95/100 (near max)",
            collected_by="metrics",
        ))

        # Evaluate: confirmed
        mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
            evaluations=[
                HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=0.9, reasoning="DB evidence"),
            ]
        ))

        # Deepen: 1 child
        mock_llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(
            child_hypotheses=[
                HypothesisProposal(statement="connection pool timeout was removed", parent_id="will-be-set"),
            ]
        ))

        # Targeted evidence for child
        agents["metrics"].register(Evidence(
            source=EvidenceSource.PROMETHEUS, query="q", summary="connection pool timeout removed in latest config",
            collected_by="metrics",
        ))

        # Evaluate child
        mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
            evaluations=[
                HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=0.95, reasoning="config change"),
            ]
        ))

        # Converge
        mock_llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(child_hypotheses=[]))

        # RCA
        mock_llm.register(RCAOutput, RCAOutput(
            root_cause_summary="Connection pool timeout was removed in the latest config change, causing database connection exhaustion.",
            confidence=0.95,
            remediation_action="rollback checkout deployment to v1.2.3",
        ))

        print("\n[COORDINATOR] Starting investigation...")
        result = await coord.run(inc.id)

        print(f"\n[RESULT] Status: {result.status.value}")
        print(f"[RESULT] Hypotheses: {len(result.hypotheses)}")
        for h in result.hypotheses:
            print(f"  - [{h.status.value}] {h.statement} (confidence: {h.confidence})")
        print(f"[RESULT] Remediation: {result.remediation.action}")
        print(f"[RESULT] Exploratory evidence: {len(result.exploratory_evidence)} items")

        report = result.rca_report()
        print(f"\n[RCA] Root cause: {report['root_cause']}")
        print(f"[RCA] Confidence: {report['confidence']}")

        print(f"\n{'=' * 60}")
        print("DEMO COMPLETE")
        print(f"{'=' * 60}")

        assert result.status == IncidentStatus.PENDING_APPROVAL
        assert result.remediation is not None
        assert "rollback" in result.remediation.action
        confirmed = [h for h in result.hypotheses if h.status == HypothesisStatus.CONFIRMED]
        assert len(confirmed) >= 2  # root + child
        assert report["confidence"] > 0.0

        return result, report

    return asyncio.run(_demo())


if __name__ == "__main__":
    demo()