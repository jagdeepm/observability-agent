"""E2E test fixtures — shared setup for running the agent against the OTEL demo.

Prerequisites:
    - Kind cluster running with monitoring stack + Loki + Tempo + OTEL demo + agent
    - Run `task e2e-setup` or `task all` from the Taskfile first.
    - `task telemetry-check` should pass before running these.

These tests:
    1. Trigger a fault in the demo app via its flagd feature flags
    2. Wait for Prometheus to fire an alert
    3. Wait for the agent to investigate and produce an RCA
    4. Assert the RCA has reasonable structure, confidence, and computed signals
"""

from __future__ import annotations

import pytest

from tests.e2e.helpers import check_cluster_ready, cleanup_incidents, reset_flags


@pytest.fixture(scope="session")
def cluster_ready():
    """Verify the cluster is up and all required components are reachable."""
    try:
        check_cluster_ready()
    except RuntimeError as e:
        pytest.skip(str(e))
    yield
    reset_flags()


@pytest.fixture(autouse=True)
def isolate_incidents(cluster_ready):
    """Clean up leftover incidents and faults before and after each test.

    Resetting flags up front matters as much as tearing them down: a test killed
    mid-run (timeout, Ctrl-C) leaves its fault switched on, and the next test then
    investigates the wrong failure.
    """
    reset_flags()
    cleanup_incidents()
    yield
    reset_flags()
    cleanup_incidents()
