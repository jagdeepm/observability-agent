"""E2E: Error rate fault → agent investigation → RCA.

Scenario:
    1. Enable cartFailure flagd feature flag (cart service returns HTTP 500s)
    2. Wait for the HighErrorRate Prometheus alert to fire
    3. Alertmanager sends the alert to the agent
    4. Agent investigates and produces an RCA
    5. Assert the RCA has a root cause, confidence > 0, and evidence

Prerequisite: OTEL demo metrics must reach kube-prometheus-stack Prometheus.
    See test_latency_fault.py for setup instructions.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.helpers import (
    DEMO_NAMESPACE,
    assert_rca,
    kubectl,
    list_incident_ids,
    wait_for_alert,
    wait_for_incident,
)

_FLAGD_CONFIGMAP = "flagd-config"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_error_rate_fault_triggers_investigation(cluster_ready):
    """Cart service failure → high error rate → agent investigates → produces RCA."""
    before = list_incident_ids()
    _set_flag("cartFailure", "on")
    try:
        alert = await wait_for_alert("HighErrorRate")
        assert alert["labels"].get("severity") in ("warning", "critical")

        incident = await wait_for_incident(exclude_ids=before)
        assert_rca(incident)
    finally:
        _set_flag("cartFailure", "off")


# ---------------------------------------------------------------------------
# Fault injection via flagd feature flags
# ---------------------------------------------------------------------------

def _set_flag(flag_name: str, variant: str) -> None:
    """Flip a flagd feature flag by patching the demo.flagd.json ConfigMap."""
    raw = kubectl(
        "get", "configmap", _FLAGD_CONFIGMAP,
        "--namespace", DEMO_NAMESPACE,
        "-o", "jsonpath={.data.demo\\.flagd\\.json}",
    )
    config = json.loads(raw)
    config["flags"][flag_name]["defaultVariant"] = variant
    patched = json.dumps({"data": {"demo.flagd.json": json.dumps(config)}})
    kubectl(
        "patch", "configmap", _FLAGD_CONFIGMAP,
        "--namespace", DEMO_NAMESPACE,
        "--type", "merge",
        "-p", patched,
    )
