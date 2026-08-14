"""Tests for the domain model — state machines, hypothesis tree, and the full demo scenario."""

from __future__ import annotations

import pytest

from observability_agent.domain import (
    Alert,
    AlertSource,
    AlertStatus,
    Edge,
    EdgeRelationship,
    Evidence,
    EvidenceSource,
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentStatus,
    Node,
    NodeType,
    Remediation,
    RemediationStatus,
    Resolution,
    ResolvedIncident,
)


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------


def test_alert_creation():
    source = AlertSource(name="prod-alertmanager", cluster_id="us-east-1")
    alert = Alert(
        source=source,
        fingerprint="abc123",
        labels={"service": "checkout", "severity": "critical"},
        severity="critical",
        cluster_id="us-east-1",
    )
    assert alert.status == AlertStatus.FIRING
    assert alert.resolved_at is None

    alert.resolve()
    assert alert.status == AlertStatus.RESOLVED
    assert alert.resolved_at is not None


# ---------------------------------------------------------------------------
# Incident state machine — valid transitions
# ---------------------------------------------------------------------------


def test_incident_happy_path():
    """open → investigating → rca_produced → pending_approval → remediating → resolved"""
    inc = Incident(cluster_id="us-east-1", title="Checkout latency > 500ms")

    assert inc.status == IncidentStatus.OPEN
    inc.acknowledge()
    assert inc.status == IncidentStatus.INVESTIGATING

    inc.produce_rca()
    assert inc.status == IncidentStatus.RCA_PRODUCED

    inc.propose_remediation("rollback checkout deployment to v1.2.3")
    assert inc.status == IncidentStatus.PENDING_APPROVAL
    assert inc.remediation is not None
    assert inc.remediation.status == RemediationStatus.PENDING_APPROVAL

    inc.approve_remediation("alice")
    assert inc.status == IncidentStatus.REMEDIATING
    assert inc.remediation.status == RemediationStatus.APPROVED
    assert inc.remediation.approver == "alice"

    inc.execute_remediation()
    assert inc.remediation.status == RemediationStatus.EXECUTING

    inc.verify_remediation(success=True)
    assert inc.status == IncidentStatus.RESOLVED
    assert inc.remediation.status == RemediationStatus.VERIFIED


def test_false_positive():
    inc = Incident(cluster_id="us-east-1", title="Test alert")
    inc.mark_false_positive("bob")
    assert inc.status == IncidentStatus.FALSE_POSITIVE
    assert inc.status.is_terminal


def test_manual_close():
    inc = Incident(cluster_id="us-east-1", title="Stale incident")
    inc.acknowledge()
    inc.close("bob")
    assert inc.status == IncidentStatus.CLOSED
    assert inc.status.is_terminal


def test_remediation_rejected_loops_back():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()
    inc.produce_rca()
    inc.propose_remediation("scale deployment")
    inc.reject_remediation("bob")
    assert inc.status == IncidentStatus.INVESTIGATING
    assert inc.remediation.status == RemediationStatus.REJECTED


def test_remediation_failed_loops_back():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()
    inc.produce_rca()
    inc.propose_remediation("restart pod")
    inc.approve_remediation("alice")
    inc.execute_remediation()
    inc.verify_remediation(success=False)
    assert inc.status == IncidentStatus.FAILED
    assert inc.remediation.status == RemediationStatus.FAILED

    inc.re_investigate()
    assert inc.status == IncidentStatus.INVESTIGATING


# ---------------------------------------------------------------------------
# Incident state machine — invalid transitions
# ---------------------------------------------------------------------------


def test_cannot_skip_states():
    inc = Incident(cluster_id="us-east-1", title="Test")
    with pytest.raises(ValueError, match="Cannot transition"):
        inc.produce_rca()  # not yet investigating

    with pytest.raises(ValueError, match="Cannot transition"):
        inc.approve_remediation("alice")  # not yet pending_approval


