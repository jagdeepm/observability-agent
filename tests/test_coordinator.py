"""Tests for the Coordinator investigation engine — mock-everything approach.

All LLM calls and agent calls are mocked. These tests verify the Coordinator's
mechanics: does it call the right agents in the right order, apply LLM outputs
correctly to the Incident, and handle the full lifecycle?
"""

from __future__ import annotations

import asyncio
import time

import pytest

from observability_agent.agents.protocols import MockAgent
from observability_agent.coordinator.engine import (
    MAX_CHILDREN_PER_DEEPEN,
    MAX_DEEPEN_PER_LEVEL,
    MAX_HYPOTHESES_PER_GENERATE,
    Coordinator,
)
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
    max_depth: int = 2,
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
        max_depth=max_depth,
        metrics_agent=agents["metrics"],
        logs_agent=agents["logs"],
        trace_agent=agents["traces"],
        kubernetes_agent=agents["kubernetes"],
        deployment_agent=agents["deployment"],
        knowledge_agent=agents["knowledge"],
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


class SlowMockAgent(MockAgent):
    """A MockAgent whose call genuinely yields control, via a real sleep.

    A mock that returns instantly can't distinguish concurrent scheduling from
    sequential — both finish in the same (near-zero) wall time. This one only
    proves the point if the delay is real.
    """

    def __init__(self, responses: list[Evidence], delay: float) -> None:
        super().__init__(responses)
        self.delay = delay

    async def __call__(self, *args, **kwargs) -> Evidence:
        await asyncio.sleep(self.delay)
        return await super().__call__(*args, **kwargs)


@pytest.mark.asyncio
async def test_targeted_evidence_gathered_concurrently():
    """Evidence for independent hypotheses is gathered in parallel, not one at a
    time — mirroring the exploratory-evidence phase, which already does this.

    Three hypotheses route to three different agents (via _pick_source keywords),
    each taking a real 0.15s. The same three agents also answer the exploratory
    phase, which is already concurrent — so the honest floor is two concurrent
    waves (~0.30s), not one. If targeted gathering regressed to sequential, the
    total would be exploratory (0.15s) + 3×0.15s sequential = 0.60s. The
    assertion sits between those two, well clear of either.
    """
    inc = make_incident()
    delay = 0.15
    agents = {
        "metrics": MockAgent(),
        "logs": SlowMockAgent(
            [Evidence(source=EvidenceSource.LOKI, query="q", summary="log ev", collected_by="logs_agent")],
            delay=delay,
        ),
        "traces": SlowMockAgent(
            [Evidence(source=EvidenceSource.TEMPO, query="q", summary="trace ev", collected_by="trace_agent")],
            delay=delay,
        ),
        "kubernetes": MockAgent(),
        "deployment": SlowMockAgent(
            [Evidence(source=EvidenceSource.DEPLOYMENT, query="q", summary="deploy ev", collected_by="deployment_agent")],
            delay=delay,
        ),
        "knowledge": MockAgent(),
    }
    coord, mock_llm, repo, agents = make_coordinator(**agents)
    await repo.save(inc)
    register_exploratory(agents)

    # Keywords route to logs, traces, and deployment respectively (see _pick_source).
    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[
            HypothesisProposal(statement="an exception is logged during checkout"),
            HypothesisProposal(statement="a slow trace span points at the database"),
            HypothesisProposal(statement="a recent deploy changed the config"),
        ]
    ))
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="refuted", confidence=0.0, reasoning="a"),
            HypothesisEvaluation(hypothesis_id="will-be-set", status="refuted", confidence=0.0, reasoning="b"),
            HypothesisEvaluation(hypothesis_id="will-be-set", status="refuted", confidence=0.0, reasoning="c"),
        ]
    ))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="no confirmed hypothesis", confidence=0.0, remediation_action="none",
    ))

    started = time.monotonic()
    result = await coord.run(inc.id)
    elapsed = time.monotonic() - started

    assert elapsed < delay * 3, (
        f"took {elapsed:.3f}s — evidence gathering does not look concurrent "
        f"(sequential total would be ~{delay * 4:.3f}s; concurrent is ~{delay * 2:.3f}s)"
    )
    assert result.status in (IncidentStatus.RCA_PRODUCED, IncidentStatus.PENDING_APPROVAL, IncidentStatus.OPEN)


