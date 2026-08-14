"""Alert Receiver — Alertmanager webhook endpoint with alert grouping.

Thin, rule-based component. No AI, no LLM calls. Creates Incidents from
inbound Alertmanager alerts and groups related alerts by label overlap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from observability_agent.domain.models import Alert, AlertSource, Incident, IncidentStatus
from observability_agent.infrastructure.repository import IncidentRepository

logger = logging.getLogger(__name__)

# Labels excluded from overlap matching (ephemeral, changes per pod/instance)
EPHEMERAL_LABELS = {"pod", "instance", "container", "job", "uid"}

# Default time window for grouping related alerts
DEFAULT_GROUP_WINDOW_MINUTES = 5

# Minimum overlapping label keys for alerts to be grouped into one incident
MIN_OVERLAP_KEYS = 2


class AlertReceiver:
    """Receives Alertmanager webhook payloads and creates/updates Incidents.

    Grouping logic:
    1. Extract labels from the alert
    2. Filter out ephemeral labels
    3. Search for an open incident in the same cluster with ≥2 overlapping labels
    4. If the incident's most recent alert is within the time window, link to it
    5. Otherwise, create a new incident
    """

    def __init__(
        self,
        repo: IncidentRepository,
        group_window_minutes: int = DEFAULT_GROUP_WINDOW_MINUTES,
        scheduler: Any = None,
    ) -> None:
        self.repo = repo
        self.group_window = timedelta(minutes=group_window_minutes)
        self.scheduler = scheduler

    async def handle_webhook(self, request: Request) -> JSONResponse:
        """Handle an Alertmanager webhook POST."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        alerts = payload.get("alerts", [])
        if not alerts:
            return JSONResponse({"error": "no alerts in payload"}, status_code=400)

        results = []
        for alert_data in alerts:
            result = await self._process_alert(alert_data)
            results.append(result)

        incident_ids = list({r["incident_id"] for r in results})
        return JSONResponse({
            "status": "accepted",
            "incidents": results,
        }, status_code=202)

    async def _process_alert(self, alert_data: dict[str, Any]) -> dict[str, Any]:
        """Process a single alert from the Alertmanager payload."""
        labels = alert_data.get("labels", {})
        annotations = alert_data.get("annotations", {})
        fingerprint = alert_data.get("fingerprint", "")
        cluster_id = labels.get("cluster", "default")

        # Create the Alert domain object
        source = AlertSource(
            name=alert_data.get("receiver", "alertmanager"),
            cluster_id=cluster_id,
        )
        alert = Alert(
            source=source,
            fingerprint=fingerprint,
            labels=labels,
            severity=labels.get("severity", "warning"),
            cluster_id=cluster_id,
        )
        if alert_data.get("status") == "resolved":
            alert.resolve()

        # Find or create an Incident
        existing = await self._find_existing_incident(alert)
        incident = existing or Incident(
            cluster_id=alert.cluster_id,
            title=self._build_title(alert),
        )
        incident.add_alert(alert)
        await self.repo.save(incident)

        # Enqueue for investigation if newly created
        if existing is None and self.scheduler:
            self.scheduler.enqueue(incident.id, severity=alert.severity)

        return {
            "alert_id": alert.id,
            "incident_id": incident.id,
            "incident_status": incident.status.value,
            "created": existing is None,
        }

    async def _find_existing_incident(self, alert: Alert) -> Incident | None:
        """Check if there's an existing incident this alert should group into."""
        active = await self.repo.list_active()
        for incident in active:
            if incident.cluster_id != alert.cluster_id:
                continue
            if not incident.linked_alerts:
                continue
            if self._alerts_overlap(alert, incident):
                return incident
        return None

    def _alerts_overlap(self, alert: Alert, incident: Incident) -> bool:
        """Check if an alert should be grouped into an existing incident."""
        if not incident.linked_alerts:
            return False

        existing_labels = incident.linked_alerts[-1].labels
        my_labels = alert.labels

        # Filter out ephemeral labels
        my_keys = set(my_labels.keys()) - EPHEMERAL_LABELS
        existing_keys = set(existing_labels.keys()) - EPHEMERAL_LABELS

        # Count overlapping keys with the same values
        overlap = 0
        for key in my_keys & existing_keys:
            if my_labels[key] == existing_labels[key]:
                overlap += 1

        if overlap < MIN_OVERLAP_KEYS:
            return False

        # Check time window
        last_alert_time = incident.linked_alerts[-1].fired_at
        now = datetime.now(timezone.utc)
        if now - last_alert_time > self.group_window:
            return False

        return True

    def _build_title(self, alert: Alert) -> str:
        service = alert.labels.get("service", "unknown")
        alertname = alert.labels.get("alertname", "unknown")
        return f"{service}: {alertname}"


