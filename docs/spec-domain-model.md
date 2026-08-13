# Spec: AI-Native Cloud Observability Agent — Domain Model & Investigation Engine

**Status:** `ready-for-agent`
**Phase:** 1 — Foundation
**Spec version:** 1.0

---

## Problem Statement

Cloud-native incident response today is manual and slow. When an alert fires — "Checkout latency > 500ms" — an SRE must manually query Prometheus, search Loki logs, inspect traces in Tempo, check Kubernetes events, and correlate findings across disjointed tools. Each investigation is a bespoke process that doesn't learn from previous incidents. The result: long MTTR, inconsistent RCAs, and knowledge that evaporates when the on-call engineer rotates.

An AI-powered SRE agent should autonomously investigate incidents by gathering telemetry from multiple sources, forming and testing hypotheses, and producing evidence-backed root cause analyses — all while keeping a human in the loop for remediation decisions.

## Solution

An **AI-Native Cloud Observability Agent** that acts as an autonomous SRE. It receives alerts from Alertmanager, investigates incidents using a hypothesis-driven approach across multiple observability sources (Prometheus, Loki, Tempo, Kubernetes, deployment history, documentation), produces a structured RCA with confidence scores, and recommends remediation — executing only after human approval.

The architecture is built around five bounded contexts, with the **Investigation** context as the core domain. The system is designed for extensibility: new observability sources plug in as MCP tools, new agent types are stateless workers, and the knowledge graph and incident memory grow independently of the investigation engine.

## User Stories

1. As an **SRE**, I want the system to automatically create an incident when an alert fires, so that I don't have to manually triage every alert.
2. As an **SRE**, I want the system to group related alerts into a single incident by label overlap and time window, so that cascading failures are investigated as one problem rather than N separate ones.
3. As an **SRE**, I want the system to gather exploratory telemetry before forming hypotheses, so that the initial investigation is grounded in data rather than guesses.
4. As an **SRE**, I want the system to propose a tree of hypotheses about the root cause, so that the investigation follows a structured causal reasoning process.
5. As an **SRE**, I want the system to gather targeted evidence for each hypothesis from the appropriate observability source, so that each theory is validated or refuted with data.
6. As an **SRE**, I want the system to prune refuted branches of the hypothesis tree, so that investigation effort is focused on the most promising causal paths.
7. As an **SRE**, I want the system to deepen confirmed hypotheses by proposing child hypotheses, so that the investigation drills down to the specific root cause.
8. As an **SRE**, I want the system to produce an RCA report that includes the full causal chain, confidence scores, and all supporting evidence, so that I can trust and verify the AI's conclusions.
9. As an **SRE**, I want the system to assign a confidence score (0.0–1.0) to each hypothesis, so that I can assess how certain the AI is about each finding.
10. As an **SRE**, I want the system to propose a remediation action based on the confirmed root cause, so that I don't have to figure out the fix myself.
11. As an **SRE**, I want remediation to require human approval before execution, so that the AI never makes infrastructure changes without oversight.
12. As an **SRE**, I want to reject a remediation and send the incident back to investigation, so that I can request a better fix if the proposal doesn't make sense.
13. As an **SRE**, I want to see the remediation execution status and verify whether it resolved the incident, so that I know the system recovered.
14. As an **SRE**, I want the system to loop back to investigation if remediation fails, so that the incident isn't left unresolved.
15. As an **SRE**, I want to mark an alert as a false positive, so that noisy alerts don't consume investigation resources.
16. As an **SRE**, I want to manually close stale incidents that are stuck in investigation, so that the incident queue doesn't accumulate abandoned items.
17. As an **SRE**, I want the system to flag incidents that exceed a configurable investigation time threshold as stale, so that I'm alerted to investigations that need human attention.
18. As an **SRE**, I want every action — both human and system — recorded in an audit log on the incident, so that I have a complete trace of what happened and why.
19. As an **SRE**, I want to query a service topology graph during investigation to understand upstream and downstream dependencies, so that I know which services could be affected by or causing the issue.
20. As an **SRE**, I want the topology graph to be continuously discovered from traces, K8s services, and Prometheus targets, so that it stays current without manual maintenance.
21. As an **SRE**, I want resolved incidents archived to a memory store with a vector-searchable summary, so that future investigations can reference similar past incidents.
22. As an **SRE**, I want the system to search the incident memory when a new alert fires, so that I get a head start if this has happened before.
23. As an **SRE**, I want to gather evidence from documentation and runbooks (RAG), so that operational procedures inform the investigation.
24. As an **SRE**, I want exploratory evidence to be untethered from any specific hypothesis, so that the initial sweep of telemetry can inform hypothesis generation.
25. As an **operator**, I want to approve or reject remediation actions, so that I control what changes are made to production.
26. As an **admin**, I want to configure the maximum number of concurrent investigations, so that I can control LLM costs and API rate limits.
27. As an **admin**, I want to register alert sources per cluster, so that the system knows which Alertmanager instances to listen to.
28. As a **developer**, I want the domain model to be a standalone Python package with no infrastructure dependencies, so that I can test the investigation logic without a running cluster.
29. As a **developer**, I want all state transitions to be validated at the domain level, so that invalid states are impossible to represent.
30. As a **developer**, I want the RCA to be a projection over the hypothesis tree, not a separate entity, so that the RCA is always consistent with the underlying evidence.

