"""Tempo HTTP client — TraceQL queries and trace retrieval."""

from __future__ import annotations

import os
from typing import Any

import httpx


class TempoClient:
    """Thin HTTP client for Tempo trace queries and retrieval."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("TEMPO_URL", "http://localhost:3200")).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def query_traceql(self, traceql: str, limit: int = 20) -> dict[str, Any]:
        """Execute a TraceQL query. Returns Tempo search response."""
        client = await self._get_client()
        resp = await client.get(
            f"{self.base_url}/api/search",
            params={"q": traceql, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        """Fetch a full trace by ID."""
        client = await self._get_client()
        resp = await client.get(f"{self.base_url}/api/traces/{trace_id}")
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None