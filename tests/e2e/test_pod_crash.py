"""E2E: Pod crash → agent investigation → RCA.

Scenario:
    1. Delete a pod in the OTEL demo namespace
    2. Wait for the PodRestarting Prometheus alert to fire
    3. Alertmanager sends the alert to the agent
    4. Agent investigates and produces an RCA
    5. Assert the RCA has a root cause and evidence
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.helpers import (
    AGENT_URL,
    DEMO_NAMESPACE,
    assert_rca,
    kubectl,
    list_incident_ids,
    wait_for_alert,
    wait_for_incident,
)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pod_crash_triggers_investigation(cluster_ready):
    """Pod crash → agent detects restart → investigates → produces RCA."""
    pod_name = _get_first_pod("ad")
    if not pod_name:
        pytest.skip("No ad pod found — is the OTEL demo deployed?")

    before = list_incident_ids()
    kubectl("delete", "pod", "-n", DEMO_NAMESPACE, pod_name)

    alert = await wait_for_alert("PodRestarting")
    assert alert["labels"].get("severity") in ("warning", "critical")

    incident = await wait_for_incident(exclude_ids=before)
    assert_rca(incident)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_agent_health_after_fault(cluster_ready):
    """Agent stays healthy after processing a real incident."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{AGENT_URL}/health", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


def _get_first_pod(service: str) -> str | None:
    try:
        return kubectl(
            "get", "pods", "-n", DEMO_NAMESPACE,
            "-l", f"app={service}",
            "-o", "jsonpath={.items[0].metadata.name}",
        )
    except RuntimeError:
        return None
