"""Tests for the Alert Receiver — webhook parsing and alert grouping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from observability_agent.api.alert_receiver import create_app
from observability_agent.domain.models import Incident, IncidentStatus
from observability_agent.infrastructure.repository import InMemoryIncidentRepository


def _alertmanager_payload(
    labels: dict | None = None,
    status: str = "firing",
    fingerprint: str = "abc123",
) -> dict:
    return {
        "receiver": "webhook",
        "status": status,
        "alerts": [
            {
                "status": status,
                "labels": labels or {
                    "service": "checkout",
                    "namespace": "prod",
                    "severity": "critical",
                    "alertname": "HighLatency",
                },
                "annotations": {"summary": "Checkout latency > 500ms"},
                "fingerprint": fingerprint,
                "startsAt": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }


def test_health_endpoint():
    repo = InMemoryIncidentRepository()
    app = create_app(repo)
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_receive_alert_creates_incident():
    repo = InMemoryIncidentRepository()
    app = create_app(repo)
    client = TestClient(app)

    resp = client.post("/alerts", json=_alertmanager_payload())
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert len(data["incidents"]) == 1
    assert data["incidents"][0]["created"] is True


def test_receive_alert_links_to_existing_incident():
    repo = InMemoryIncidentRepository()
    app = create_app(repo)
    client = TestClient(app)

    # First alert
    client.post("/alerts", json=_alertmanager_payload(
        labels={"service": "checkout", "namespace": "prod", "severity": "critical", "alertname": "HighLatency"},
    ))

    # Second alert — same service, same namespace → should group
    resp = client.post("/alerts", json=_alertmanager_payload(
        labels={"service": "checkout", "namespace": "prod", "severity": "warning", "alertname": "HighErrorRate"},
        fingerprint="def456",
    ))
    assert resp.status_code == 202
    data = resp.json()
    assert data["incidents"][0]["created"] is False


def test_alert_with_different_service_creates_new_incident():
    repo = InMemoryIncidentRepository()
    app = create_app(repo)
    client = TestClient(app)

    # First alert for checkout
    client.post("/alerts", json=_alertmanager_payload(
        labels={"service": "checkout", "namespace": "prod", "severity": "critical", "alertname": "HighLatency"},
    ))

    # Second alert for cart — different service, different alertname, only 1 overlap (namespace) → new incident
    resp = client.post("/alerts", json=_alertmanager_payload(
        labels={"service": "cart", "namespace": "prod", "severity": "warning", "alertname": "HighErrorRate"},
        fingerprint="ghi789",
    ))
    assert resp.status_code == 202
    data = resp.json()
    assert data["incidents"][0]["created"] is True


def test_list_incidents():
    repo = InMemoryIncidentRepository()
    app = create_app(repo)
    client = TestClient(app)

    client.post("/alerts", json=_alertmanager_payload(
        labels={"service": "checkout", "namespace": "prod", "severity": "critical", "alertname": "HighLatency"},
    ))

    resp = client.get("/incidents")
    assert resp.status_code == 200
    incidents = resp.json()
    assert len(incidents) == 1
    assert incidents[0]["title"] == "checkout: HighLatency"


def test_get_incident_not_found():
    repo = InMemoryIncidentRepository()
    app = create_app(repo)
    client = TestClient(app)

    resp = client.get("/incidents/nonexistent")
    assert resp.status_code == 404


def test_invalid_json_returns_400():
    repo = InMemoryIncidentRepository()
    app = create_app(repo)
    client = TestClient(app)

    resp = client.post("/alerts", content="not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_empty_alerts_returns_400():
    repo = InMemoryIncidentRepository()
    app = create_app(repo)
    client = TestClient(app)

    resp = client.post("/alerts", json={"alerts": []})
    assert resp.status_code == 400