def _llm_budget_status(scheduler: Any) -> dict[str, Any] | None:
    """The Scheduler's configured LLMProvider's token-budget snapshot, or None.

    Duck-typed and optional: MockLLMProvider has no budget_status, so a
    mock-LLM deployment (or one with no scheduler at all) gets None here and
    /health omits the key entirely, rather than showing a null placeholder.
    """
    llm = getattr(getattr(scheduler, "coordinator", None), "llm", None)
    budget_status = getattr(llm, "budget_status", None)
    return budget_status() if budget_status is not None else None


def create_app(
    repo: IncidentRepository,
    scheduler: Any = None,
    memory_store: Any = None,
) -> Starlette:
    """Create the Starlette app with all routes."""
    receiver = AlertReceiver(repo=repo, scheduler=scheduler)

    async def health(request: Request) -> JSONResponse:
        payload: dict[str, Any] = {
            "status": "ok",
            "queue_size": scheduler.queue_size if scheduler else 0,
            "running": scheduler.running_count if scheduler else 0,
        }
        budget = _llm_budget_status(scheduler)
        if budget is not None:
            payload["llm_budget"] = budget
        return JSONResponse(payload)

    async def list_incidents(request: Request) -> JSONResponse:
        incidents = await repo.list_active()
        return JSONResponse([
            {
                "id": inc.id,
                "title": inc.title,
                "status": inc.status.value,
                "cluster_id": inc.cluster_id,
                "alert_count": len(inc.linked_alerts),
                "created_at": inc.created_at.isoformat(),
                "is_stale": inc.is_stale,
                "queue_position": scheduler.queue_position(inc.id) if scheduler else None,
            }
            for inc in incidents
        ])

    async def get_incident(request: Request) -> JSONResponse:
        inc_id = request.path_params["incident_id"]
        inc = await repo.get(inc_id)
        if inc is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        report = inc.rca_report() if inc.status not in (
            IncidentStatus.OPEN, IncidentStatus.INVESTIGATING
        ) else None
        return JSONResponse({
            "id": inc.id,
            "title": inc.title,
            "status": inc.status.value,
            "cluster_id": inc.cluster_id,
            "alert_count": len(inc.linked_alerts),
            "hypothesis_count": len(inc.hypotheses),
            "created_at": inc.created_at.isoformat(),
            "updated_at": inc.updated_at.isoformat(),
            "is_stale": inc.is_stale,
            "rca": report,
        })

    async def resolve_incident(request: Request) -> JSONResponse:
        inc_id = request.path_params["incident_id"]
        inc = await repo.get(inc_id)
        if inc is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if inc.status.is_terminal:
            return JSONResponse({"error": "already resolved"}, status_code=409)
        from observability_agent.domain.models import Resolution
        inc.close("api")
        await repo.save(inc)
        if memory_store:
            archived = memory_store.archive(inc, Resolution.CLOSED)
            return JSONResponse({
                "status": "resolved",
                "incident_id": inc.id,
                "archived_id": archived.incident_id,
            })
        return JSONResponse({"status": "resolved", "incident_id": inc.id})

    async def search_memory(request: Request) -> JSONResponse:
        q = request.query_params.get("q", "")
        if not q:
            return JSONResponse({"error": "query parameter 'q' required"}, status_code=400)
        if not memory_store:
            return JSONResponse({"results": [], "error": "memory store not configured"})
        results = memory_store.search(q)
        return JSONResponse({"query": q, "results": results})

    async def get_memory_incident(request: Request) -> JSONResponse:
        inc_id = request.path_params["incident_id"]
        if not memory_store:
            return JSONResponse({"error": "memory store not configured"}, status_code=404)
        archived = memory_store.get(inc_id)
        if archived is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(archived.model_dump(mode="json"))

    async def list_memory(request: Request) -> JSONResponse:
        if not memory_store:
            return JSONResponse([], status_code=200)
        return JSONResponse([
            {
                "incident_id": a.incident_id,
                "rca_summary": a.rca_summary,
                "severity": a.severity,
                "resolution": a.resolution.value,
                "resolved_at": a.resolved_at.isoformat(),
            }
            for a in memory_store.list_all()
        ])

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/alerts", receiver.handle_webhook, methods=["POST"]),
        Route("/incidents", list_incidents, methods=["GET"]),
        Route("/incidents/{incident_id}", get_incident, methods=["GET"]),
        Route("/incidents/{incident_id}/resolve", resolve_incident, methods=["POST"]),
        Route("/memory/search", search_memory, methods=["GET"]),
        Route("/memory/incidents", list_memory, methods=["GET"]),
        Route("/memory/incidents/{incident_id}", get_memory_incident, methods=["GET"]),
    ]
    return Starlette(routes=routes)