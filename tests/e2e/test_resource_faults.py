"""E2E: resource exhaustion and crashes → investigation → RCA.

Non-HTTP fault classes. These alert off kube-state-metrics and cAdvisor rather than
the demo's own instrumentation, so they also prove the KubernetesAgent has something
to say when the HTTP metrics are unremarkable.
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
    set_flag,
    wait_for_alert,
    wait_for_incident,
)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_ad_high_cpu_triggers_investigation(cluster_ready):
    """Ad service burns CPU → HighCPU alert → agent investigates."""
    before = list_incident_ids()
    set_flag("adHighCpu", "on")
    try:
        await wait_for_alert("HighCPU")
        incident = await wait_for_incident(exclude_ids=before)
        assert_rca(incident)
    finally:
        set_flag("adHighCpu", "off")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_email_memory_leak_triggers_restart(cluster_ready):
    """Email service leaks until the kubelet OOM-kills it → PodRestarting."""
    before = list_incident_ids()
    set_flag("emailMemoryLeak", "1000x")
    try:
        await wait_for_alert("PodRestarting")
        incident = await wait_for_incident(exclude_ids=before)
        assert_rca(incident)
    finally:
        set_flag("emailMemoryLeak", "off")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pod_crash_triggers_investigation(cluster_ready):
    """Deleting a pod produces a restart the agent should detect."""
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
        assert resp.json()["status"] == "ok"


def _get_first_pod(component: str) -> str | None:
    """Find a demo pod by component. The chart labels pods with
    app.kubernetes.io/name, not `app` — a stale selector here silently skips."""
    try:
        return kubectl(
            "get", "pods", "-n", DEMO_NAMESPACE,
            "-l", f"app.kubernetes.io/name={component}",
            "-o", "jsonpath={.items[0].metadata.name}",
        )
    except RuntimeError:
        return None
