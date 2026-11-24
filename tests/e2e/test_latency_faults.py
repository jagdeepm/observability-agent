"""E2E: slow services → HighLatency alert → investigation → RCA.

These exercise the two halves of the deterministic analysis that only real data can
test: distribution statistics over a Prometheus range response, and span-tree self
time over a Tempo search response.
"""

from __future__ import annotations

import pytest

from tests.e2e.helpers import (
    assert_rca,
    list_incident_ids,
    set_flag,
    wait_for_alert,
    wait_for_incident,
)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_image_slow_load_triggers_investigation(cluster_ready):
    """Frontend image loads stall for 10s → P99 latency breaches the threshold."""
    before = list_incident_ids()
    set_flag("imageSlowLoad", "10sec")
    try:
        alert = await wait_for_alert("HighLatency", service="frontend")
        assert alert["labels"].get("severity") in ("warning", "critical")

        incident = await wait_for_incident(exclude_ids=before)
        assert_rca(incident, expect_signals={"p95"})
    finally:
        set_flag("imageSlowLoad", "off")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_shipping_slowdown_surfaces_slowest_span(cluster_ready):
    """International shipping stalls inside the checkout call chain.

    The delay is in a child span, so the slowest *total* duration is still the
    checkout root. Only self time identifies shipping — this is the scenario that
    would have passed while the analysis was silently wrong.
    """
    before = list_incident_ids()
    set_flag("intlShippingSlowdown", "10sec")
    try:
        await wait_for_alert("HighLatency", service="checkout")
        incident = await wait_for_incident(exclude_ids=before)
        assert_rca(incident, expect_signals={"slowest_self_ms"})
    finally:
        set_flag("intlShippingSlowdown", "off")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_load_spike_with_cart_errors(cluster_ready):
    """A load spike alongside partial cart failures — two faults at once, so the
    agent has to pick a root cause rather than restate the alert."""
    before = list_incident_ids()
    set_flag("loadGeneratorVUs", "50")
    set_flag("cartFailure", "50%")
    try:
        await wait_for_alert("HighLatency", service="frontend")
        incident = await wait_for_incident(exclude_ids=before)
        assert_rca(incident)
    finally:
        set_flag("cartFailure", "off")
        set_flag("loadGeneratorVUs", "5")
