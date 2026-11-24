"""E2E: the deterministic analysis survives contact with real telemetry.

Every other e2e test asserts that an investigation completed. This one asserts that
the numbers in it were computed rather than described — that analysis.py parsed a
real Prometheus/Loki/Tempo payload instead of falling back to a raw dump.

The fallback is silent by design (an unfamiliar payload shape degrades to the old
truncation behaviour rather than failing the investigation), which is exactly why it
needs a test that only real payloads can satisfy.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.helpers import (
    AGENT_URL,
    list_incident_ids,
    set_flag,
    wait_for_alert,
    wait_for_incident,
)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_signals_are_computed_from_real_payloads(cluster_ready):
    """At least one evidence item carries computed facts, and they survive the API."""
    before = list_incident_ids()
    set_flag("cartFailure", "100%")
    try:
        await wait_for_alert("HighErrorRate", service="cart")
        incident = await wait_for_incident(exclude_ids=before)

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{AGENT_URL}/incidents/{incident['id']}", timeout=10)
            resp.raise_for_status()
            evidence = resp.json()["rca"]["evidence"]

        with_signals = [e for e in evidence if e.get("signals")]
        assert with_signals, (
            "No evidence item carried computed signals. Either the analysis fell back "
            "to truncation on every payload, or no metrics/logs/traces agent "
            "contributed to the confirmed path. Sources present: "
            f"{sorted({e['source'] for e in evidence})}"
        )

        # Scalars only — the field has to round-trip through JSON unchanged.
        for item in with_signals:
            for key, value in item["signals"].items():
                assert isinstance(value, (str, int, float, bool)), (
                    f"signal {key!r} serialized as {type(value).__name__}"
                )
    finally:
        set_flag("cartFailure", "off")