def test_cannot_close_terminal():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.mark_false_positive("bob")
    with pytest.raises(ValueError, match="terminal"):
        inc.close("bob")


def test_cannot_propose_remediation_from_open():
    inc = Incident(cluster_id="us-east-1", title="Test")
    with pytest.raises(ValueError, match="Cannot propose remediation"):
        inc.propose_remediation("do something")


def test_cannot_add_hypothesis_outside_investigating():
    inc = Incident(cluster_id="us-east-1", title="Test")
    with pytest.raises(ValueError, match="Cannot add hypothesis"):
        inc.add_hypothesis("root cause is X")


# ---------------------------------------------------------------------------
# Hypothesis tree
# ---------------------------------------------------------------------------


def test_hypothesis_tree_structure():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()

    root = inc.add_hypothesis("latency is in the database layer", created_by="coordinator")
    child = inc.add_hypothesis(
        "DB connection pool exhausted", parent_id=root.id, created_by="coordinator"
    )
    grandchild = inc.add_hypothesis(
        "connection leak from recent deploy", parent_id=child.id, created_by="coordinator"
    )

    assert root.is_root
    assert not child.is_root
    assert inc.get_children(root) == [child]
    assert inc.get_children(child) == [grandchild]
    assert inc.get_children(grandchild) == []


def test_ancestor_chain():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()

    root = inc.add_hypothesis("A", created_by="coordinator")
    child = inc.add_hypothesis("B", parent_id=root.id, created_by="coordinator")
    grandchild = inc.add_hypothesis("C", parent_id=child.id, created_by="coordinator")

    chain = grandchild.ancestor_chain(inc.hypotheses)
    assert [h.statement for h in chain] == ["C", "B", "A"]


def test_confirmed_path():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()

    root = inc.add_hypothesis("A", created_by="coordinator")
    child = inc.add_hypothesis("B", parent_id=root.id, created_by="coordinator")
    grandchild = inc.add_hypothesis("C", parent_id=child.id, created_by="coordinator")

    # None confirmed yet
    assert grandchild.confirmed_path(inc.hypotheses) is None

    root.confirm(0.8)
    child.confirm(0.9)
    grandchild.confirm(0.95)

    path = grandchild.confirmed_path(inc.hypotheses)
    assert path is not None
    assert [h.statement for h in path] == ["A", "B", "C"]


def test_prune_refuted_branches():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()

    root = inc.add_hypothesis("DB layer latency", created_by="coordinator")
    child = inc.add_hypothesis("connection pool exhausted", parent_id=root.id, created_by="coordinator")
    sibling = inc.add_hypothesis("slow query", parent_id=root.id, created_by="coordinator")

    root.refute()
    pruned = inc.prune_refuted_branches()

    assert len(pruned) == 2
    assert child.status == HypothesisStatus.REFUTED
    assert sibling.status == HypothesisStatus.REFUTED


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def test_evidence_nullable_hypothesis():
    """Exploratory evidence is not tied to any hypothesis."""
    ev = Evidence(
        source=EvidenceSource.PROMETHEUS,
        query='rate(http_request_duration_seconds_sum[5m])',
        summary="latency p95 is 800ms, up from 200ms baseline",
        collected_by="metrics_agent",
    )
    assert ev.hypothesis_id is None


def test_evidence_linked_to_hypothesis():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()
    hyp = inc.add_hypothesis("connection pool exhausted", created_by="coordinator")

    ev = Evidence(
        source=EvidenceSource.LOKI,
        query='{service="checkout"} |= "connection pool exhausted"',
        summary="21 occurrences of pool exhaustion in last 5 minutes",
        hypothesis_id=hyp.id,
        collected_by="logs_agent",
    )
    inc.add_evidence(ev)

    assert len(hyp.evidence) == 1
    assert hyp.evidence[0].source == EvidenceSource.LOKI


