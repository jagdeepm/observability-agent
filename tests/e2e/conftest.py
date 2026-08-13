"""E2E test fixtures — shared setup for running the agent against the OTEL demo.

Prerequisites:
    - Kind cluster running with monitoring stack + OTEL demo + agent deployed
    - Run `task e2e-setup` or `task all` from the Taskfile first.

These tests:
    1. Trigger a fault in the demo app
    2. Wait for Prometheus to fire an alert
    3. Wait for the agent to investigate and produce an RCA
    4. Assert the RCA has reasonable structure and confidence
"""

from __future__ import annotations

import pytest

from tests.e2e.helpers import check_cluster_ready, cleanup_incidents


@pytest.fixture(scope="session")
def cluster_ready():
    """Verify the cluster is up and all required components are reachable."""
    try:
        check_cluster_ready()
    except RuntimeError as e:
        pytest.skip(str(e))
    yield


@pytest.fixture(autouse=True)
def isolate_incidents(cluster_ready):
    """Clean up leftover incidents before and after each test."""
    cleanup_incidents()
    yield
    cleanup_incidents()
