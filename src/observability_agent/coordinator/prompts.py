"""Prompt templates for the Coordinator's LLM calls.

Progressive context — each call gets only the fields it needs, never raw payloads.
"""

from __future__ import annotations

from observability_agent.domain.models import Evidence, Hypothesis


SYSTEM_PROMPT = """You are an AI Site Reliability Engineer investigating a production incident.
You have access to observability data from Prometheus, Loki, Tempo, Kubernetes, deployment history, and documentation.
Be precise, evidence-driven, and cite specific data points in your reasoning.
Always prefer the most specific root cause over vague generalizations."""


def format_evidence(evidence: Evidence) -> str:
    """Render one Evidence for a prompt: summary, plus computed facts when present.

    The single formatting point for Evidence across all prompts — the Coordinator
    passes pre-formatted strings to the builders that take summaries.
    """
    line = f"- [{evidence.source.value}] {evidence.summary}"
    if evidence.signals:
        facts = " ".join(f"{k}={v}" for k, v in evidence.signals.items())
        line += f"\n  facts: {facts}"
    return line


def generate_hypotheses_prompt(
    alert_labels: dict[str, str],
    exploratory_evidence: list[Evidence],
) -> tuple[str, str]:
    """Build prompt for the hypothesis generation step.

    Context: alert labels + exploratory evidence summaries and computed signals.
    """
    evidence_text = "\n".join(format_evidence(e) for e in exploratory_evidence)
    if not evidence_text:
        evidence_text = "(no exploratory evidence gathered yet)"

    user_prompt = f"""An alert fired with the following labels:
{_format_dict(alert_labels)}

Exploratory evidence gathered so far:
{evidence_text}

Based on this evidence, propose root-cause hypotheses. Each hypothesis should be a specific, testable statement.
Group related hypotheses under a common parent. Start with broad hypotheses, then add child hypotheses for specifics.

Return a structured output with your hypotheses."""
    return SYSTEM_PROMPT, user_prompt


def evaluate_hypotheses_prompt(
    hypothesis: Hypothesis,
    evidence_summaries: list[str],
) -> tuple[str, str]:
    """Build prompt for evaluating a single hypothesis.

    Context: the hypothesis statement + its linked evidence summaries.
    """
    evidence_text = "\n".join(f"- {s}" for s in evidence_summaries)
    if not evidence_text:
        evidence_text = "(no evidence gathered for this hypothesis)"

    user_prompt = f"""Evaluate this hypothesis:

Hypothesis: {hypothesis.statement}

Evidence gathered:
{evidence_text}

Determine whether this hypothesis is CONFIRMED or REFUTED based on the evidence.
If confirmed, assign a confidence score (0.0–1.0) and explain your reasoning.
If refuted, explain why the evidence contradicts the hypothesis.

Return a structured output with your evaluation."""
    return SYSTEM_PROMPT, user_prompt


def deepen_hypotheses_prompt(
    hypothesis: Hypothesis,
    ancestor_chain: list[Hypothesis],
    evidence_summaries: list[str],
) -> tuple[str, str]:
    """Build prompt for generating child hypotheses from a confirmed parent.

    Context: the confirmed hypothesis, its ancestor chain, and its evidence.
    """
    chain_text = " → ".join(h.statement for h in ancestor_chain)
    evidence_text = "\n".join(f"- {s}" for s in evidence_summaries)

    user_prompt = f"""The following hypothesis has been CONFIRMED:

Causal chain so far: {chain_text}

Current hypothesis: {hypothesis.statement} (confidence: {hypothesis.confidence})

Supporting evidence:
{evidence_text}

Now drill deeper. What could be causing {hypothesis.statement}?
Propose child hypotheses that explain the mechanism behind this confirmed finding.
Each child should be more specific than its parent — move from "what" to "why" or "how".

Return a structured output with child hypotheses."""
    return SYSTEM_PROMPT, user_prompt


def produce_rca_prompt(
    confirmed_path: list[Hypothesis],
    all_evidence: list[Evidence],
) -> tuple[str, str]:
    """Build prompt for producing the final RCA.

    Context: the full confirmed path + all evidence summaries.
    """
    chain_text = " → ".join(h.statement for h in confirmed_path)
    evidence_text = "\n".join(
        f"{format_evidence(e)} (confidence: {h.confidence if (h := _find_parent(e, confirmed_path)) else 'N/A'})"
        for e in all_evidence
    )

    user_prompt = f"""The investigation is complete. Here is the confirmed causal chain:

{chain_text}

All evidence gathered:
{evidence_text}

Produce the final Root Cause Analysis:
1. Summarize the root cause in a clear, concise narrative
2. Assign an overall confidence score (0.0–1.0)
3. Recommend a specific remediation action using prefix conventions:
   - "rollback <deployment> to revision <rev>"
   - "restart deployment <name> in <namespace>"
   - "scale <deployment> to <N> replicas"
   - "clear consumer group <name> on <topic>"
   - "rotate certificate <name>"

Return a structured output."""
    return SYSTEM_PROMPT, user_prompt


def _format_dict(d: dict[str, str]) -> str:
    return "\n".join(f"  {k}: {v}" for k, v in d.items())


def _find_parent(
    evidence: Evidence, hypotheses: list[Hypothesis]
) -> Hypothesis | None:
    if evidence.hypothesis_id is None:
        return None
    return next((h for h in hypotheses if h.id == evidence.hypothesis_id), None)
