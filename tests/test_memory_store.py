"""Tests for the Memory Store — archiving and similarity search."""

from __future__ import annotations

from observability_agent.domain.models import (
    Alert,
    AlertSource,
    Evidence,
    EvidenceSource,
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentStatus,
    Resolution,
)
from observability_agent.memory.store import MemoryStore


def make_resolved_incident(title: str, rca_statement: str) -> Incident:
    """Create a resolved incident for testing."""
    source = AlertSource(name="test", cluster_id="test")
    alert = Alert(
        source=source,
        fingerprint="test",
        labels={"service": "checkout", "severity": "critical"},
        cluster_id="test",
    )
    inc = Incident(cluster_id="test", title=title)
    inc.add_alert(alert)
    inc.acknowledge()

    hyp = inc.add_hypothesis(rca_statement, created_by="coordinator")
    hyp.confirm(0.9)
    ev = Evidence(
        source=EvidenceSource.PROMETHEUS,
        query="test",
        summary="test evidence",
        hypothesis_id=hyp.id,
        collected_by="metrics",
    )
    inc.add_evidence(ev)

    inc.produce_rca()
    inc.propose_remediation("restart deployment")
    inc.close("test-user")
    return inc


def test_archive_incident():
    inc = make_resolved_incident("test", "database connection pool exhausted")
    store = MemoryStore()

    archived = store.archive(inc, Resolution.CLOSED)
    assert archived.incident_id == inc.id
    assert archived.rca_summary == "database connection pool exhausted"
    assert archived.resolution == Resolution.CLOSED
    assert archived.verified is False


def test_retrieve_archived():
    inc = make_resolved_incident("test", "cpu throttling")
    store = MemoryStore()
    store.archive(inc, Resolution.RESOLVED)

    retrieved = store.get(inc.id)
    assert retrieved is not None
    assert retrieved.incident_id == inc.id


def test_search_similar():
    store = MemoryStore()

    inc1 = make_resolved_incident("inc1", "database connection pool exhausted")
    inc2 = make_resolved_incident("inc2", "cpu throttling due to node pressure")
    inc3 = make_resolved_incident("inc3", "database query timeout from missing index")

    store.archive(inc1, Resolution.RESOLVED)
    store.archive(inc2, Resolution.RESOLVED)
    store.archive(inc3, Resolution.RESOLVED)

    results = store.search("database connection pool")
    assert len(results) > 0
    # inc1 should be the top result (exact match on "database connection pool")
    assert results[0]["incident_id"] == inc1.id


def test_search_no_match():
    store = MemoryStore()
    inc = make_resolved_incident("test", "database issue")
    store.archive(inc, Resolution.RESOLVED)

    results = store.search("xyzzy nonexistent")
    assert len(results) == 0


def test_list_all():
    store = MemoryStore()
    inc1 = make_resolved_incident("a", "issue a")
    inc2 = make_resolved_incident("b", "issue b")
    store.archive(inc1, Resolution.RESOLVED)
    store.archive(inc2, Resolution.CLOSED)

    all_incidents = store.list_all()
    assert len(all_incidents) == 2