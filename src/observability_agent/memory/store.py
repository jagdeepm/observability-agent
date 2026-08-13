"""Memory Store — in-memory archive of resolved incidents with token-based search.

For MVP, uses Jaccard similarity on tokenized RCA summaries. A vector DB
(Qdrant/pgvector) can be swapped in later without changing the API.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from observability_agent.domain.models import (
    HypothesisStatus,
    Incident,
    Resolution,
    ResolvedIncident,
)


class MemoryStore:
    """In-memory store for resolved incidents with similarity search.

    Token-based Jaccard similarity for MVP. Swap for vector DB later.
    """

    def __init__(self) -> None:
        self._incidents: dict[str, ResolvedIncident] = {}

    def archive(self, incident: Incident, resolution: Resolution) -> ResolvedIncident:
        """Snapshot a resolved incident into the memory store.

        Extracts the confirmed hypothesis path, RCA summary, and remediation.
        """
        confirmed = [h for h in incident.hypotheses if h.status == HypothesisStatus.CONFIRMED]
        best_leaf = max(confirmed, key=lambda h: h.confidence) if confirmed else None
        path = best_leaf.confirmed_path(incident.hypotheses) if best_leaf else None
        causal_chain = [h.statement for h in (path or [])] if path else []

        now = datetime.now(timezone.utc)
        duration = (incident.updated_at - incident.created_at).total_seconds()

        # Build RCA summary from the causal chain
        rca_summary = " → ".join(causal_chain) if causal_chain else "no root cause identified"

        resolved = ResolvedIncident(
            incident_id=incident.id,
            alert_signature=incident.linked_alerts[0].labels if incident.linked_alerts else {},
            resolution=resolution,
            duration_seconds=duration,
            severity=max((a.severity for a in incident.linked_alerts), default="warning"),
            affected_services=list({
                a.labels.get("service", "unknown")
                for a in incident.linked_alerts
            }),
            hypothesis_tree={
                "causal_chain": causal_chain,
                "confidence": best_leaf.confidence if best_leaf else 0.0,
                "total_hypotheses": len(incident.hypotheses),
                "confirmed": len(confirmed),
                "refuted": len([h for h in incident.hypotheses if h.status == HypothesisStatus.REFUTED]),
            },
            rca_summary=rca_summary,
            remediation_applied=incident.remediation.action if incident.remediation else None,
            verified=incident.remediation.status.value == "verified" if incident.remediation else False,
            resolved_at=now,
            created_at=incident.created_at,
        )
        self._incidents[incident.id] = resolved
        return resolved

    def get(self, incident_id: str) -> ResolvedIncident | None:
        """Retrieve an archived incident by ID."""
        return self._incidents.get(incident_id)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search archived incidents by token-based Jaccard similarity.

        Returns results sorted by similarity score (descending).
        """
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return []

        scored = []
        for archived in self._incidents.values():
            doc_tokens = set(_tokenize(archived.rca_summary))
            # Also include alert signature tokens
            for v in archived.alert_signature.values():
                doc_tokens.update(_tokenize(v))

            if not doc_tokens:
                continue

            intersection = query_tokens & doc_tokens
            union = query_tokens | doc_tokens
            score = len(intersection) / len(union) if union else 0.0

            if score > 0:
                scored.append({
                    "incident_id": archived.incident_id,
                    "score": round(score, 3),
                    "rca_summary": archived.rca_summary,
                    "severity": archived.severity,
                    "resolution": archived.resolution.value,
                    "resolved_at": archived.resolved_at.isoformat(),
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def list_all(self) -> list[ResolvedIncident]:
        """List all archived incidents."""
        return list(self._incidents.values())


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase words, filtering stopwords."""
    stopwords = {
        "the", "a", "an", "is", "was", "in", "to", "of", "and", "or",
        "for", "on", "at", "by", "with", "from", "this", "that", "it",
        "be", "as", "are", "not", "no", "has", "had", "have", "was",
    }
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in stopwords and len(w) > 1]