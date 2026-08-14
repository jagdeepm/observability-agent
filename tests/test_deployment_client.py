"""Tests for the DeploymentClient's configuration guard."""

from __future__ import annotations

import pytest

from observability_agent.mcp.deployment import DeploymentClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.recent_commits(),
        lambda c: c.compare_commits("a", "b"),
        lambda c: c.list_deployments(),
    ],
)
async def test_unconfigured_client_never_calls_github(call, monkeypatch):
    """An empty repo must not become https://api.github.com/repos//commits.

    That URL is a real request to a public API with a malformed path. It answers
    404, the agent reports the 404 as evidence, and an investigation can end up
    blaming a production incident on the agent's own missing configuration —
    observed doing exactly that against a live cluster.
    """
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    client = DeploymentClient(repo="")

    def explode(*args, **kwargs):
        raise AssertionError("network call attempted without a configured repo")

    monkeypatch.setattr(client, "_get_client", explode)

    with pytest.raises(RuntimeError, match="GITHUB_REPO"):
        await call(client)


@pytest.mark.asyncio
async def test_configured_client_passes_the_guard(monkeypatch):
    """The guard must not block a properly configured client."""
    client = DeploymentClient(repo="owner/name")
    client._require_repo()  # does not raise
