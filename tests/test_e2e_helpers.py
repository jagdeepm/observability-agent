"""Unit tests for the e2e flag helpers — they run without a cluster.

`task inject-fault` and `task reset-faults` call these directly, so a break here
takes out fault injection from both the test suite and the command line. kubectl is
the only external dependency, and it is stubbed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.e2e import helpers

FLAGS = {
    "flags": {
        "cartFailure": {
            "defaultVariant": "off",
            "variants": {"off": 0, "50%": 0.5, "100%": 1.0},
        },
        "loadGeneratorTraffic": {"defaultVariant": "on", "variants": {"off": 0, "on": 1}},
        "loadGeneratorVUs": {"defaultVariant": "5", "variants": {"5": 5, "50": 50}},
    }
}


@pytest.fixture
def flagd(monkeypatch):
    """Stub kubectl with an in-memory flagd ConfigMap, recording every call."""
    state: dict[str, Any] = {
        "config": json.loads(json.dumps(FLAGS)),
        "calls": [],
    }

    def fake_kubectl(*args: str) -> str:
        state["calls"].append(args)
        if args[0] == "get":
            return json.dumps(state["config"])
        if args[0] == "patch":
            patch = json.loads(args[args.index("-p") + 1])
            state["config"] = json.loads(patch["data"][helpers.FLAGD_KEY])
            return ""
        if args[0] == "rollout":
            return ""
        raise AssertionError(f"unexpected kubectl call: {args}")

    monkeypatch.setattr(helpers, "kubectl", fake_kubectl)
    return state


def test_set_flag_writes_the_variant(flagd):
    helpers.set_flag("cartFailure", "100%")

    assert flagd["config"]["flags"]["cartFailure"]["defaultVariant"] == "100%"


def test_set_flag_drops_targeting_that_would_override_the_variant(flagd):
    flagd["config"]["flags"]["cartFailure"]["targeting"] = {"if": [True, "off", "off"]}

    helpers.set_flag("cartFailure", "100%")

    assert "targeting" not in flagd["config"]["flags"]["cartFailure"]


def test_set_flag_restarts_flagd(flagd):
    """flagd serves an emptyDir copy that its initContainer makes at pod start, so
    a ConfigMap patch is invisible until the pod restarts. Without the rollout the
    fault is never injected and the test times out waiting for an alert that has
    no reason to fire."""
    helpers.set_flag("cartFailure", "100%")

    verbs = [c[0] for c in flagd["calls"]]
    assert "patch" in verbs
    assert verbs.index("patch") < verbs.index("rollout"), "patch must precede the rollout"
    assert any(c[1] == "restart" for c in flagd["calls"] if c[0] == "rollout")


def test_no_rollout_when_nothing_changed(flagd):
    """reset_flags on an already-clean demo must not restart flagd for nothing."""
    helpers.reset_flags()

    assert not [c for c in flagd["calls"] if c[0] in ("patch", "rollout")]


def test_unknown_flag_names_the_available_ones(flagd):
    """An upstream rename must read as 'this flag is gone', not as a bare KeyError
    from somewhere inside a test that was waiting on an alert."""
    with pytest.raises(KeyError, match="cartFailure"):
        helpers.set_flag("loadGeneratorFloodHomepage", "on")


def test_unknown_variant_names_the_available_ones(flagd):
    with pytest.raises(KeyError, match="100%"):
        helpers.set_flag("cartFailure", "on")


def test_reset_restores_fault_flags(flagd):
    helpers.set_flag("cartFailure", "100%")

    helpers.reset_flags()

    assert flagd["config"]["flags"]["cartFailure"]["defaultVariant"] == "off"


def test_reset_leaves_the_load_generator_running(flagd):
    """Resetting loadGeneratorTraffic to "off" would silence every signal the demo
    produces, leaving the next test with nothing to investigate."""
    helpers.set_flag("loadGeneratorVUs", "50")

    helpers.reset_flags()

    flags = flagd["config"]["flags"]
    assert flags["loadGeneratorTraffic"]["defaultVariant"] == "on"
    assert flags["loadGeneratorVUs"]["defaultVariant"] == "5"


def test_reset_ignores_flags_it_does_not_recognise(flagd):
    """A chart bump that adds a flag must not crash the reset."""
    flagd["config"]["flags"]["newUpstreamFlag"] = {
        "defaultVariant": "enabled",
        "variants": {"enabled": 1, "disabled": 0},
    }

    helpers.reset_flags()

    assert flagd["config"]["flags"]["newUpstreamFlag"]["defaultVariant"] == "enabled"


def test_list_flags_reports_current_variants(flagd):
    helpers.set_flag("cartFailure", "50%")

    assert helpers.list_flags() == {
        "cartFailure": "50%",
        "loadGeneratorTraffic": "on",
        "loadGeneratorVUs": "5",
    }
