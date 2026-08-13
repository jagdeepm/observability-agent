"""Pydantic models for LLM structured outputs.

Each model corresponds to a Coordinator LLM call. The LLM returns
validated instances of these, and the Coordinator maps them to domain operations.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HypothesisProposal(BaseModel):
    """A single hypothesis proposed by the LLM."""

    statement: str = Field(description="The hypothesis statement")
    parent_id: str | None = Field(
        default=None,
        description="Parent hypothesis ID. None for root hypotheses.",
    )


class GenerateHypothesesOutput(BaseModel):
    """LLM output for the hypothesis generation step."""

    hypotheses: list[HypothesisProposal] = Field(
        description="List of hypotheses, each with optional parent_id for tree structure"
    )


class HypothesisEvaluation(BaseModel):
    """Evaluation of a single hypothesis."""

    hypothesis_id: str = Field(description="The ID of the hypothesis being evaluated")
    status: str = Field(
        description="Either 'confirmed' or 'refuted'",
        pattern="^(confirmed|refuted)$",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score 0.0–1.0. Required when status is 'confirmed'.",
    )
    reasoning: str = Field(
        description="Why this conclusion, citing specific evidence"
    )


class EvaluateHypothesesOutput(BaseModel):
    """LLM output for the hypothesis evaluation step."""

    evaluations: list[HypothesisEvaluation] = Field(
        description="Evaluation for each proposed hypothesis"
    )


class DeepenHypothesesOutput(BaseModel):
    """LLM output for the deepen step — child hypotheses for confirmed parents."""

    child_hypotheses: list[HypothesisProposal] = Field(
        default_factory=list,
        description="Child hypotheses that drill down into confirmed parents",
    )


class RCAOutput(BaseModel):
    """LLM output for the RCA production step."""

    root_cause_summary: str = Field(
        description="The root cause narrative, explaining the full causal chain"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall confidence in the root cause",
    )
    remediation_action: str = Field(
        description="Recommended remediation action, using prefix convention (e.g., 'rollback checkout deployment to v1.2.3')"
    )