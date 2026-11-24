"""Domain models for the AI-Native Cloud Observability Agent.

Bounded contexts:
- Alerting: Alert, AlertSource
- Investigation: Incident (aggregate root), Hypothesis, Evidence, Remediation
- Topology: Node, Edge
- Memory: ResolvedIncident
- Knowledge/RAG: infrastructure only, no domain entities
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class IncidentStatus(str, Enum):
    """Lifecycle states for an Incident.

    Terminal: resolved, false_positive, closed.
    Looping: failed, rejected both return to investigating.
    """

    OPEN = "open"
    INVESTIGATING = "investigating"
    FALSE_POSITIVE = "false_positive"
    RCA_PRODUCED = "rca_produced"
    PENDING_APPROVAL = "pending_approval"
    REMEDIATING = "remediating"
    RESOLVED = "resolved"
    FAILED = "failed"  # remediation failed, back to investigating
    CLOSED = "closed"  # manual close of stale incident

    @property
    def is_terminal(self) -> bool:
        return self in (IncidentStatus.RESOLVED, IncidentStatus.FALSE_POSITIVE, IncidentStatus.CLOSED)

    @property
    def is_active(self) -> bool:
        return not self.is_terminal


class HypothesisStatus(str, Enum):
    PROPOSED = "proposed"
    INVESTIGATING = "investigating"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"


class RemediationStatus(str, Enum):
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    EXECUTED = "executed"
    VERIFIED = "verified"
    FAILED = "failed"


# A scalar fact computed from a telemetry payload rather than inferred by the LLM.
Signal = str | int | float | bool


class EvidenceSource(str, Enum):
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    TEMPO = "tempo"
    KUBERNETES = "kubernetes"
    DEPLOYMENT = "deployment"
    DOCUMENTATION = "documentation"


class NodeType(str, Enum):
    SERVICE = "service"
    DATABASE = "database"
    QUEUE = "queue"
    TOPIC = "topic"
    DEPLOYMENT = "deployment"
    DASHBOARD = "dashboard"
    RUNBOOK = "runbook"
    OWNER = "owner"


class EdgeRelationship(str, Enum):
    DEPENDS_ON = "depends_on"
    PUBLISHES_TO = "publishes_to"
    SUBSCRIBES_TO = "subscribes_to"
    OWNS = "owns"
    MONITORED_BY = "monitored_by"
    DOCUMENTED_BY = "documented_by"


class Resolution(str, Enum):
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"
    CLOSED = "closed"


class UserRole(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class AlertStatus(str, Enum):
    FIRING = "firing"
    RESOLVED = "resolved"


# ---------------------------------------------------------------------------
# Alerting context
# ---------------------------------------------------------------------------


class AlertSource(BaseModel):
    """Where alerts come from — e.g. an Alertmanager instance."""

    name: str
    type: str = "alertmanager"
    cluster_id: str


class Alert(BaseModel):
    """An inbound alert signal. Immutable fact — alerts are not updated, only created."""

    id: str = Field(default_factory=lambda: f"alert-{uuid.uuid4().hex[:8]}")
    source: AlertSource
    fingerprint: str
    labels: dict[str, str] = Field(default_factory=dict)
    severity: str = "warning"
    status: AlertStatus = AlertStatus.FIRING
    fired_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    cluster_id: str

    def resolve(self) -> None:
        self.status = AlertStatus.RESOLVED
        self.resolved_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Investigation context — core aggregate
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    """A single entry in the incident audit log."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: str  # user_id or agent name
    action: str
    note: str = ""


class Evidence(BaseModel):
    """A piece of evidence gathered from an observability source.

    hypothesis_id is nullable — exploratory evidence (initial sweep) is not tied to any hypothesis.
    """

    id: str = Field(default_factory=lambda: f"ev-{uuid.uuid4().hex[:8]}")
    source: EvidenceSource
    query: str  # the exact PromQL / LogQL / kubectl command / search query
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    hypothesis_id: str | None = None
    summary: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    collected_by: str = ""  # agent type that collected this
    # Facts computed in code rather than inferred by the LLM (see analysis.py).
    # Rendered into Coordinator prompts alongside summary; empty for sources
    # whose payloads carry nothing worth computing.
    signals: dict[str, Signal] = Field(default_factory=dict)