def test_evidence_invalid_hypothesis_raises():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()

    ev = Evidence(
        source=EvidenceSource.KUBERNETES,
        query="kubectl describe pod checkout-abc",
        summary="pod is running",
        hypothesis_id="nonexistent",
        collected_by="kubernetes_agent",
    )
    with pytest.raises(ValueError, match="not found"):
        inc.add_evidence(ev)


# ---------------------------------------------------------------------------
# Remediation sub-state-machine
# ---------------------------------------------------------------------------


def test_remediation_lifecycle():
    rem = Remediation(action="rollback deployment")

    assert rem.status == RemediationStatus.PROPOSED
    rem.request_approval()
    assert rem.status == RemediationStatus.PENDING_APPROVAL

    rem.approve("alice")
    assert rem.status == RemediationStatus.APPROVED
    assert rem.approver == "alice"

    rem.execute()
    assert rem.status == RemediationStatus.EXECUTING
    assert rem.executed_at is not None

    rem.complete_execution()
    assert rem.status == RemediationStatus.EXECUTED

    rem.verify(success=True)
    assert rem.status == RemediationStatus.VERIFIED


def test_remediation_reject():
    rem = Remediation(action="restart pod")
    rem.request_approval()
    rem.reject("bob")
    assert rem.status == RemediationStatus.REJECTED


def test_remediation_cannot_execute_without_approval():
    rem = Remediation(action="do something")
    with pytest.raises(ValueError, match="Cannot execute"):
        rem.execute()


def test_remediation_cannot_verify_without_execution():
    rem = Remediation(action="do something")
    rem.request_approval()
    rem.approve("alice")
    with pytest.raises(ValueError, match="Cannot verify"):
        rem.verify(success=True)


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


def test_topology_graph():
    checkout = Node(type=NodeType.SERVICE, name="checkout", namespace="prod", cluster_id="us-east-1")
    kafka = Node(type=NodeType.TOPIC, name="orders-topic", namespace="prod", cluster_id="us-east-1")
    postgres = Node(type=NodeType.DATABASE, name="orders-db", namespace="prod", cluster_id="us-east-1")

    edge1 = Edge(
        source_node_id=checkout.id,
        target_node_id=kafka.id,
        relationship=EdgeRelationship.PUBLISHES_TO,
        cluster_id="us-east-1",
    )
    edge2 = Edge(
        source_node_id=kafka.id,
        target_node_id=postgres.id,
        relationship=EdgeRelationship.DEPENDS_ON,
        cluster_id="us-east-1",
    )

    assert edge1.relationship == EdgeRelationship.PUBLISHES_TO
    assert edge2.relationship == EdgeRelationship.DEPENDS_ON


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def test_resolved_incident_archive():
    archived = ResolvedIncident(
        incident_id="inc-abc123",
        alert_signature={"service": "checkout", "severity": "critical"},
        resolution=Resolution.RESOLVED,
        duration_seconds=420.0,
        severity="critical",
        affected_services=["checkout", "orders-db"],
        hypothesis_tree={"root": "DB connection pool exhausted"},
        rca_summary="Recent deployment introduced a connection leak in the checkout service.",
        remediation_applied="rollback checkout deployment to v1.2.3",
        verified=True,
    )
    assert archived.resolution == Resolution.RESOLVED
    assert archived.verified is True


# ---------------------------------------------------------------------------
# RCA projection
# ---------------------------------------------------------------------------


