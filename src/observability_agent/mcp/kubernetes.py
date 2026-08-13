"""Kubernetes HTTP client — pod, event, and deployment queries.

Uses the K8s API server. For MVP, also supports kubectl subprocess fallback.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class KubernetesClient:
    """Thin HTTP client for Kubernetes API queries."""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("K8S_API_URL", "https://kubernetes.default.svc")).rstrip("/")
        self.token = token or os.getenv("K8S_TOKEN", "")
        self._client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            verify = not os.getenv("K8S_INSECURE", "")
            self._client = httpx.AsyncClient(timeout=30.0, verify=verify)
        return self._client

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        client = await self._get_client()
        resp = await client.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def list_pods(self, namespace: str = "default", labels: str | None = None) -> dict[str, Any]:
        """List pods in a namespace, optionally filtered by label selector."""
        params: dict[str, Any] = {}
        if labels:
            params["labelSelector"] = labels
        return await self._get(f"/api/v1/namespaces/{namespace}/pods", **params)

    async def get_pod(self, name: str, namespace: str = "default") -> dict[str, Any]:
        """Get a single pod's details."""
        return await self._get(f"/api/v1/namespaces/{namespace}/pods/{name}")

    async def get_pod_logs(
        self, name: str, namespace: str = "default", container: str | None = None,
        tail_lines: int = 100,
    ) -> str:
        """Get pod logs as text."""
        client = await self._get_client()
        params: dict[str, Any] = {"tailLines": tail_lines}
        if container:
            params["container"] = container
        resp = await client.get(
            f"{self.base_url}/api/v1/namespaces/{namespace}/pods/{name}/log",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.text

    async def list_events(self, namespace: str = "default") -> dict[str, Any]:
        """List events in a namespace."""
        return await self._get(f"/api/v1/namespaces/{namespace}/events")

    async def get_deployment(self, name: str, namespace: str = "default") -> dict[str, Any]:
        """Get deployment details."""
        return await self._get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None