@pytest.mark.asyncio
async def test_computed_signals_reach_the_llm_prompts():
    """Evidence signals are rendered into prompts — not stranded on the object.

    Without this, signals could be computed correctly and influence nothing.
    """
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)

    for key, source in [
        ("metrics", EvidenceSource.PROMETHEUS),
        ("logs", EvidenceSource.LOKI),
        ("traces", EvidenceSource.TEMPO),
        ("kubernetes", EvidenceSource.KUBERNETES),
        ("deployment", EvidenceSource.DEPLOYMENT),
        ("knowledge", EvidenceSource.DOCUMENTATION),
    ]:
        agents[key].register(Evidence(
            source=source, query="exploratory", summary=f"{key} ok", collected_by=key,
            signals={"anomaly": True, "p95": 4.2} if key == "metrics" else {},
        ))

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[HypothesisProposal(statement="database connections are exhausted")]
    ))
    agents["metrics"].register(Evidence(
        source=EvidenceSource.PROMETHEUS, query="q", summary="DB connections near max",
        collected_by="metrics_agent", signals={"anomaly": True, "spike_count": 3},
    ))
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed",
                                 confidence=0.9, reasoning="ok"),
        ]
    ))
    mock_llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(child_hypotheses=[]))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="DB connections exhausted", confidence=0.9,
        remediation_action="scale up",
    ))

    await coord.run(inc.id)

    prompts = [c["prompt"] for c in mock_llm.calls]

    # Hypothesis generation sees the exploratory signals
    generation = next(p for p in prompts if "propose root-cause hypotheses" in p.lower())
    assert "p95=4.2" in generation
    assert "anomaly=True" in generation

    # Hypothesis evaluation sees the targeted evidence signals
    evaluation = next(p for p in prompts if p.startswith("Evaluate these hypotheses"))
    assert "spike_count=3" in evaluation

    # Evidence without signals renders exactly as before
    assert "logs ok" in generation


@pytest.mark.asyncio
async def test_evidence_without_signals_renders_unchanged():
    """The facts line is omitted entirely when there are no signals."""
    from observability_agent.coordinator.prompts import format_evidence

    plain = Evidence(
        source=EvidenceSource.LOKI, query="q", summary="nothing unusual", collected_by="logs",
    )

    assert format_evidence(plain) == "- [loki] nothing unusual"
    assert "facts" not in format_evidence(plain)


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
# Hypothesis breadth cap — spec: docs/specs/investigation-cost-guardrails.md
#
# A real LLM does not share a mock's restraint. These deliberately register
# more than the cap allows, which the existing suite never had reason to do.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generation_is_truncated_to_the_cap():
    """More hypotheses than the cap allows are proposed; only the cap survives."""
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)
    register_exploratory(agents)

    proposed = MAX_HYPOTHESES_PER_GENERATE + 5
    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[HypothesisProposal(statement=f"hyp {i}") for i in range(proposed)]
    ))
    for _ in range(MAX_HYPOTHESES_PER_GENERATE):
        agents["metrics"].register(Evidence(
            source=EvidenceSource.PROMETHEUS, query="q", summary="ok", collected_by="metrics",
        ))
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="refuted", confidence=0.0, reasoning="no")
            for _ in range(MAX_HYPOTHESES_PER_GENERATE)
        ]
    ))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="none confirmed", confidence=0.0, remediation_action="none",
    ))

    result = await coord.run(inc.id)

    assert len(result.hypotheses) == MAX_HYPOTHESES_PER_GENERATE