class Hypothesis(BaseModel):
    """A hypothesis in the investigation tree. Parent-child adjacency forms a causal chain."""

    id: str = Field(default_factory=lambda: f"hyp-{uuid.uuid4().hex[:8]}")
    parent_id: str | None = None
    statement: str
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = ""  # agent type that proposed this
    evidence: list[Evidence] = Field(default_factory=list)

    def start_investigating(self) -> None:
        self.status = HypothesisStatus.INVESTIGATING

    def confirm(self, confidence: float = 1.0) -> None:
        self.status = HypothesisStatus.CONFIRMED
        self.confidence = confidence

    def refute(self) -> None:
        self.status = HypothesisStatus.REFUTED
        self.confidence = 0.0

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def is_leaf(self) -> bool:
        """A leaf has no children in the tree. Determined by the Incident, not self."""
        return True  # Incident resolves this by scanning all hypotheses

    def ancestor_chain(self, all_hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        """Walk up to the root, returning [self, parent, grandparent, ..., root]."""
        chain = [self]
        current = self
        by_id = {h.id: h for h in all_hypotheses}
        while current.parent_id and current.parent_id in by_id:
            current = by_id[current.parent_id]
            chain.append(current)
        return chain

    def confirmed_path(self, all_hypotheses: list[Hypothesis]) -> list[Hypothesis] | None:
        """Return the confirmed chain from root to self, or None if any link is not confirmed."""
        chain = self.ancestor_chain(all_hypotheses)
        if all(h.status == HypothesisStatus.CONFIRMED for h in chain):
            return list(reversed(chain))  # root first
        return None


class Remediation(BaseModel):
    """A remediation action within an Incident. Has its own mini-state-machine."""

    status: RemediationStatus = RemediationStatus.PROPOSED
    action: str
    approver: str | None = None
    proposed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    executed_at: datetime | None = None
    verification_result: str | None = None

    def propose(self) -> None:
        self.status = RemediationStatus.PROPOSED

    def request_approval(self) -> None:
        self.status = RemediationStatus.PENDING_APPROVAL

    def approve(self, user_id: str) -> None:
        if self.status != RemediationStatus.PENDING_APPROVAL:
            raise ValueError(f"Cannot approve remediation in status {self.status}")
        self.status = RemediationStatus.APPROVED
        self.approver = user_id

    def reject(self, user_id: str) -> None:
        if self.status != RemediationStatus.PENDING_APPROVAL:
            raise ValueError(f"Cannot reject remediation in status {self.status}")
        self.status = RemediationStatus.REJECTED
        self.approver = user_id

    def execute(self) -> None:
        if self.status != RemediationStatus.APPROVED:
            raise ValueError(f"Cannot execute remediation in status {self.status}")
        self.status = RemediationStatus.EXECUTING
        self.executed_at = datetime.now(timezone.utc)

    def complete_execution(self) -> None:
        if self.status != RemediationStatus.EXECUTING:
            raise ValueError(f"Cannot complete execution in status {self.status}")
        self.status = RemediationStatus.EXECUTED

    def verify(self, success: bool) -> None:
        if self.status != RemediationStatus.EXECUTED:
            raise ValueError(f"Cannot verify remediation in status {self.status}")
        if success:
            self.status = RemediationStatus.VERIFIED
            self.verification_result = "remediation successful, alert cleared"
        else:
            self.status = RemediationStatus.FAILED
            self.verification_result = "remediation did not resolve the issue"

    def fail(self) -> None:
        """Mark remediation as failed without verification."""
        self.status = RemediationStatus.FAILED


class Incident(BaseModel):
    """The aggregate root for the Investigation bounded context.

    Manages the full lifecycle: alert → investigation → hypothesis tree → RCA → remediation → resolution.
    """

    id: str = Field(default_factory=lambda: f"inc-{uuid.uuid4().hex[:8]}")
    cluster_id: str
    status: IncidentStatus = IncidentStatus.OPEN
    title: str = ""
    linked_alerts: list[Alert] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    exploratory_evidence: list[Evidence] = Field(default_factory=list)
    remediation: Remediation | None = None
    audit_log: list[AuditEntry] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_stale: bool = False
    stale_flagged_at: datetime | None = None

    # ---- State transitions ----

    def _transition(self, from_status: IncidentStatus, to_status: IncidentStatus) -> None:
        if self.status != from_status:
            raise ValueError(f"Cannot transition from {self.status} to {to_status}; expected {from_status}")
        self.status = to_status
        self.updated_at = datetime.now(timezone.utc)

    def acknowledge(self) -> None:
        """Acknowledge the alert and begin investigation."""
        self._transition(IncidentStatus.OPEN, IncidentStatus.INVESTIGATING)
        self._log("system", "acknowledged", "Investigation started")

    def mark_false_positive(self, user_id: str) -> None:
        """Mark the alert as noise — no investigation needed."""
        if self.status not in (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING):
            raise ValueError(f"Cannot mark false positive from status {self.status}")
        self.status = IncidentStatus.FALSE_POSITIVE
        self.updated_at = datetime.now(timezone.utc)
        self._log(user_id, "marked_false_positive", "Alert determined to be noise")

    def start_investigation(self) -> None:
        self._transition(IncidentStatus.OPEN, IncidentStatus.INVESTIGATING)
        self._log("system", "started_investigation", "Investigation workflow initiated")

    def produce_rca(self) -> None:
        self._transition(IncidentStatus.INVESTIGATING, IncidentStatus.RCA_PRODUCED)
        self._log("system", "rca_produced", "Root cause analysis complete")

    def propose_remediation(self, action: str) -> Remediation:
        if self.status != IncidentStatus.RCA_PRODUCED:
            raise ValueError(f"Cannot propose remediation from status {self.status}")
        self.remediation = Remediation(action=action)
        self.remediation.request_approval()
        self.status = IncidentStatus.PENDING_APPROVAL
        self.updated_at = datetime.now(timezone.utc)
        self._log("system", "remediation_proposed", action)
        return self.remediation

    def approve_remediation(self, user_id: str) -> None:
        self._transition(IncidentStatus.PENDING_APPROVAL, IncidentStatus.REMEDIATING)
        if self.remediation is None:
            raise ValueError("No remediation to approve")
        self.remediation.approve(user_id)
        self._log(user_id, "approved_remediation", self.remediation.action)

    def reject_remediation(self, user_id: str) -> None:
        if self.status != IncidentStatus.PENDING_APPROVAL:
            raise ValueError(f"Cannot reject remediation from status {self.status}")
        if self.remediation is None:
            raise ValueError("No remediation to reject")
        self.remediation.reject(user_id)
        self.status = IncidentStatus.INVESTIGATING
        self.updated_at = datetime.now(timezone.utc)
        self._log(user_id, "rejected_remediation", self.remediation.action)

    def execute_remediation(self) -> None:
        if self.remediation is None:
            raise ValueError("No remediation to execute")
        self.remediation.execute()
        self._log("system", "executed_remediation", self.remediation.action)

    def verify_remediation(self, success: bool) -> None:
        if self.remediation is None:
            raise ValueError("No remediation to verify")
        self.remediation.complete_execution()
        self.remediation.verify(success)
        if success:
            self.status = IncidentStatus.RESOLVED
            self._log("system", "remediation_verified", "Incident resolved")
        else:
            self.status = IncidentStatus.FAILED
            self._log("system", "remediation_failed", self.remediation.verification_result or "")
            # ponytail: failed loops back to investigating — caller must call re_investigate()
        self.updated_at = datetime.now(timezone.utc)

    def re_investigate(self) -> None:
        """Return to investigating after a failed remediation."""
        self._transition(IncidentStatus.FAILED, IncidentStatus.INVESTIGATING)
        self._log("system", "re_investigating", "Returning to investigation after failed remediation")

    def close(self, user_id: str) -> None:
        """Manually close a stale or abandoned incident."""
        if self.status.is_terminal:
            raise ValueError(f"Incident is already terminal: {self.status}")
        self.status = IncidentStatus.CLOSED
        self.updated_at = datetime.now(timezone.utc)
        self._log(user_id, "closed", "Incident manually closed")

    def flag_stale(self) -> None:
        self.is_stale = True
        self.stale_flagged_at = datetime.now(timezone.utc)
        self._log("system", "flagged_stale", "Incident has exceeded investigation time threshold")

    # ---- Hypothesis management ----

    def add_hypothesis(
        self, statement: str, parent_id: str | None = None, created_by: str = ""
    ) -> Hypothesis:
        if self.status != IncidentStatus.INVESTIGATING:
            raise ValueError(f"Cannot add hypothesis in status {self.status}")
        hypothesis = Hypothesis(
            statement=statement, parent_id=parent_id, created_by=created_by
        )
        self.hypotheses.append(hypothesis)
        self._log(created_by or "system", "hypothesis_added", statement)
        return hypothesis

    def add_evidence(self, evidence: Evidence) -> None:
        if self.status != IncidentStatus.INVESTIGATING:
            raise ValueError(f"Cannot add evidence in status {self.status}")
        if evidence.hypothesis_id:
            hyp = next((h for h in self.hypotheses if h.id == evidence.hypothesis_id), None)
            if hyp is None:
                raise ValueError(f"Hypothesis {evidence.hypothesis_id} not found in incident")
            hyp.evidence.append(evidence)
        else:
            self.exploratory_evidence.append(evidence)
        self._log(
            evidence.collected_by or "system",
            "evidence_collected",
            f"{evidence.source.value}: {evidence.summary}",
        )

    # ---- Alert management ----

    def add_alert(self, alert: Alert) -> None:
        self.linked_alerts.append(alert)
        self._log("system", "alert_linked", f"Alert {alert.id} linked to incident")

    # ---- Audit ----

    def _log(self, actor: str, action: str, note: str = "") -> None:
        self.audit_log.append(AuditEntry(actor=actor, action=action, note=note))

    # ---- RCA projection ----

    def rca_report(self) -> dict[str, Any]:
        """Generate the RCA report as a projection over the hypothesis tree.

        The RCA is the confirmed hypothesis chain with the highest confidence,
        bundled with all supporting evidence.
        """
        if self.status not in (
            IncidentStatus.RCA_PRODUCED,
            IncidentStatus.PENDING_APPROVAL,
            IncidentStatus.REMEDIATING,
            IncidentStatus.RESOLVED,
        ):
            raise ValueError(f"Cannot produce RCA from status {self.status}")

        confirmed = [h for h in self.hypotheses if h.status == HypothesisStatus.CONFIRMED]
        if not confirmed:
            return {
                "incident_id": self.id,
                "title": self.title,
                "status": "no confirmed hypotheses",
                "root_cause": None,
                "confidence": 0.0,
                "evidence": [],
            }

        # Find the deepest confirmed path (highest confidence chain)
        best_leaf = max(confirmed, key=lambda h: h.confidence)
        path = best_leaf.confirmed_path(self.hypotheses)
        causal_chain = [h.statement for h in (path or [best_leaf])]

        all_evidence = []
        for h in path or [best_leaf]:
            all_evidence.extend(h.evidence)

        return {
            "incident_id": self.id,
            "title": self.title,
            "cluster_id": self.cluster_id,
            "severity": max((a.severity for a in self.linked_alerts), default="unknown"),
            "affected_services": list({a.labels.get("service", "unknown") for a in self.linked_alerts}),
            "root_cause": causal_chain[-1] if causal_chain else None,
            "causal_chain": causal_chain,
            "confidence": best_leaf.confidence,
            "evidence": [
                {
                    "source": e.source.value,
                    "query": e.query,
                    "summary": e.summary,
                    "collected_by": e.collected_by,
                }
                for e in all_evidence
            ],
            "remediation": self.remediation.action if self.remediation else None,
            "remediation_status": self.remediation.status.value if self.remediation else None,
        }

    # ---- Tree helpers ----

    def get_children(self, hypothesis: Hypothesis) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.parent_id == hypothesis.id]

    def get_root_hypotheses(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.is_root]

    def prune_refuted_branches(self) -> list[Hypothesis]:
        """Refute all children of refuted hypotheses. Returns list of pruned hypotheses."""
        refuted_ids = {h.id for h in self.hypotheses if h.status == HypothesisStatus.REFUTED}
        pruned = []
        for h in self.hypotheses:
            if h.parent_id in refuted_ids and h.status != HypothesisStatus.REFUTED:
                h.refute()
                pruned.append(h)
                self._log("system", "hypothesis_pruned", f"Refuted: {h.statement} (parent refuted)")
        return pruned


# ---------------------------------------------------------------------------
# Topology context
# ---------------------------------------------------------------------------


class Node(BaseModel):
    """A node in the service dependency topology graph."""

    id: str = Field(default_factory=lambda: f"node-{uuid.uuid4().hex[:8]}")
    type: NodeType
    name: str
    namespace: str = "default"
    cluster_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Edge(BaseModel):
    """A directed edge in the service dependency topology graph."""

    id: str = Field(default_factory=lambda: f"edge-{uuid.uuid4().hex[:8]}")
    source_node_id: str
    target_node_id: str
    relationship: EdgeRelationship
    cluster_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Memory context
# ---------------------------------------------------------------------------


class ResolvedIncident(BaseModel):
    """An archived snapshot of a resolved incident, stored for similarity search.

    The embedding field is handled by the vector database infrastructure, not the domain model.
    """

    incident_id: str
    alert_signature: dict[str, Any] = Field(default_factory=dict)
    resolution: Resolution
    duration_seconds: float = 0.0
    severity: str = "warning"
    affected_services: list[str] = Field(default_factory=list)
    hypothesis_tree: dict[str, Any] = Field(default_factory=dict)
    rca_summary: str = ""
    remediation_applied: str | None = None
    verified: bool = False
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
