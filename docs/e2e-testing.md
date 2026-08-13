# End-to-End Testing

The observability agent ships with a full end-to-end test harness that deploys the agent against a real Kubernetes cluster running the [OpenTelemetry Astronomy Shop Demo](https://github.com/open-telemetry/opentelemetry-demo) — a 12-service microservices application with pre-instrumented telemetry. The e2e tests inject faults (latency spikes, error rates, pod crashes), verify that Prometheus fires alerts, and assert that the agent autonomously investigates and produces a root cause analysis.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Kind Cluster (observability-e2e)                       │
│                                                         │
│  ┌──────────────────┐   ┌──────────────────────────┐   │
│  │ OTEL Demo (12 svc)│   │ kube-prometheus-stack     │   │
│  │                    │   │                            │   │
│  │ checkoutservice    │──▶│ Prometheus (scrape metrics)│   │
│  │ cartservice        │   │ Alertmanager              │   │
│  │ adservice          │   │ Grafana                   │   │
│  │ ...                │   └──────────┬───────────────┘   │
│  └──────────────────┘              │ webhook            │
│                                    ▼                    │
│  ┌──────────────────────────────────────────────────┐   │
│  │ observability-agent                              │   │
│  │                                                  │   │
│  │  POST /alerts  ← Alertmanager webhook            │   │
│  │  GET  /incidents  → investigation results         │   │
│  │  GET  /health     → health check                 │   │
│  │                                                  │   │
│  │  Coordinator → LLM → 6 agents → MCP servers      │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
         │                    │
    localhost:8080      localhost:9091
    (agent API)         (Prometheus)
```

## Prerequisites

| Tool | Install | Check |
|---|---|---|
| [Kind](https://kind.sigs.k8s.io/) | `brew install kind` | `kind version` |
| [Helm](https://helm.sh/) | `brew install helm` | `helm version` |
| [Task](https://taskfile.dev/) | `brew install task` | `task --version` |
| Docker | Docker Desktop / OrbStack / Colima | `docker ps` |
| Python 3.12+ | `brew install python@3.12` | `python3 --version` |

## Quick Start

```bash
# Clone and install
git clone <repo-url> && cd observability-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run everything (cluster → monitoring → demo → agent → tests)
task all
```

This takes ~10–15 minutes on first run (pulling images, building the agent). Subsequent runs are faster.

## Running with a Real LLM

The agent uses a mock LLM by default (canned responses, no API key needed). To use a real LLM so the e2e tests exercise genuine hypothesis generation and evaluation:

```bash
export LLM_API_KEY="sk-..."
export LLM_MODEL="openai:gpt-4o"          # optional, default
export LLM_BASE_URL=""                     # optional, for non-OpenAI providers

task all
```

The `LLM_API_KEY` is injected into the cluster as a Kubernetes Secret at deploy time. The agent pod reads it via `secretKeyRef`.

### LLM Providers

Any OpenAI-compatible API works:

```bash
# OpenAI
export LLM_API_KEY="sk-..." LLM_MODEL="openai:gpt-4o"

# Anthropic (via compatible proxy)
export LLM_API_KEY="sk-ant-..." LLM_MODEL="anthropic:claude-sonnet-5"

# Local model (Ollama, vLLM, LM Studio)
export LLM_API_KEY="not-needed" \
       LLM_BASE_URL="http://localhost:11434/v1" \
       LLM_MODEL="openai:llama3"
```

### Switching modes on a running cluster

```bash
export LLM_API_KEY="sk-..."
task agent-configure-llm    # patches secret, toggles mock=false, restarts pod

task agent-use-mock         # switches back to mock mode
```

## Taskfile Reference

Every operation is a `task` target. Run `task --list` to see all available commands.

### Full pipeline

| Task | Description |
|---|---|
| `task all` | Cluster → monitoring → demo → agent → e2e tests |
| `task e2e-setup` | Everything except running the tests |
| `task teardown` | Destroy the Kind cluster |

### Individual components

| Task | Description |
|---|---|
| `task cluster` | Create the Kind cluster (`observability-e2e`) |
| `task monitoring` | Deploy kube-prometheus-stack (Prometheus + Alertmanager + Grafana) |
| `task demo` | Deploy OpenTelemetry Astronomy Shop Demo |
| `task agent-build` | Build the agent Docker image and load it into Kind |
| `task agent-deploy` | Deploy the agent with RBAC, ConfigMap, Secret, and Service |
| `task port-forward` | Forward agent:8080, demo:9090, prometheus:9091 to localhost |

### Fault injection

| Task | Description |
|---|---|
| `task inject-latency` | Trigger high latency on checkoutservice |
| `task inject-error` | Trigger error rate spike on cartservice |
| `task inject-crash` | Kill a pod to trigger restart detection |

### Testing

| Task | Description |
|---|---|
| `task e2e-test` | Run all 5 e2e tests |
| `task e2e-test-latency` | Run only the latency fault test |
| `task smoke-test` | Quick API checks (health, incidents, alerts, memory) |

### Agent management

| Task | Description |
|---|---|
| `task agent-logs` | Stream agent pod logs |
| `task agent-restart` | Restart the agent deployment |
| `task agent-configure-llm` | Inject real LLM API key, disable mock mode |
| `task agent-use-mock` | Switch back to mock mode |

### Diagnostics

| Task | Description |
|---|---|
| `task status` | Show cluster, pod, and alert state |
| `task stop-port-forward` | Kill all port-forward processes |

## E2E Test Scenarios

Five tests in `tests/e2e/`. Each test:

1. Injects a fault into the OTEL demo
2. Waits for Prometheus to detect the issue and fire an alert
3. Waits for Alertmanager to deliver the alert to the agent's webhook
4. Waits for the agent to investigate and produce an RCA
5. Asserts the RCA has a root cause, confidence > 0, and evidence

| Test | Fault | Alert | Assertions |
|---|---|---|---|
| `test_latency_fault_triggers_investigation` | 2s latency on checkoutservice | `HighLatency` | RCA has root cause, confidence > 0, evidence, ≥1 hypothesis |
| `test_latency_fault_multiple_services` | Latency on checkout + cart | `HighLatency` | Agent handles cascading alerts |
| `test_error_rate_fault_triggers_investigation` | 50% error rate on cartservice | `HighErrorRate` | RCA produced with evidence |
| `test_pod_crash_triggers_investigation` | Kill adservice pod | `PodRestarting` | RCA detects restart, confidence > 0 |
| `test_agent_health_after_fault` | (after any fault) | — | Agent health endpoint returns 200 |

### Running e2e tests manually

```bash
cd tests/e2e

# All e2e tests
AGENT_URL=http://localhost:8080 PROMETHEUS_URL=http://localhost:9091 \
  pytest . -v -m e2e --tb=long

# Single test
AGENT_URL=http://localhost:8080 PROMETHEUS_URL=http://localhost:9091 \
  pytest test_latency_fault.py -v --tb=long
```

### Test timeout

The default timeout is 300 seconds (5 minutes). Override with `E2E_TIMEOUT`:

```bash
E2E_TIMEOUT=600 task e2e-test
```

## How It Works

### Alert Flow

```
Fault injected
  → OTEL demo emits metrics (http_server_duration_bucket)
    → Prometheus scrapes every 15s
      → PrometheusRule evaluates (HighLatency for > 1m)
        → Alertmanager receives alert (group_wait: 10s)
          → Alertmanager POSTs to agent webhook (POST /alerts)
            → AlertReceiver creates Incident
              → Scheduler enqueues investigation
                → Coordinator runs investigation loop
                  → Agent API exposes RCA at GET /incidents/{id}
```

### Investigation Flow

```
open → acknowledge
  → explore (6 agents in parallel: Prometheus, Loki, Tempo, K8s, Deploy, Docs)
  → generate hypotheses (LLM)
  → investigate (loop)
    ├── gather targeted evidence (route by keyword heuristic)
    ├── evaluate hypotheses (LLM — confirm or refute)
    ├── prune refuted branches
    └── deepen confirmed leaves (LLM — child hypotheses)
  → produce RCA (LLM — narrative + remediation)
  → pending_approval
```

### Prometheus Rules

The e2e test deploys custom PrometheusRules in `deploy/monitoring/prometheus-rules.yaml`:

| Rule | Expression | Threshold |
|---|---|---|
| `HighLatency` | `histogram_quantile(0.99, rate(http_server_duration_bucket[5m]))` | > 500ms P99 |
| `HighErrorRate` | `rate(http_server_duration_count{status_code=~"5.."}[5m]) / rate(...)` | > 10% |
| `PodRestarting` | `rate(kube_pod_container_status_restarts_total[10m])` | > 0 |
| `HighCPU` | `rate(container_cpu_usage_seconds_total[5m]) * 100` | > 1% |

### Alertmanager Config

All alerts are routed to the agent's webhook:

```yaml
receivers:
  - name: 'observability-agent'
    webhook_configs:
      - url: 'http://observability-agent.observability-agent.svc.cluster.local:8000/alerts'
        send_resolved: true
```

## Cluster Topology

```
Namespace: monitoring
  ├── kube-prometheus-stack-prometheus      (metrics scraper)
  ├── kube-prometheus-stack-alertmanager    (alert router → agent webhook)
  └── kube-prometheus-stack-grafana         (dashboards, optional)

Namespace: otel-demo
  ├── checkoutservice                       (Go)
  ├── cartservice                           (.NET)
  ├── productcatalogservice                 (Go)
  ├── currencyservice                       (C++)
  ├── paymentservice                        (JavaScript)
  ├── shippingservice                       (Rust)
  ├── emailservice                          (Ruby)
  ├── adservice                             (Java)
  ├── frauddetectionservice                 (Kotlin)
  ├── recommendationservice                 (Python)
  ├── frontend                              (JavaScript)
  ├── flagd                                 (feature flags)
  ├── opentelemetry-collector               (telemetry pipeline)
  └── loadgenerator                         (synthetic traffic)

Namespace: observability-agent
  └── observability-agent                   (the agent, NodePort: 30080)
```

## Troubleshooting

### Agent not reachable

```bash
kubectl get pods -n observability-agent
kubectl logs -n observability-agent -l app=observability-agent
```

### Alerts not firing

```bash
# Check Prometheus rules
kubectl get prometheusrule -n monitoring

# Check current alerts
curl -s http://localhost:9091/api/v1/alerts | python -m json.tool

# Check Alertmanager config
kubectl get secret alertmanager-config -n monitoring -o jsonpath='{.data.alertmanager\.yaml}' | base64 -d
```

### Agent not receiving alerts

```bash
kubectl logs -n observability-agent -l app=observability-agent | grep -i alert
```

### OTEL demo not scraping metrics

The OTEL demo Helm chart includes a ServiceMonitor. Verify Prometheus can see it:

```bash
kubectl get servicemonitor -n otel-demo
curl -s http://localhost:9091/api/v1/targets | python -m json.tool | grep -A5 otel-demo
```

### Cluster resources exhausted

The full stack (monitoring + demo + agent) needs ~4GB RAM and ~4 CPU cores. Reduce the demo footprint by disabling non-essential services:

```bash
helm upgrade otel-demo open-telemetry/opentelemetry-demo \
  --namespace otel-demo \
  --set loadgenerator.enabled=false \
  --set jaeger.enabled=false \
  --set grafana.enabled=false
```

## CI/CD

The e2e tests are designed to run in CI. A minimal pipeline:

```yaml
# .github/workflows/e2e.yml
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -e ".[dev]"
      - run: task e2e-setup
        env:
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
      - run: task e2e-test
      - run: task teardown
        if: always()
```

Unit tests (no cluster needed) run in a separate lightweight job:

```yaml
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v -m "not e2e"
```