@pytest.mark.asyncio
async def test_generation_at_exactly_the_cap_is_not_truncated():
    """The boundary is inclusive — exactly the cap keeps every proposal."""
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator()
    await repo.save(inc)
    register_exploratory(agents)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[HypothesisProposal(statement=f"hyp {i}") for i in range(MAX_HYPOTHESES_PER_GENERATE)]
    ))
    for _ in range(MAX_HYPOTHESES_PER_GENERATE):
        agents["metrics"].register(Evidence(
            source=EvidenceSource.PROMETHEUS, query="q", summary="ok", collected_by="metrics",
        ))
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="refuted", confidence=0.0, reasoning="no")
            for _ in range(MAX_HYPOTHESES_PER_GENERATE)
        ]
    ))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="none confirmed", confidence=0.0, remediation_action="none",
    ))

    result = await coord.run(inc.id)

    assert len(result.hypotheses) == MAX_HYPOTHESES_PER_GENERATE


@pytest.mark.asyncio
async def test_deepen_call_output_is_truncated_to_the_cap():
    """One confirmed hypothesis proposes more children than the cap allows."""
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator(max_depth=1)
    await repo.save(inc)
    register_exploratory(agents)

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[HypothesisProposal(statement="root cause candidate")]
    ))
    agents["metrics"].register(Evidence(
        source=EvidenceSource.PROMETHEUS, query="q", summary="ok", collected_by="metrics",
    ))
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=0.9, reasoning="ok"),
        ]
    ))
    proposed_children = MAX_CHILDREN_PER_DEEPEN + 5
    mock_llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(
        child_hypotheses=[HypothesisProposal(statement=f"child {i}") for i in range(proposed_children)]
    ))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="root cause candidate", confidence=0.9, remediation_action="restart deployment x",
    ))

    result = await coord.run(inc.id)

    assert len(result.hypotheses) == 1 + MAX_CHILDREN_PER_DEEPEN


@pytest.mark.asyncio
async def test_only_the_most_confident_leaves_are_deepened():
    """More confirmed leaves than MAX_DEEPEN_PER_LEVEL — only the top-K by
    confidence get a deepen call. The rest stay confirmed, not dropped.

    Confidence is deliberately not correlated with list position (index 7 is
    selected, indices 1/3/5 in the middle are not), so this fails if deepening
    picks by list order rather than by confidence.
    """
    assert MAX_HYPOTHESES_PER_GENERATE > MAX_DEEPEN_PER_LEVEL, (
        "test assumes the generation cap exceeds the deepen-per-level cap; "
        "adjust this test if that relationship changes"
    )
    inc = make_incident()
    coord, mock_llm, repo, agents = make_coordinator(max_depth=1)
    await repo.save(inc)
    register_exploratory(agents)

    n = MAX_DEEPEN_PER_LEVEL + 3
    confidences = [0.9, 0.1, 0.8, 0.1, 0.7, 0.1, 0.6, 0.5][:n]
    expected_deepened = {i for i, c in enumerate(confidences) if c >= 0.5}
    assert len(expected_deepened) == MAX_DEEPEN_PER_LEVEL

    mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[HypothesisProposal(statement=f"hyp {i}") for i in range(n)]
    ))
    for _ in range(n):
        agents["metrics"].register(Evidence(
            source=EvidenceSource.PROMETHEUS, query="q", summary="ok", collected_by="metrics",
        ))
    mock_llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(hypothesis_id="will-be-set", status="confirmed", confidence=c, reasoning="ok")
            for c in confidences
        ]
    ))
    for _ in range(MAX_DEEPEN_PER_LEVEL):
        mock_llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(child_hypotheses=[]))
    mock_llm.register(RCAOutput, RCAOutput(
        root_cause_summary="hyp 0", confidence=0.9, remediation_action="restart deployment x",
    ))

    result = await coord.run(inc.id)

    deepen_prompts = [c["prompt"] for c in mock_llm.calls if c["output_schema"] == "DeepenHypothesesOutput"]
    assert len(deepen_prompts) == MAX_DEEPEN_PER_LEVEL
    for i in range(n):
        stmt = f"hyp {i}"
        was_deepened = any(stmt in p for p in deepen_prompts)
        assert was_deepened == (i in expected_deepened), (
            f"{stmt} (confidence {confidences[i]}): expected deepened={i in expected_deepened}"
        )

    # Nothing dropped — every hypothesis is still present and confirmed,
    # whether or not it was one of the ones deepened.
    assert len(result.hypotheses) == n
    assert all(h.status == HypothesisStatus.CONFIRMED for h in result.hypotheses)


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