## Implementation Decisions

### Bounded Contexts

The system is partitioned into five bounded contexts. Each context has its own vocabulary and consistency boundary.

| Context | Role | Entities |
|---|---|---|
| **Alerting** | Inbound signals from alert sources | Alert, AlertSource |
| **Investigation** | Core domain — incident lifecycle & hypothesis-driven RCA | Incident (aggregate root), Hypothesis, Evidence, Remediation |
| **Topology** | Service dependency graph, continuously discovered | Node, Edge |
| **Memory** | Archived incident snapshots for similarity search | ResolvedIncident |
| **Knowledge/RAG** | Documentation retrieval — infrastructure only | (no domain entities) |

### Alert vs Incident

Alert and Incident are distinct entities. An Alert is an immutable inbound signal — it fired or it didn't. An Incident is a lifecycle wrapper created when an alert is acknowledged. One Incident can group multiple alerts via label overlap and time-window correlation. Alerts carry a `cluster_id` for multi-cluster partitioning from day one.

### Hypothesis Tree

The investigation engine is built around a hypothesis tree with parent-child adjacency. Each hypothesis has a status (`proposed → investigating → confirmed | refuted`), a confidence score (0.0–1.0), and a list of linked Evidence. The tree supports:

- **Ancestor chain:** walk from any hypothesis to the root.
- **Confirmed path:** the chain from root to a leaf where every link is confirmed. Returns None if any link in the chain is not confirmed.
- **Pruning:** when a hypothesis is refuted, all its children are automatically refuted (cascading refutation).

### RCA as a Projection

The Root Cause Analysis is not a separate entity. It is a projection over the hypothesis tree — the confirmed path with the highest-confidence leaf, bundled with all supporting evidence. The `rca_report()` method on the Incident aggregate produces this projection. This ensures the RCA is always consistent with the underlying evidence.

### Evidence Model

A single unified Evidence entity with a type discriminator (`EvidenceSource` enum) and a JSON `raw_payload` for source-specific data. The `hypothesis_id` field is nullable — exploratory evidence gathered during the initial sweep is not tied to any hypothesis. Evidence can only be added when the Incident is in `investigating` status.

### Remediation as a Sub-Entity

Remediation is a sub-entity of the Incident aggregate, not a standalone aggregate. It has its own mini-state-machine:

```
proposed → pending_approval → approved → executing → executed → verified
                              ↘ rejected                   ↘ failed
```

Rejected and failed remediations loop the Incident back to `investigating` status. Remediation can only be executed after human approval.

### Incident State Machine

The Incident aggregate enforces a strict state machine. Transitions are validated at the domain level — invalid transitions raise `ValueError`.

```
open → investigating → rca_produced → pending_approval → remediating → resolved
  ↘        ↘                                                 ↘ failed (→ investigating)
false_positive  ↘                                           
            closed (manual, from any non-terminal state)
```

Three terminal states: `resolved`, `false_positive`, `closed`. Failed remediation loops back to `investigating` via `re_investigate()`. Rejected remediation loops back to `investigating` directly.

### Stale Detection

Incidents that exceed a configurable time threshold in `investigating` status are flagged as stale (a boolean flag, not a state). Stale incidents are surfaced for human attention — they are never auto-closed. Manual closure moves the incident to `closed` (terminal).

### Agents as Stateless Workers

The seven specialized agents (Coordinator, Metrics, Logs, Trace, Kubernetes, Deployment, Knowledge) are not domain entities. They are stateless workers — functions that take input, call an LLM with MCP tools, and return output. The Coordinator orchestrates the investigation by reading the Incident state and delegating to specialized agents. Agent identity is recorded as metadata on hypotheses and evidence (`created_by`, `collected_by` fields), but there is no Agent or AgentRun entity.

### Topology Graph

The topology graph uses a flat node/edge model. Nodes are typed by `NodeType` (service, database, queue, topic, deployment, dashboard, runbook, owner). Edges are typed by `EdgeRelationship` (depends_on, publishes_to, subscribes_to, owns, monitored_by, documented_by). Both carry a `properties` JSON blob for type-specific data. The graph is partitioned by `cluster_id` and is read-only from the investigation's perspective.

### Incident Memory

When an incident reaches a terminal state, it is snapshotted to the Memory store as a `ResolvedIncident`. The snapshot includes the alert signature, the full hypothesis tree, the RCA summary, the remediation applied, and whether it was verified. The Memory store is a separate read model optimized for vector similarity search. The `embedding` field is an infrastructure concern — handled by the vector database, not the domain model.

### Inter-Context Communication

