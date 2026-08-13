"""CLI entry point for the observability agent.

Usage:
    observability-agent investigate --service <name> --alert <description>
    observability-agent investigate --service checkout --alert "latency > 500ms" --mock-llm
    observability-agent investigate --service checkout --alert "latency > 500ms" --output json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from observability_agent.agents.protocols import MockAgent
from observability_agent.coordinator.engine import Coordinator
from observability_agent.domain.models import Alert, AlertSource, Incident
from observability_agent.infrastructure.llm_provider import (
    MockLLMProvider,
    create_llm_provider,
)
from observability_agent.infrastructure.repository import InMemoryIncidentRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="observability-agent",
        description="AI-Native Cloud Observability Agent — autonomous incident investigation",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("investigate", help="Run an investigation")
    inv.add_argument("--service", required=True, help="Affected service name")
    inv.add_argument("--alert", required=True, help="Alert description (e.g., 'latency > 500ms')")
    inv.add_argument("--severity", default="warning", help="Alert severity")
    inv.add_argument("--cluster", default="default", help="Cluster identifier")
    inv.add_argument("--namespace", default="prod", help="Kubernetes namespace")
    inv.add_argument("--mock-llm", action="store_true", help="Use mock LLM provider (offline)")
    inv.add_argument("--output", choices=["text", "json"], default="text", help="Output format")

    srv = sub.add_parser("serve", help="Start the API server (Alertmanager webhook)")
    srv.add_argument("--host", default="0.0.0.0", help="Bind address")
    srv.add_argument("--port", type=int, default=8000, help="Bind port")
    srv.add_argument("--mock-llm", action="store_true", help="Use mock LLM provider (offline)")

    args = parser.parse_args()
    _configure_logging()
    if args.command == "serve":
        asyncio.run(_run_server(args))
    else:
        asyncio.run(_run_investigation(args))


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    # Keep uvicorn/httpx noise at WARNING unless DEBUG requested
    if level != "DEBUG":
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _run_server(args: argparse.Namespace) -> None:
    """Start the API server with Alertmanager webhook and scheduler."""
    import uvicorn

    from observability_agent.agents.protocols import MockAgent
    from observability_agent.api.alert_receiver import create_app
    from observability_agent.coordinator.engine import Coordinator
    from observability_agent.infrastructure.llm_provider import (
        MockLLMProvider,
        create_llm_provider,
    )
    from observability_agent.infrastructure.repository import InMemoryIncidentRepository
    from observability_agent.infrastructure.scheduler import Scheduler

    repo = InMemoryIncidentRepository()

    if args.mock_llm:
        llm = MockLLMProvider()
        _register_mock_responses(llm)
        metrics_agent = MockAgent()
    else:
        llm = create_llm_provider()
        from observability_agent.agents.metrics import MetricsAgent
        metrics_agent = MetricsAgent(llm=llm)

    if args.mock_llm:
        logs_agent = MockAgent()
        trace_agent = MockAgent()
        kubernetes_agent = MockAgent()
    else:
        from observability_agent.agents.logs import LogsAgent
        from observability_agent.agents.traces import TraceAgent
        from observability_agent.agents.kubernetes import KubernetesAgent
        logs_agent = LogsAgent(llm=llm)
        trace_agent = TraceAgent(llm=llm)
        kubernetes_agent = KubernetesAgent(llm=llm)

    if args.mock_llm:
        deployment_agent = MockAgent()
        knowledge_agent = MockAgent()
    else:
        from observability_agent.agents.deployment import DeploymentAgent
        from observability_agent.agents.knowledge import KnowledgeAgent
        deployment_agent = DeploymentAgent(llm=llm)
        knowledge_agent = KnowledgeAgent(llm=llm)

    coord = Coordinator(
        llm=llm,
        repo=repo,
        metrics_agent=metrics_agent,
        logs_agent=logs_agent,
        trace_agent=trace_agent,
        kubernetes_agent=kubernetes_agent,
        deployment_agent=deployment_agent,
        knowledge_agent=knowledge_agent,
    )

    from observability_agent.memory.store import MemoryStore
    memory = MemoryStore()

    scheduler = Scheduler(coordinator=coord, repo=repo)
    app = create_app(repo, scheduler=scheduler, memory_store=memory)

    print(f"Starting observability-agent API on http://{args.host}:{args.port}", file=sys.stderr)
    print(f"  POST /alerts — Alertmanager webhook", file=sys.stderr)
    print(f"  GET  /incidents — List active incidents", file=sys.stderr)
    print(f"  GET  /incidents/<id> — Incident details", file=sys.stderr)
    print(f"  Scheduler: max_concurrent={scheduler.max_concurrent}", file=sys.stderr)

    # Start scheduler in background
    scheduler_task = asyncio.create_task(scheduler.start())

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

    # Cleanup
    await scheduler.stop()
    scheduler_task.cancel()


async def _run_investigation(args: argparse.Namespace) -> None:
    # Create the alert
    source = AlertSource(name="cli", cluster_id=args.cluster)
    alert = Alert(
        source=source,
        fingerprint=f"{args.service}-{args.alert}",
        labels={
            "service": args.service,
            "namespace": args.namespace,
            "severity": args.severity,
            "alertname": args.alert,
        },
        severity=args.severity,
        cluster_id=args.cluster,
    )

    # Create the incident
    incident = Incident(
        cluster_id=args.cluster,
        title=f"{args.service}: {args.alert}",
    )
    incident.add_alert(alert)

    # Wire up dependencies
    repo = InMemoryIncidentRepository()
    await repo.save(incident)

    if args.mock_llm:
        llm = MockLLMProvider()
        _register_mock_responses(llm)
        metrics_agent = MockAgent()
    else:
        llm = create_llm_provider()
        # Use real MetricsAgent with Prometheus, mocks for the rest
        from observability_agent.agents.metrics import MetricsAgent
        metrics_agent = MetricsAgent(llm=llm)

    coord = Coordinator(
        llm=llm,
        repo=repo,
        metrics_agent=metrics_agent,
        logs_agent=MockAgent(),
        trace_agent=MockAgent(),
        kubernetes_agent=MockAgent(),
        deployment_agent=MockAgent(),
        knowledge_agent=MockAgent(),
    )

    # Run investigation
    print(f"Investigating: {incident.title}", file=sys.stderr)
    result = await coord.run(incident.id)

    # Output
    report = result.rca_report()
    if args.output == "json":
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_text_report(result, report)


def _print_text_report(incident: Incident, report: dict) -> None:
    """Print a human-readable RCA report."""
    width = 60
    print("=" * width)
    print("  ROOT CAUSE ANALYSIS")
    print("=" * width)
    print(f"  Incident:    {incident.id}")
    print(f"  Title:       {incident.title}")
    print(f"  Status:      {incident.status.value}")
    print(f"  Cluster:     {incident.cluster_id}")
    print("-" * width)

    if report.get("root_cause"):
        print(f"  Root Cause:  {report['root_cause']}")
        print(f"  Confidence:  {report['confidence']:.0%}")
        if report.get("causal_chain"):
            print(f"  Causal Chain:")
            for i, step in enumerate(report["causal_chain"], 1):
                print(f"    {i}. {step}")
    else:
        print(f"  Result:      {report.get('status', 'unknown')}")

    print("-" * width)
    evidence = report.get("evidence", [])
    if evidence:
        print(f"  Evidence ({len(evidence)} items):")
        for ev in evidence:
            print(f"    [{ev['source']}] {ev['summary'][:80]}")
    else:
        print("  Evidence:    (none)")

    remediation = report.get("remediation")
    if remediation:
        print("-" * width)
        print(f"  Remediation: {remediation}")
        print(f"  Status:      {report.get('remediation_status', 'unknown')}")
        print(f"  ⚠ Human approval required before execution")

    print("=" * width)


def _register_mock_responses(llm: MockLLMProvider) -> None:
    """Register canned LLM responses for a mock investigation."""
    from observability_agent.coordinator.outputs import (
        DeepenHypothesesOutput,
        EvaluateHypothesesOutput,
        GenerateHypothesesOutput,
        HypothesisEvaluation,
        HypothesisProposal,
        RCAOutput,
    )

    llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
        hypotheses=[
            HypothesisProposal(statement="database connection pool is exhausted"),
            HypothesisProposal(statement="recent deployment introduced a regression"),
        ]
    ))

    llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(
                hypothesis_id="will-be-set", status="confirmed",
                confidence=0.9, reasoning="connection pool exhaustion is a common cause of latency",
            ),
            HypothesisEvaluation(
                hypothesis_id="will-be-set", status="refuted",
                confidence=0.0, reasoning="no recent deployments detected",
            ),
        ]
    ))

    llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(
        child_hypotheses=[
            HypothesisProposal(statement="connection pool timeout was removed in recent config change"),
        ]
    ))

    llm.register(EvaluateHypothesesOutput, EvaluateHypothesesOutput(
        evaluations=[
            HypothesisEvaluation(
                hypothesis_id="will-be-set", status="confirmed",
                confidence=0.95, reasoning="config change matches the timeline",
            ),
        ]
    ))

    llm.register(DeepenHypothesesOutput, DeepenHypothesesOutput(child_hypotheses=[]))

    llm.register(RCAOutput, RCAOutput(
        root_cause_summary="The connection pool timeout was removed in a recent configuration change, causing database connection exhaustion and increased checkout latency.",
        confidence=0.95,
        remediation_action="rollback checkout deployment to restore connection pool timeout",
    ))