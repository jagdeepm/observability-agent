"""Prometheus HTTP client — direct API access for MVP.

MCP-based access can be swapped in later without changing the agent interface.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class PrometheusClient:
    """Thin HTTP client for Prometheus instant queries and metadata."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("PROMETHEUS_URL", "http://localhost:9090")).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def query_promql(self, promql: str) -> dict[str, Any]:
        """Execute an instant PromQL query. Returns the Prometheus API response."""
        client = await self._get_client()
        resp = await client.get(
            f"{self.base_url}/api/v1/query",
            params={"query": promql},
        )
        resp.raise_for_status()
        return resp.json()

    async def query_range(self, promql: str, start: str, end: str, step: str) -> dict[str, Any]:
        """Execute a range PromQL query."""
        client = await self._get_client()
        resp = await client.get(
            f"{self.base_url}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_metrics(self, pattern: str = "") -> list[str]:
        """List available metric names matching a pattern."""
        client = await self._get_client()
        resp = await client.get(
            f"{self.base_url}/api/v1/label/__name__/values",
        )
        resp.raise_for_status()
        data = resp.json()
        metrics = data.get("data", [])
        if pattern:
            metrics = [m for m in metrics if pattern in m]
        return metrics

    async def get_label_values(self, metric: str, label: str) -> list[str]:
        """Get all values for a label on a given metric."""
        client = await self._get_client()
        resp = await client.get(
            f"{self.base_url}/api/v1/label/{label}/values",
            params={"match[]": metric},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None