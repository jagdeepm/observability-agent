"""Documentation search client — semantic search over indexed docs/runbooks."""

from __future__ import annotations

import os
from typing import Any

import httpx


class DocSearchClient:
    """HTTP client for documentation/runbook search (RAG endpoint)."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("RAG_URL", "http://localhost:8001")).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Semantic search over indexed documentation."""
        client = await self._get_client()
        resp = await client.post(
            f"{self.base_url}/search",
            json={"query": query, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def get_document(self, doc_id: str) -> dict[str, Any]:
        """Fetch a full document by ID."""
        client = await self._get_client()
        resp = await client.get(f"{self.base_url}/documents/{doc_id}")
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None