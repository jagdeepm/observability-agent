"""Shared helpers for e2e tests — not a conftest, importable as a module."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import Any

import httpx

AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8080")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9091")
DEMO_NAMESPACE = "otel-demo"
AGENT_NAMESPACE = "observability-agent"
E2E_TIMEOUT = int(os.getenv("E2E_TIMEOUT", "300"))
POLL_INTERVAL = 5

logger = logging.getLogger(__name__)


def kubectl(*args: str) -> str:
    cmd = ["kubectl", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def list_incident_ids() -> set[str]:
    """Return the set of current incident IDs (sync, for test setup snapshots)."""
    try:
        with httpx.Client() as client:
            resp = client.get(f"{AGENT_URL}/incidents", timeout=10)
            resp.raise_for_status()
            return {inc["id"] for inc in resp.json()}
    except Exception:
        return set()


async def wait_for_alert(alert_name: str, timeout: int = E2E_TIMEOUT) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts", timeout=10)
                resp.raise_for_status()
                last_error = None
                for alert in resp.json().get("data", {}).get("alerts", []):
                    if (
                        alert.get("labels", {}).get("alertname") == alert_name
                        and alert.get("state") == "firing"
                    ):
                        return alert
            except Exception as exc:
                last_error = exc
                logger.debug("wait_for_alert polling error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)
    msg = f"Alert {alert_name!r} did not fire within {timeout}s"
    if last_error:
        msg += f" (last error: {last_error})"
    raise TimeoutError(msg)


async def wait_for_incident(
    *,
    exclude_ids: set[str] | None = None,
    title_contains: str | None = None,
    min_hypotheses: int = 1,
    timeout: int = E2E_TIMEOUT,
) -> dict[str, Any]:
    """Poll until an incident (not in exclude_ids) has a completed RCA.

    Pass exclude_ids=list_incident_ids() before injecting the fault to ensure
    you get the incident created by the current test, not a pre-existing one.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{AGENT_URL}/incidents", timeout=10)
                resp.raise_for_status()
                last_error = None
                for summary in resp.json():
                    if exclude_ids and summary["id"] in exclude_ids:
                        continue
                    if title_contains and title_contains not in summary.get("title", ""):
                        continue
                    detail = (
                        await client.get(f"{AGENT_URL}/incidents/{summary['id']}", timeout=10)
                    )
                    detail.raise_for_status()
                    inc = detail.json()
                    rca = inc.get("rca")
                    if (
                        rca
                        and rca.get("root_cause")
                        and inc.get("hypothesis_count", 0) >= min_hypotheses
                    ):
                        return inc
            except Exception as exc:
                last_error = exc
                logger.debug("wait_for_incident polling error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)
    msg = f"No matching incident with RCA found within {timeout}s"
    if last_error:
        msg += f" (last error: {last_error})"
    raise TimeoutError(msg)


def cleanup_incidents() -> None:
    """Resolve any active incidents (sync, safe to call from fixtures)."""
    try:
        with httpx.Client() as client:
            resp = client.get(f"{AGENT_URL}/incidents", timeout=5)
            resp.raise_for_status()
            for inc in resp.json() or []:
                try:
                    client.post(
                        f"{AGENT_URL}/incidents/{inc['id']}/resolve", timeout=5
                    )
                except Exception:
                    pass
    except Exception:
        pass


def assert_rca(incident: dict[str, Any]) -> None:
    """Common RCA shape assertions, reusable across all test files."""
    rca = incident.get("rca")
    assert rca is not None, "Agent must produce an RCA"
    assert rca.get("root_cause"), "RCA must have a non-empty root_cause"
    assert rca.get("confidence", 0) > 0, "RCA confidence must be > 0"
    assert len(rca.get("evidence", [])) > 0, "RCA must include at least one evidence item"
    assert incident.get("hypothesis_count", 0) >= 1, "Investigation must evaluate at least one hypothesis"
    terminal_or_approval = {"pending_approval", "rca_produced", "resolved", "closed"}
    assert incident.get("status") in terminal_or_approval, (
        f"Expected a post-RCA status, got {incident.get('status')!r}"
    )


def check_cluster_ready() -> None:
    """Verify cluster is up. Raises RuntimeError with diagnostics if not ready."""
    import shutil
    import urllib.request

    kind_path = shutil.which("kind")
    if not kind_path:
        raise RuntimeError("kind not found in PATH. Install: brew install kind")

    result = subprocess.run(
        ["kind", "get", "clusters"], capture_output=True, text=True
    )
    clusters = result.stdout.strip()
    if "observability-e2e" not in clusters:
        raise RuntimeError(
            f"Kind cluster 'observability-e2e' not found. "
            f"Existing clusters: {clusters or '(none)'}. "
            f"Run: task cluster"
        )

    ctx = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True
    ).stdout.strip()
    if "observability-e2e" not in ctx:
        raise RuntimeError(
            f"kubectl context is '{ctx}', expected 'kind-observability-e2e'. "
            f"Run: kubectl config use-context kind-observability-e2e"
        )

    try:
        urllib.request.urlopen(f"{AGENT_URL}/health", timeout=5)
    except Exception as e:
        raise RuntimeError(
            f"Agent not reachable at {AGENT_URL}. Run: task port-forward"
        ) from e
