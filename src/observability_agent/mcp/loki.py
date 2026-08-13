"""Loki HTTP client — LogQL queries and label discovery."""

from __future__ import annotations

import os
from typing import Any

import httpx


class LokiClient:
    """Thin HTTP client for Loki log queries and label discovery."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("LOKI_URL", "http://localhost:3100")).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def query_logql(self, logql: str, limit: int = 100) -> dict[str, Any]:
        """Execute a LogQL query. Returns Loki API response."""
        client = await self._get_client()
        resp = await client.get(
            f"{self.base_url}/loki/api/v1/query_range",
            params={"query": logql, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_labels(self) -> list[str]:
        """List available label names."""
        client = await self._get_client()
        resp = await client.get(f"{self.base_url}/loki/api/v1/labels")
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None