def test_rca_report():
    inc = Incident(cluster_id="us-east-1", title="Checkout latency > 500ms")
    inc.acknowledge()

    # Build hypothesis tree
    root = inc.add_hypothesis("latency is in the database layer", created_by="coordinator")
    child = inc.add_hypothesis("DB connection pool exhausted", parent_id=root.id, created_by="coordinator")
    leaf = inc.add_hypothesis(
        "connection leak from deployment v1.2.4", parent_id=child.id, created_by="coordinator"
    )

    # Add evidence
    ev1 = Evidence(
        source=EvidenceSource.PROMETHEUS,
        query='rate(http_request_duration_seconds_sum{service="checkout"}[5m])',
        summary="p95 latency 800ms, 4x baseline",
        hypothesis_id=root.id,
        collected_by="metrics_agent",
    )
    ev2 = Evidence(
        source=EvidenceSource.LOKI,
        query='{service="checkout"} |= "connection pool exhausted"',
        summary="21 pool exhaustion errors in 5 minutes",
        hypothesis_id=child.id,
        collected_by="logs_agent",
    )
    ev3 = Evidence(
        source=EvidenceSource.DEPLOYMENT,
        query="git diff v1.2.3..v1.2.4 -- checkout/",
        summary="Removed connection pool timeout config in db.py",
        hypothesis_id=leaf.id,
        collected_by="deployment_agent",
    )
    inc.add_evidence(ev1)
    inc.add_evidence(ev2)
    inc.add_evidence(ev3)

    # Confirm the chain
    root.confirm(0.8)
    child.confirm(0.9)
    leaf.confirm(0.95)

    inc.produce_rca()
    inc.propose_remediation("rollback checkout deployment to v1.2.3")

    report = inc.rca_report()
    assert report["root_cause"] == "connection leak from deployment v1.2.4"
    assert report["confidence"] == 0.95
    assert len(report["evidence"]) == 3
    assert report["remediation"] == "rollback checkout deployment to v1.2.3"


def test_rca_report_carries_computed_signals():
    """Measured facts reach the report, not just the narrative summary.

    Without this the deterministic analysis is invisible to every consumer of the
    API — a reader cannot check the prose against the numbers behind it.
    """
    inc = Incident(cluster_id="us-east-1", title="Checkout latency > 500ms")
    inc.acknowledge()
    hyp = inc.add_hypothesis("checkout is slow", created_by="coordinator")
    inc.add_evidence(
        Evidence(
            source=EvidenceSource.PROMETHEUS,
            query="up",
            summary="Latency is elevated.",
            signals={"anomaly": True, "p95": 0.82, "spike_count": 3},
            hypothesis_id=hyp.id,
            collected_by="metrics_agent",
        )
    )
    hyp.confirm(0.9)
    inc.produce_rca()

    signals = inc.rca_report()["evidence"][0]["signals"]
    assert signals["anomaly"] is True
    assert signals["p95"] == 0.82
    assert signals["spike_count"] == 3


def test_rca_report_signals_default_to_empty():
    """Sources without deterministic analysis still serialize cleanly."""
    inc = Incident(cluster_id="us-east-1", title="Checkout latency > 500ms")
    inc.acknowledge()
    hyp = inc.add_hypothesis("a bad deploy went out", created_by="coordinator")
    inc.add_evidence(
        Evidence(
            source=EvidenceSource.DEPLOYMENT,
            query="git log -1",
            summary="v1.2.4 shipped 6 minutes before the alert.",
            hypothesis_id=hyp.id,
            collected_by="deployment_agent",
        )
    )
    hyp.confirm(0.7)
    inc.produce_rca()

    assert inc.rca_report()["evidence"][0]["signals"] == {}


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------


def test_flag_stale():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()
    assert not inc.is_stale

    inc.flag_stale()
    assert inc.is_stale
    assert inc.stale_flagged_at is not None


# ---------------------------------------------------------------------------
# LLM budget exceeded — spec: docs/specs/investigation-cost-guardrails.md
# ---------------------------------------------------------------------------


def test_flag_budget_exceeded():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()
    assert not inc.llm_budget_exceeded

    inc.flag_budget_exceeded()

    assert inc.llm_budget_exceeded
    assert inc.budget_exceeded_flagged_at is not None


def test_flag_budget_exceeded_is_distinct_from_stale():
    """A human reading the incident later should be able to tell the two
    causes apart — 'ran out of budget' is not 'took too long'."""
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()

    inc.flag_budget_exceeded()

    assert inc.llm_budget_exceeded
    assert not inc.is_stale
    assert inc.stale_flagged_at is None


