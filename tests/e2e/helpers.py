"""Shared helpers for e2e tests — not a conftest, importable as a module."""

from __future__ import annotations

import asyncio
import json
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

FLAGD_CONFIGMAP = "flagd-config"
FLAGD_DEPLOYMENT = "flagd"
FLAGD_KEY = "demo.flagd.json"
# jsonpath treats "." as a path separator, so the dots in the key are escaped.
FLAGD_JSONPATH = r"{.data.demo\.flagd\.json}"

logger = logging.getLogger(__name__)


def kubectl(*args: str) -> str:
    cmd = ["kubectl", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Fault injection via flagd feature flags
#
# The demo's own fault switches, so the failures are real rather than simulated.
#
# The flag lives in the flagd-config ConfigMap, but flagd does not read it from
# there: an initContainer copies the file into an emptyDir at pod start, and flagd
# watches that copy. Patching the ConfigMap alone is therefore invisible to the
# running pod — every write has to be followed by a rollout. Roughly 15s per flag
# change, which is noise next to the 1m `for:` clause on the alert rules.
# ---------------------------------------------------------------------------


def _read_flagd_config() -> dict[str, Any]:
    raw = kubectl(
        "get", "configmap", FLAGD_CONFIGMAP,
        "--namespace", DEMO_NAMESPACE,
        "-o", f"jsonpath={FLAGD_JSONPATH}",
    )
    if not raw:
        raise RuntimeError(
            f"ConfigMap {FLAGD_CONFIGMAP} in {DEMO_NAMESPACE} has no {FLAGD_KEY} key. "
            "Is the OTEL demo deployed? Run: task demo"
        )
    return json.loads(raw)


def _write_flagd_config(config: dict[str, Any]) -> None:
    patch = json.dumps({"data": {FLAGD_KEY: json.dumps(config)}})
    kubectl(
        "patch", "configmap", FLAGD_CONFIGMAP,
        "--namespace", DEMO_NAMESPACE,
        "--type", "merge",
        "-p", patch,
    )
    # Without this the patch is inert: flagd is serving the emptyDir copy its
    # initContainer made when the pod started.
    kubectl("rollout", "restart", f"deployment/{FLAGD_DEPLOYMENT}", "--namespace", DEMO_NAMESPACE)
    kubectl(
        "rollout", "status", f"deployment/{FLAGD_DEPLOYMENT}",
        "--namespace", DEMO_NAMESPACE, "--timeout=120s",
    )


def list_flags() -> dict[str, str]:
    """Map every flag in the deployed demo to its current variant."""
    flags = _read_flagd_config()["flags"]
    return {name: spec.get("defaultVariant", "") for name, spec in flags.items()}


def set_flag(flag_name: str, variant: str) -> None:
    """Set a flagd feature flag to a variant.

    Raises with the available names rather than a bare KeyError — the demo renames
    flags between majors, and a rename should read as "this flag is gone", not as a
    mystery failure deep in a test.
    """
    config = _read_flagd_config()
    flags = config["flags"]
    if flag_name not in flags:
        raise KeyError(
            f"No flagd flag named {flag_name!r}. Available: {', '.join(sorted(flags))}"
        )
    variants = flags[flag_name].get("variants", {})
    if variant not in variants:
        raise KeyError(
            f"Flag {flag_name!r} has no variant {variant!r}. "
            f"Available: {', '.join(sorted(variants))}"
        )
    flags[flag_name]["defaultVariant"] = variant
    _write_flagd_config(config)
    logger.info("flagd: %s = %s", flag_name, variant)


# Flags whose healthy baseline is not "off". Everything else in the demo is a fault
# switch that defaults off; these two drive the synthetic traffic the whole harness
# depends on, so resetting them to "off" would silence the telemetry entirely.
BASELINE_VARIANTS = {
    "loadGeneratorTraffic": "on",
    "loadGeneratorVUs": "5",
}


def reset_flags() -> None:
    """Return every flag to its shipped default, so a crashed test cannot leave the
    demo broken for the next one."""
    try:
        config = _read_flagd_config()
    except RuntimeError:
        return
    changed = False
    for name, spec in config["flags"].items():
        target = BASELINE_VARIANTS.get(name, "off")
        if target not in spec.get("variants", {}):
            continue  # a flag added upstream that we know nothing about
        if spec.get("defaultVariant") != target:
            spec["defaultVariant"] = target
            changed = True
            logger.info("flagd: reset %s -> %s", name, target)
    if changed:
        _write_flagd_config(config)


def list_incident_ids() -> set[str]:
    """Return the set of current incident IDs (sync, for test setup snapshots)."""
    try:
        with httpx.Client() as client:
            resp = client.get(f"{AGENT_URL}/incidents", timeout=10)
            resp.raise_for_status()
            return {inc["id"] for inc in resp.json()}
    except Exception:
        return set()


async def wait_for_alert(
    alert_name: str,
    *,
    service: str | None = None,
    timeout: int = E2E_TIMEOUT,
) -> dict[str, Any]:
    """Wait for an alert to fire, optionally for one specific service.

    Pass `service` whenever the test injected a fault into a known service. The
    demo runs hot on a local cluster and some alerts stand permanently — an
    unfiltered wait returns one of those immediately and the test passes without
    its fault ever having mattered.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    seen: set[str] = set()
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts", timeout=10)
                resp.raise_for_status()
                last_error = None
                for alert in resp.json().get("data", {}).get("alerts", []):
                    labels = alert.get("labels", {})
                    if labels.get("alertname") != alert_name:
                        continue
                    seen.add(f"{labels.get('service', '?')}:{alert.get('state')}")
                    if alert.get("state") != "firing":
                        continue
                    if service and labels.get("service") != service:
                        continue
                    return alert
            except Exception as exc:
                last_error = exc
                logger.debug("wait_for_alert polling error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)
    target = f"{alert_name!r}" + (f" for service {service!r}" if service else "")
    msg = f"Alert {target} did not fire within {timeout}s"
    if seen:
        msg += f" (saw {alert_name} as: {', '.join(sorted(seen))})"
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


def assert_rca(
    incident: dict[str, Any],
    *,
    expect_signals: set[str] | None = None,
) -> None:
    """Common RCA shape assertions, reusable across all test files.

    expect_signals names computed facts (analysis.py) that must appear on at least
    one evidence item. Asserting on those rather than on the narrative is what
    distinguishes "the agent measured this" from "the LLM wrote a plausible
    sentence" — which is the whole point of running against real telemetry.
    """
    rca = incident.get("rca")
    assert rca is not None, "Agent must produce an RCA"
    assert rca.get("root_cause"), "RCA must have a non-empty root_cause"
    assert rca.get("confidence", 0) > 0, "RCA confidence must be > 0"
    evidence = rca.get("evidence", [])
    assert len(evidence) > 0, "RCA must include at least one evidence item"
    assert incident.get("hypothesis_count", 0) >= 1, "Investigation must evaluate at least one hypothesis"
    terminal_or_approval = {"pending_approval", "rca_produced", "resolved", "closed"}
    assert incident.get("status") in terminal_or_approval, (
        f"Expected a post-RCA status, got {incident.get('status')!r}"
    )

    if expect_signals:
        seen: set[str] = set()
        for item in evidence:
            seen.update(item.get("signals") or {})
        missing = expect_signals - seen
        assert not missing, (
            f"Expected computed signals {sorted(missing)} on the RCA evidence, "
            f"got {sorted(seen) or 'none'}. Sources: "
            f"{sorted({e.get('source') for e in evidence})}"
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

    # Without demo metrics in Prometheus the latency and error rules can never
    # fire, and every scenario fails as a five-minute timeout that looks like an
    # agent bug. Fail fast and point at the real problem instead.
    try:
        with urllib.request.urlopen(
            f"{PROMETHEUS_URL}/api/v1/label/service_name/values", timeout=10
        ) as resp:
            services = json.load(resp).get("data") or []
    except Exception as e:
        raise RuntimeError(f"Prometheus not reachable at {PROMETHEUS_URL}") from e
    if not services:
        raise RuntimeError(
            "Prometheus has no service_name label values — the demo's collector is "
            "not exporting into kube-prometheus-stack. Run: task telemetry-check"
        )
