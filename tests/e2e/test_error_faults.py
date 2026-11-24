"""E2E: services returning 5xx → HighErrorRate alert → investigation → RCA.

Each test flips one of the demo's own failure flags, so the fault is real: the
service genuinely returns errors, genuinely logs them, and genuinely shows up in
traces. Nothing here is simulated by the harness.

The `expect_signals` assertions are the point of running against a live stack —
they check that the error counts in the Evidence were computed from the Loki
response by analysis.py, not paraphrased by the LLM.
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
async def test_cart_failure_triggers_investigation(cluster_ready):
    """Cart service fails every request → high error rate → agent produces an RCA."""
    before = list_incident_ids()
    set_flag("cartFailure", "100%")
    try:
        alert = await wait_for_alert("HighErrorRate", service="cart")
        assert alert["labels"].get("severity") in ("warning", "critical")

        incident = await wait_for_incident(exclude_ids=before)
        assert_rca(incident, expect_signals={"error_count"})
    finally:
        set_flag("cartFailure", "off")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_payment_failure_triggers_investigation(cluster_ready):
    """Payment charge failures surface as checkout errors — the fault is one hop
    away from the service that alerts, so the RCA has a causal chain to find."""
    before = list_incident_ids()
    set_flag("paymentFailure", "100%")
    try:
        await wait_for_alert("HighErrorRate", service="payment")
        incident = await wait_for_incident(exclude_ids=before)
        assert_rca(incident)
        assert len(incident["rca"]["causal_chain"]) >= 1
    finally:
        set_flag("paymentFailure", "off")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_product_catalog_failure_triggers_investigation(cluster_ready):
    """Product catalog fails on one product → errors propagate to the frontend."""
    before = list_incident_ids()
    set_flag("productCatalogFailure", "on")
    try:
        await wait_for_alert("HighErrorRate", service="product-catalog")
        incident = await wait_for_incident(exclude_ids=before)
        assert_rca(incident, expect_signals={"error_count"})
    finally:
        set_flag("productCatalogFailure", "off")