def test_flag_budget_exceeded_logs_a_distinct_reason():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()

    inc.flag_budget_exceeded()

    note = inc.audit_log[-1].note
    assert "budget" in note.lower()
    assert "time threshold" not in note.lower()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_log_tracks_all_actions():
    inc = Incident(cluster_id="us-east-1", title="Test")
    inc.acknowledge()
    inc.produce_rca()
    inc.propose_remediation("restart deployment")
    inc.approve_remediation("alice")
    inc.execute_remediation()
    inc.verify_remediation(success=True)

    actions = [entry.action for entry in inc.audit_log]
    assert "acknowledged" in actions
    assert "rca_produced" in actions
    assert "remediation_proposed" in actions
    assert "approved_remediation" in actions
    assert "executed_remediation" in actions
    assert "remediation_verified" in actions


# ---------------------------------------------------------------------------
# Demo — full checkout-latency scenario from the spec
# ---------------------------------------------------------------------------


def demo():
    """Run the full checkout-latency investigation scenario from the spec.

    Alert: Checkout latency > 500ms
    Investigation: hypothesis tree → evidence gathering → RCA → remediation
    """
    print("=" * 60)
    print("DEMO: Checkout Latency Incident Investigation")
    print("=" * 60)

    # 1. Alert fires
    source = AlertSource(name="prod-alertmanager", cluster_id="us-east-1")
    alert = Alert(
        source=source,
        fingerprint="checkout-latency-high",
        labels={"service": "checkout", "namespace": "prod", "severity": "critical"},
        severity="critical",
        cluster_id="us-east-1",
    )
    print(f"\n[ALERT] {alert.labels['service']} latency > 500ms — {alert.severity}")

    # 2. Incident created
    inc = Incident(
        cluster_id="us-east-1",
        title=f"{alert.labels['service']} latency > 500ms",
    )
    inc.add_alert(alert)
    inc.acknowledge()
    print(f"[INCIDENT] {inc.id} created — status: {inc.status.value}")

    # 3. Exploratory evidence (no hypothesis yet)
    exploratory = Evidence(
        source=EvidenceSource.PROMETHEUS,
        query='rate(http_request_duration_seconds_sum{service="checkout"}[5m])',
        summary="p95 latency: 800ms (baseline: 200ms). Anomaly started at 14:32 UTC.",
        collected_by="metrics_agent",
    )
    inc.add_evidence(exploratory)
    print(f"[EVIDENCE] Exploratory: {exploratory.summary}")

    # 4. Coordinator proposes hypotheses
    root = inc.add_hypothesis("latency is caused by a downstream dependency", created_by="coordinator")
    h_db = inc.add_hypothesis("database layer is the bottleneck", parent_id=root.id, created_by="coordinator")
    h_kafka = inc.add_hypothesis("Kafka broker is slow", parent_id=root.id, created_by="coordinator")

    print(f"\n[HYPOTHESES] Proposed {len(inc.hypotheses)} hypotheses:")
    for h in inc.hypotheses:
        print(f"  - [{h.id[:8]}] {h.statement} (parent: {h.parent_id})")

    # 5. Gather evidence for each hypothesis
    # DB hypothesis evidence
    db_metrics = Evidence(
        source=EvidenceSource.PROMETHEUS,
        query='rate(pg_stat_activity_count{db="orders"}[5m])',
        summary="Active DB connections: 95/100 (near max). Connection wait time: 300ms.",
        hypothesis_id=h_db.id,
        collected_by="metrics_agent",
    )
    inc.add_evidence(db_metrics)

    db_logs = Evidence(
        source=EvidenceSource.LOKI,
        query='{service="checkout"} |= "connection pool exhausted"',
        summary="21 'connection pool exhausted' errors in last 5 minutes.",
        hypothesis_id=h_db.id,
        collected_by="logs_agent",
    )
    inc.add_evidence(db_logs)

    # Kafka hypothesis evidence (refuted)
    kafka_metrics = Evidence(
        source=EvidenceSource.PROMETHEUS,
        query='rate(kafka_consumer_fetch_rate{topic="orders"}[5m])',
        summary="Kafka consumer fetch rate: 200 msg/s (normal: 180-220). No anomaly.",
        hypothesis_id=h_kafka.id,
        collected_by="metrics_agent",
    )
    inc.add_evidence(kafka_metrics)

    # 6. Evaluate hypotheses
    root.confirm(0.8)
    h_db.confirm(0.9)
    h_kafka.refute()
    inc.prune_refuted_branches()
    print(f"\n[EVALUATION] Root confirmed (0.8), DB confirmed (0.9), Kafka refuted and pruned")

    # 7. Deepen the confirmed branch
    h_deploy = inc.add_hypothesis(
        "recent deployment caused connection leak", parent_id=h_db.id, created_by="coordinator"
    )
    deploy_evidence = Evidence(
        source=EvidenceSource.DEPLOYMENT,
        query="git diff v1.2.3..v1.2.4 -- checkout/db.py",
        summary="v1.2.4 removed connection_pool_timeout=30s from db.py. Deployed at 14:28 UTC.",
        hypothesis_id=h_deploy.id,
        collected_by="deployment_agent",
    )
    inc.add_evidence(deploy_evidence)

    k8s_evidence = Evidence(
        source=EvidenceSource.KUBERNETES,
        query="kubectl describe deployment checkout -n prod",
        summary="checkout deployment rolled out v1.2.4 at 14:28 UTC. 3/3 replicas running.",
        hypothesis_id=h_deploy.id,
        collected_by="kubernetes_agent",
    )
    inc.add_evidence(k8s_evidence)

    h_deploy.confirm(0.95)
    print(f"[EVALUATION] Deployment hypothesis confirmed (0.95)")

    # 8. Produce RCA
    inc.produce_rca()
    report = inc.rca_report()
    print(f"\n[RCA] Root cause: {report['root_cause']}")
    print(f"[RCA] Confidence: {report['confidence']}")
    print(f"[RCA] Causal chain: {' → '.join(report['causal_chain'])}")
    print(f"[RCA] Evidence collected: {len(report['evidence'])} items")

    # 9. Propose and approve remediation
    inc.propose_remediation("rollback checkout deployment to v1.2.3")
    print(f"\n[REMEDIATION] Proposed: {inc.remediation.action}")

    inc.approve_remediation("alice")
    inc.execute_remediation()
    print(f"[REMEDIATION] Approved by alice, executing...")

    inc.verify_remediation(success=True)
    print(f"[REMEDIATION] Verified — incident resolved!")

    # 10. Archive to memory
    resolved = ResolvedIncident(
        incident_id=inc.id,
        alert_signature=alert.labels,
        resolution=Resolution.RESOLVED,
        duration_seconds=(inc.updated_at - inc.created_at).total_seconds(),
        severity=alert.severity,
        affected_services=["checkout", "orders-db"],
        hypothesis_tree={
            "confirmed_path": report["causal_chain"],
            "confidence": report["confidence"],
        },
        rca_summary=report["root_cause"],
        remediation_applied=inc.remediation.action,
        verified=True,
    )
    print(f"\n[MEMORY] Incident archived: {resolved.incident_id}")
    print(f"[MEMORY] Resolution: {resolved.resolution.value}, Duration: {resolved.duration_seconds:.0f}s")

    print(f"\n{'=' * 60}")
    print("DEMO COMPLETE")
    print(f"{'=' * 60}")

    # Assertions — demo is also a test
    assert inc.status == IncidentStatus.RESOLVED
    assert report["confidence"] == 0.95
    assert report["root_cause"] == "recent deployment caused connection leak"
    assert len(report["evidence"]) == 4  # DB metrics, DB logs, deploy evidence, K8s evidence
    assert resolved.resolution == Resolution.RESOLVED
    assert resolved.verified is True

    return inc, report, resolved


if __name__ == "__main__":
    demo()