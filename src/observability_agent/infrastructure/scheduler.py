"""Scheduler — async worker pool for concurrent incident investigations.

Manages a queue of open Incidents, spawns Coordinator tasks with concurrency
control, tracks running investigations, handles stale detection and crash recovery.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any

from observability_agent.domain.models import IncidentStatus

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 3
DEFAULT_STALE_HOURS = 24
DEFAULT_MAX_RETRIES = 3


class Scheduler:
    """Manages concurrent incident investigations with an async worker pool.

    Queue is FIFO with severity-weighted priority: critical alerts jump to the front.
    """

    def __init__(
        self,
        coordinator: Any,  # Coordinator — avoid circular import
        repo: Any,  # IncidentRepository
        max_concurrent: int | None = None,
        stale_threshold_hours: int | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.coordinator = coordinator
        self.repo = repo
        self.max_concurrent = max_concurrent or int(
            os.getenv("MAX_CONCURRENT_INVESTIGATIONS", str(DEFAULT_MAX_CONCURRENT))
        )
        self.stale_threshold_hours = stale_threshold_hours or int(
            os.getenv("STALE_THRESHOLD_HOURS", str(DEFAULT_STALE_HOURS))
        )
        self.max_retries = max_retries

        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._queue: deque[str] = deque()  # incident IDs
        self._running: dict[str, asyncio.Task] = {}
        self._retries: dict[str, int] = {}
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._stale_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, incident_id: str, severity: str = "warning") -> None:
        """Add an incident to the investigation queue.

        Critical-severity incidents jump to the front.
        """
        if severity == "critical":
            self._queue.appendleft(incident_id)
            logger.info("Enqueued %s (critical, priority)", incident_id)
        else:
            self._queue.append(incident_id)
            logger.info("Enqueued %s (severity=%s)", incident_id, severity)

    async def start(self) -> None:
        """Start the scheduler worker loop."""
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._stale_task = asyncio.create_task(self._stale_loop())
        logger.info(
            "Scheduler started (max_concurrent=%d, stale_threshold=%dh)",
            self.max_concurrent,
            self.stale_threshold_hours,
        )

    async def stop(self) -> None:
        """Stop the scheduler and wait for running investigations."""
        self._stop_event.set()
        if self._worker_task:
            self._worker_task.cancel()
        if self._stale_task:
            self._stale_task.cancel()
        # Wait for running tasks to complete
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        logger.info("Scheduler stopped")

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    @property
    def running_count(self) -> int:
        return len(self._running)

    def queue_position(self, incident_id: str) -> int | None:
        """Return the position in the queue (0-indexed), or None if not queued."""
        try:
            return list(self._queue).index(incident_id)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        """Main loop: dequeue and run investigations."""
        while not self._stop_event.is_set():
            if not self._queue:
                await asyncio.sleep(0.1)
                continue

            incident_id = self._queue.popleft()
            asyncio.create_task(self._run_investigation(incident_id))

    async def _run_investigation(self, incident_id: str) -> None:
        """Run a single investigation with concurrency control and retry."""
        async with self._semaphore:
            self._running[incident_id] = asyncio.current_task()
            try:
                await self.coordinator.run(incident_id)
            except Exception as exc:
                logger.error("Investigation %s failed: %s", incident_id, exc)
                retries = self._retries.get(incident_id, 0) + 1
                self._retries[incident_id] = retries
                if retries <= self.max_retries:
                    logger.info("Re-queuing %s (retry %d/%d)", incident_id, retries, self.max_retries)
                    self._queue.appendleft(incident_id)
                else:
                    logger.error("Investigation %s failed after %d retries — flagging stale", incident_id, retries)
                    incident = await self.repo.get(incident_id)
                    if incident and incident.status.is_active:
                        incident.flag_stale()
                        await self.repo.save(incident)
            finally:
                self._running.pop(incident_id, None)

    # ------------------------------------------------------------------
    # Stale detection
    # ------------------------------------------------------------------

    async def _stale_loop(self) -> None:
        """Periodically check for stale investigations."""
        while not self._stop_event.is_set():
            await asyncio.sleep(60)  # check every minute
            active = await self.repo.list_active()
            now = datetime.now(timezone.utc)
            threshold_seconds = self.stale_threshold_hours * 3600

            for incident in active:
                if incident.is_stale:
                    continue
                if incident.status == IncidentStatus.INVESTIGATING:
                    elapsed = (now - incident.created_at).total_seconds()
                    if elapsed > threshold_seconds:
                        logger.warning("Flagging stale incident %s (elapsed: %.1fh)", incident.id, elapsed / 3600)
                        incident.flag_stale()
                        await self.repo.save(incident)