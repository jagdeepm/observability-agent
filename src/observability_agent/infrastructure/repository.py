"""Incident repository — persistence abstraction for the Investigation aggregate."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from observability_agent.domain.models import Incident


@runtime_checkable
class IncidentRepository(Protocol):
    """Repository protocol for loading and saving Incidents."""

    async def get(self, incident_id: str) -> Incident | None:
        """Load an Incident by ID. Returns None if not found."""
        ...

    async def save(self, incident: Incident) -> None:
        """Persist the Incident aggregate. Full save — no partial updates."""
        ...

    async def list_active(self) -> list[Incident]:
        """List all Incidents that are not in a terminal state."""
        ...


class InMemoryIncidentRepository:
    """Dict-backed repository. For MVP and tests. No persistence across restarts."""

    def __init__(self) -> None:
        self._store: dict[str, Incident] = {}
        self.save_count = 0

    async def get(self, incident_id: str) -> Incident | None:
        return self._store.get(incident_id)

    async def save(self, incident: Incident) -> None:
        self._store[incident.id] = incident
        self.save_count += 1

    async def list_active(self) -> list[Incident]:
        return [inc for inc in self._store.values() if inc.status.is_active]