- **Alerting → Investigation:** Async domain event (`AlertCreated`). The Coordinator picks up the event and creates/updates an Incident.
- **Investigation → Topology:** Synchronous query. The Coordinator needs the answer to proceed.
- **Investigation → Memory:** Hybrid. Searching past incidents is synchronous (needed for hypothesis generation). Archiving a resolved incident is async (`IncidentResolved` event).
- **Investigation → Knowledge/RAG:** Synchronous query. Results feed into the unified Evidence model.

### User Model

Minimal user model: `viewer`, `operator`, `admin` roles. User identity is a string (user ID) — authentication is delegated to an external gateway (OAuth, LDAP). Every human action on an Incident is recorded in the audit log with the user ID, action, timestamp, and note.

### Multi-Cluster

`cluster_id` is a first-class dimension on all entities (Alert, Incident, Node, Edge). This costs nothing to add now and prevents a painful migration when multi-cluster support is needed.

### Concurrency

Multiple Incidents can be investigated simultaneously. A configurable `max_concurrent_investigations` cap (default: 3) limits concurrent LLM calls to avoid rate-limiting and cost spikes. Incidents beyond the cap are queued in `open` status.

### Technology

- **Language:** Python 3.12+
- **Domain models:** Pydantic v2 — all entities are Pydantic BaseModel subclasses with validation
- **Future:** PydanticAI for agent orchestration, LangGraph for investigation workflows, Pydantic for structured LLM outputs
- **Dependencies (Phase 1):** pydantic only. No database, no MCP, no LLM client.

## Testing Decisions

### Seam

The primary test seam is the **Incident aggregate root's public interface**. All investigation behavior — state transitions, hypothesis management, evidence attachment, RCA projection, remediation flow — is exercised through the Incident's methods. The `Hypothesis` tree operations and `Remediation` sub-state-machine are secondary seams, tested through the Incident aggregate.

### What makes a good test

Tests exercise external behavior, not implementation details. They assert:
- State transitions succeed or fail as expected
- State machines reject invalid transitions with clear errors
- The hypothesis tree correctly resolves ancestor chains and confirmed paths
- The RCA projection is consistent with the underlying hypothesis tree
- The audit log records every action

### Test modules

All tests live in `tests/test_domain.py`. One file, 26 tests. No test framework beyond pytest. No fixtures, no mocks. The `demo()` function doubles as an integration test — it runs the full checkout-latency scenario from the spec and asserts the outcome.

### Prior art

The `demo()` function in `tests/test_domain.py` serves as the canonical integration test pattern. It traces the full lifecycle: alert creation → incident creation → exploratory evidence → hypothesis proposal → targeted evidence → hypothesis evaluation → tree pruning → hypothesis deepening → RCA production → remediation proposal → approval → execution → verification → memory archival. Future tests for new agent types or investigation workflows should follow this pattern.

## Out of Scope

- MCP server implementations (Prometheus, Loki, Tempo, Kubernetes, GitHub, Grafana, ArgoCD, Terraform)
- LLM integration (PydanticAI, LangGraph, model selection, prompt engineering)
- Coordinator agent orchestration logic
- Specialized agent implementations (Metrics, Logs, Trace, Kubernetes, Deployment, Knowledge)
- REST API, CLI, Slack bot, Web dashboard
- Authentication and authorization (OAuth, LDAP, JWT)
- Database persistence (the domain model is in-memory Pydantic for now)
- Event bus / message queue
- Vector database for incident memory (Qdrant, pgvector)
- RAG indexing pipeline for documentation
- Topology graph auto-discovery from traces, K8s, and Prometheus
- Kubernetes infrastructure (Kind cluster, Helm charts, OpenTelemetry Demo)
- Alertmanager webhook receiver
- Natural language to PromQL/LogQL/TraceQL translation
- Predictive incident detection / anomaly detection
- Cost optimization, capacity forecasting, health scoring
- Multi-cloud support (AWS, Azure, GCP)

## Further Notes

- The domain model is the foundation. Every subsequent phase (MCP integration, agent orchestration, knowledge layer, memory) builds on these entities and state machines.
- The `rca_report()` method produces a dictionary — a structured output the LLM can consume and the UI can render. Future phases may add a `StructuredRCA` Pydantic model for stronger typing.
- The `raw_payload` field on Evidence is intentionally untyped (`dict[str, Any]`). Each MCP server defines its own payload shape. Validation of payload structure is the responsibility of the agent that produces it, not the domain model.
- The `confirmed_path()` method requires the entire hypothesis list to be passed in. This is deliberate — the Hypothesis doesn't hold a reference to its parent object, only a `parent_id`. This keeps Hypothesis independently testable while allowing the Incident to resolve the tree.
- The `verify_remediation()` method transitions the Incident to `FAILED` on failure but does NOT automatically call `re_investigate()`. The caller must explicitly call `re_investigate()` — this gives the Coordinator a chance to log, notify, or adjust its approach before re-entering investigation.
