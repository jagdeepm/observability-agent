"""Deployment history client — GitHub API for recent deployments and diffs."""

from __future__ import annotations

import os
from typing import Any

import httpx


class DeploymentClient:
    """HTTP client for GitHub deployment history and commit diffs."""

    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
    ) -> None:
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self.repo = repo or os.getenv("GITHUB_REPO", "")
        self.api_base = "https://api.github.com"
        self._client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def recent_commits(self, path: str = "", count: int = 10) -> list[dict[str, Any]]:
        """Get recent commits, optionally filtered by path."""
        client = await self._get_client()
        params: dict[str, Any] = {"per_page": count}
        if path:
            params["path"] = path
        resp = await client.get(
            f"{self.api_base}/repos/{self.repo}/commits",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def compare_commits(self, base: str, head: str) -> dict[str, Any]:
        """Get the diff between two commits/revisions."""
        client = await self._get_client()
        resp = await client.get(
            f"{self.api_base}/repos/{self.repo}/compare/{base}...{head}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def list_deployments(self, environment: str = "production") -> list[dict[str, Any]]:
        """List recent deployments for a given environment."""
        client = await self._get_client()
        resp = await client.get(
            f"{self.api_base}/repos/{self.repo}/deployments",
            headers=self._headers(),
            params={"environment": environment, "per_page": 10},
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None