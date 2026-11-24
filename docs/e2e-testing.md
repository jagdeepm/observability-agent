# End-to-End Testing

The observability agent ships with a full end-to-end test harness that deploys the agent against a real Kubernetes cluster running the [OpenTelemetry Astronomy Shop Demo](https://github.com/open-telemetry/opentelemetry-demo) — a 12-service microservices application with pre-instrumented telemetry. The e2e tests inject faults (latency spikes, error rates, pod crashes), verify that Prometheus fires alerts, and assert that the agent autonomously investigates and produces a root cause analysis.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Kind Cluster (observability-e2e)                                  │
│                                                                    │
│  ┌───────────────────┐         ┌───────────────────────────────┐  │
│  │ OTEL Demo         │  OTLP   │ monitoring                    │  │
│  │  (ns: otel-demo)  │ metrics │                               │  │
│  │                   │────────▶│ Prometheus  ← alert rules     │  │
│  │  cart   checkout  │  OTLP   │ Alertmanager ─┐               │  │
│  │  payment  ad      │ traces  │ Tempo         │               │  │
│  │  shipping  email  │────────▶│ Loki  ◀── Promtail (pod logs) │  │
│  │  frontend  ...    │         │ Grafana       │               │  │
│  │                   │         └───────────────┼───────────────┘  │
│  │  flagd ← faults   │                         │ webhook          │
│  └───────────────────┘                         ▼                  │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ observability-agent                                          │ │
│  │   POST /alerts     ← Alertmanager webhook                    │ │
│  │   GET  /incidents  → investigation results (RCA + signals)   │ │
│  │   GET  /health     → health check                            │ │
│  │                                                              │ │
│  │   Coordinator → 6 agents → Prometheus / Loki / Tempo / K8s   │ │
│  │                 ↳ deterministic analysis → LLM → Evidence    │ │
│  └──────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────┘
      │              │              │            │            │
 localhost:8080  :9090         :9091        :3100        :3200
  (agent API)   (demo UI)   (Prometheus)    (Loki)      (Tempo)
```

The demo ships its own Prometheus, Jaeger, Grafana and OpenSearch. All four are
disabled in `deploy/monitoring/otel-demo-values.yaml` and the collector's exporters
are re-targeted at the monitoring stack above — otherwise both telemetry pipelines
export into services that do not exist, and no latency or error alert can fire.

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

# Configure the LLM
task env-init          # creates .env from .env.example
$EDITOR .env           # fill in LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
task llm-check         # confirm it works before building a cluster

# Run everything (cluster → monitoring → loki → tempo → demo → agent → tests)
task all
```

This takes ~15–20 minutes on first run (pulling images, building the agent). Subsequent runs are faster.

Without an `LLM_API_KEY` the agent still deploys, in mock mode — useful for checking the cluster wiring, but the investigations are canned.

## Configuring the LLM

Every `task` target loads `.env` automatically. A variable already exported in your shell wins over the file, so CI can inject secrets without writing one.

```dotenv
# .env
LLM_BASE_URL=https://litellm.example.com/v1
LLM_API_KEY=sk-litellm-xxxxxxxxxxxx
LLM_MODEL=claude-sonnet-4-5
```

`LLM_API_KEY` and `LLM_BASE_URL` become a Kubernetes Secret; the pod reads them via `secretKeyRef`. `LLM_MODEL` is patched into the `agent-config` ConfigMap. `LLM_MOCK` is derived — mock mode iff no API key is set.

### LiteLLM proxy

The expected setup. Point `LLM_BASE_URL` at the proxy's OpenAI-compatible route (the `/v1` suffix matters), use a virtual key, and name the model exactly as it appears in the proxy's `model_list`:

```dotenv
LLM_BASE_URL=https://litellm.internal/v1
LLM_API_KEY=sk-litellm-...
LLM_MODEL=claude-sonnet-4-5
```

Model names pass through verbatim unless they look like `provider:model`, in which case the prefix is stripped — so `claude-sonnet-4-5` and `anthropic/claude-sonnet-4-5` both reach the proxy unchanged, while `openai:gpt-4o` arrives as `gpt-4o`.

### Other providers

Any OpenAI-compatible API works:

```dotenv
# OpenAI
LLM_API_KEY=sk-...
LLM_MODEL=openai:gpt-4o

# Local model (Ollama, vLLM, LM Studio)
LLM_API_KEY=not-needed
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=openai:llama3
```

### Verifying the endpoint

Run this before `task all` — it takes seconds and catches the mistakes that otherwise surface twenty minutes into a cluster build:

```bash
task llm-check     # endpoint reachable, model name valid, structured output works
task llm-models    # list the model names the endpoint offers
```

The third check is the one that matters. The agent never asks for free text — every call goes through `generate_structured` with a Pydantic schema, which needs tool calling or JSON-schema support all the way through the proxy to the model. An endpoint can answer `/v1/models` happily and still be unusable here.

It also flags a slow endpoint: an investigation makes roughly ten to a few dozen calls depending on how wide the hypothesis tree grows within its cap (see "Known limitations" below), so anything over ~20s per call needs a raised `E2E_TIMEOUT`.

### Switching modes on a running cluster

```bash
task agent-configure-llm    # re-reads .env, patches secret + configmap, restarts the pod
task agent-use-mock         # switches back to mock mode
```

## Iterating locally

The full loop — docker build, `kind load`, rollout — is several minutes. For prompt and LLM work, run the agent on your machine against the port-forwarded backends instead:

```bash
task port-forward     # agent, demo, Prometheus, Loki, Tempo
task run-local        # agent on http://127.0.0.1:8081, reading .env

# in another shell — Alertmanager cannot reach your laptop, so feed it by hand
task send-test-alert
task send-test-alert SERVICE=checkout ALERTNAME=HighLatency

curl -s http://127.0.0.1:8081/incidents | python3 -m json.tool
```

This queries the same real Prometheus, Loki and Tempo the in-cluster agent uses. Only the agent process is local.

## Taskfile Reference

Every operation is a `task` target. Run `task --list` to see all available commands.

### Full pipeline

| Task | Description |
|---|---|
| `task all` | Cluster → monitoring → loki → tempo → demo → agent → e2e tests |
| `task e2e-setup` | Everything except running the tests |
| `task teardown` | Destroy the Kind cluster |

### Individual components

| Task | Description |
|---|---|
| `task env-init` | Create `.env` from `.env.example` |
| `task cluster` | Create the Kind cluster (`observability-e2e`) |
| `task monitoring` | Deploy kube-prometheus-stack (Prometheus + Alertmanager + Grafana) |
| `task loki` | Deploy Loki + Promtail (ships every pod's stdout) |
| `task tempo` | Deploy Tempo (trace backend for the TraceAgent) |
| `task demo` | Deploy OpenTelemetry Astronomy Shop Demo |
| `task agent-build` | Build the agent Docker image and load it into Kind |
| `task agent-deploy` | Deploy the agent with RBAC, ConfigMap, Secret, and Service |
| `task port-forward` | Forward agent:8080, demo:9090, prometheus:9091, loki:3100, tempo:3200 |

### Fault injection

Faults come from the demo's own flagd feature flags, so the failures are real: the service genuinely errors, genuinely logs, genuinely shows up in traces.

| Task | Description |
|---|---|
| `task list-faults` | List every flag in the deployed demo and its current variant |
| `task inject-fault -- cartFailure 100%` | Flip a flag |
| `task reset-faults` | Return every flag to its shipped default |
| `task inject-crash` | Kill an ad pod to trigger restart detection |

**Flipping a flag restarts flagd.** The flag lives in the `flagd-config` ConfigMap, but flagd does not read it from there — an initContainer copies the file into an `emptyDir` at pod start and flagd watches that copy. Patching the ConfigMap alone is inert, so every write is followed by `kubectl rollout restart deployment/flagd`. Budget ~15 seconds per flag change, and expect a few seconds of flag-evaluation blips in the demo while the pod comes back.

### Testing

| Task | Description |
|---|---|
| `task e2e-test` | Run all e2e tests |
| `task e2e-test-one -- tests/e2e/test_error_faults.py` | Run a subset |
| `task smoke-test` | Quick API checks (health, incidents, alerts, memory) |

### Agent management

| Task | Description |
|---|---|
| `task agent-logs` | Stream agent pod logs |
| `task agent-restart` | Restart the agent deployment |
| `task llm-check` | Verify the endpoint, model name, and structured output |
| `task llm-models` | List the models the endpoint offers |
| `task agent-configure-llm` | Re-read `.env`, patch secret + configmap, restart the pod |
| `task agent-use-mock` | Switch back to mock mode |
| `task run-local` | Run the agent on your machine against the port-forwards |
| `task send-test-alert` | POST a sample Alertmanager payload to the local agent |

### Diagnostics

| Task | Description |
|---|---|
| `task telemetry-check` | Verify demo telemetry is reaching Prometheus, Loki and Tempo |
| `task discover-metrics` | Show the metric and service names Prometheus actually has |
| `task status` | Show cluster, pod, and alert state |
| `task stop-port-forward` | Kill all port-forward processes |

## E2E Test Scenarios

Ten tests in `tests/e2e/`, grouped by fault class. Each one:

1. Flips a flagd feature flag (or kills a pod)
2. Waits for Prometheus to detect the issue and fire an alert
3. Waits for Alertmanager to deliver the alert to the agent's webhook
4. Waits for the agent to investigate and produce an RCA
5. Asserts the RCA has a root cause, confidence > 0, evidence — and where noted, specific **computed signals**

| Test | Fault | Alert | Also asserts |
|---|---|---|---|
| `test_cart_failure_triggers_investigation` | `cartFailure=100%` | `HighErrorRate` | `error_count` signal present |
| `test_payment_failure_triggers_investigation` | `paymentFailure=100%` | `HighErrorRate` | non-empty causal chain |
| `test_product_catalog_failure_triggers_investigation` | `productCatalogFailure=on` | `HighErrorRate` | `error_count` signal present |
| `test_image_slow_load_triggers_investigation` | `imageSlowLoad=10sec` | `HighLatency` | `p95` signal present |
| `test_shipping_slowdown_surfaces_slowest_span` | `intlShippingSlowdown=10sec` | `HighLatency` | `slowest_self_ms` signal present |
| `test_load_spike_with_cart_errors` | `loadGeneratorVUs=50` + `cartFailure=50%` | `HighLatency` | agent picks a root cause under two faults |
| `test_ad_high_cpu_triggers_investigation` | `adHighCpu=on` | `HighCPU` | non-HTTP fault class |
| `test_email_memory_leak_triggers_restart` | `emailMemoryLeak=1000x` | `PodRestarting` | OOMKill detected |
| `test_pod_crash_triggers_investigation` | `kubectl delete pod` | `PodRestarting` | restart detected |
| `test_signals_are_computed_from_real_payloads` | `cartFailure=100%` | `HighErrorRate` | signals exist and survive JSON |
| `test_agent_health_after_fault` | (after any fault) | — | health endpoint returns 200 |

### Asserting on signals, not prose

The interesting assertion is `expect_signals`. The agent computes facts from telemetry in code (`analysis.py`) and attaches them to each Evidence, and `analysis.py` is deliberately forgiving: an unrecognised payload shape falls back to a truncated dump with no signals rather than failing the investigation.

That fallback is invisible in unit tests, which supply payloads of a known shape. So the e2e assertion is: after a real fault, at least one Evidence carries the expected computed key. If Tempo starts returning span attributes in a shape the parser does not know, `slowest_self_ms` disappears and `test_shipping_slowdown_surfaces_slowest_span` fails — which is the only place that failure is detectable.

### Running e2e tests manually

```bash
# All e2e tests
AGENT_URL=http://localhost:8080 PROMETHEUS_URL=http://localhost:9091 \
  pytest tests/e2e -v -m e2e --tb=long

# One file
task e2e-test-one -- tests/e2e/test_latency_faults.py
```

### Test timeout

The example ships `E2E_TIMEOUT=1800` (30 minutes). That's not a guess — see "Known limitations" below for the measurement behind it. Override per run if needed:

```bash
E2E_TIMEOUT=900 task e2e-test
```

Expect the full suite to take well over an hour against a real LLM — each scenario waits out a 1-minute `for:` clause plus alert delivery plus an investigation, and investigations vary widely in how long they take (see below).

## Known limitations

**Hypothesis breadth is now capped** (`docs/specs/investigation-cost-guardrails.md`) — `MAX_HYPOTHESES_PER_GENERATE`, `MAX_CHILDREN_PER_DEEPEN`, and `MAX_DEEPEN_PER_LEVEL` in `coordinator/engine.py`, next to the existing `MAX_DEPTH`. Worst case across three depth levels is now roughly 53 hypotheses, down from the 100–200+ observed uncapped. When there are more confirmed leaves than the level allows, only the most-confident ones get deepened — that selection is what actually bounds cost, since a deepen call happens once per leaf rather than in a single batch. This has not yet been re-measured against a live cluster; `E2E_TIMEOUT` stays at 1800s until it has.

Evidence gathering across a level also runs concurrently (`_gather_targeted_evidence`, mirroring the exploratory-evidence phase), which independently cut typical investigation time roughly 10× — a cart-failure investigation that previously took 20–40 minutes completed in ~4 minutes in the common case even before the breadth cap landed.

**Every LLM call has a 120-second timeout** (`PydanticAIProvider`, `infrastructure/llm_provider.py`) as a backstop against a stalled proxy — a call that never returns fails cleanly instead of hanging an investigation forever.

**An optional cumulative token budget** (`LLM_TOKEN_BUDGET_DAILY` / `LLM_TOKEN_BUDGET_WEEKLY` in `.env`) bounds total spend independent of any single investigation's size — see `docs/specs/investigation-cost-guardrails.md`, Part 2. Unset by default. In-memory only: it resets on pod restart, a deliberate limitation stated in the spec rather than fixed.

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

`deploy/monitoring/prometheus-rules.yaml` defines two groups. Recording rules normalise the source metrics; alerts read the normalised series.

| Recording rule | Source |
|---|---|
| `otel:service_latency_p99_seconds` | spanmetrics `traces_span_metrics_duration_*_bucket`, falling back to `http_server_request_duration_seconds_bucket` |
| `otel:service_error_ratio` | spanmetrics `traces_span_metrics_calls_total` by `status_code` |

The indirection is not decoration. The demo spans nine languages at different semconv versions, and the spanmetrics connector is mid-migration from milliseconds to seconds — so the source metric name is genuinely uncertain until the stack is running. The recording rule accepts whichever series exists (`or` takes the first non-empty vector). Run `task discover-metrics` to see which one is live.

| Alert | Expression | Threshold | `for` |
|---|---|---|---|
| `HighLatency` | `otel:service_latency_p99_seconds` | > 0.5s | 1m |
| `HighErrorRate` | `otel:service_error_ratio` | > 10% | 1m |
| `PodRestarting` | `rate(kube_pod_container_status_restarts_total[10m])` | > 0 | 1m |
| `HighCPU` | `rate(container_cpu_usage_seconds_total[5m])` | > 0.4 cores | 1m |
| `PodNotReady` | `kube_pod_status_ready{condition="true"}` | == 0 | 2m |

The last three read kube-state-metrics and cAdvisor rather than the demo's instrumentation, so they still fire when the OTLP pipeline is misconfigured — useful as a control.

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

Service names below are from demo chart `0.41.0` (app 3.0.0), pinned in the Taskfile. Earlier majors used a `…service` suffix (`cartservice`, `checkoutservice`); the alert rules and tests are written against the current names, which is why the version is pinned rather than tracking latest.

```
Namespace: monitoring
  ├── kube-prometheus-stack-prometheus      (metrics, OTLP receiver enabled)
  ├── kube-prometheus-stack-alertmanager    (alert router → agent webhook)
  ├── kube-prometheus-stack-grafana         (dashboards, optional)
  ├── loki + promtail                       (pod logs)
  └── tempo                                 (traces; OTLP gRPC :4317, query :3200)

Namespace: otel-demo
  ├── cart                (.NET)        ├── frontend        (JavaScript)
  ├── checkout            (Go)          ├── frontend-proxy  (Envoy)
  ├── product-catalog     (Go)          ├── image-provider  (nginx)
  ├── currency            (C++)         ├── quote           (PHP)
  ├── payment             (JavaScript)  ├── accounting      (.NET)
  ├── shipping            (Rust)        ├── flagd           (feature flags)
  ├── email               (Ruby)        ├── kafka, valkey-cart, astronomy-db
  ├── ad                  (Java)        ├── otel-collector  (telemetry pipeline)
  ├── fraud-detection     (Kotlin)      └── load-generator  (synthetic traffic)
  └── recommendation      (Python)

Namespace: observability-agent
  └── observability-agent                   (the agent, NodePort: 30080)
```

The demo's `agent`, `chatbot` and `mcp` components are disabled — they are LLM demos needing their own credentials, and nothing here investigates them.

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

### Demo telemetry not reaching the monitoring stack

**Start here.** This is the failure that costs the most time, because it presents as every latency and error test timing out after five minutes — which looks like an agent bug and is not.

```bash
task telemetry-check    # fails loudly if metrics, traces or logs are missing
task discover-metrics   # shows which source metric the recording rules are using
```

If Prometheus has no `service_name` values, the demo's collector is not exporting into it:

```bash
kubectl logs -n otel-demo -l opentelemetry.io/name=otel-collector | grep -i "export\|error"

# Confirm the exporter endpoints were actually overridden
kubectl get cm otel-collector -n otel-demo -o yaml | grep -A2 "otlp_http/prometheus\|otlp_grpc/jaeger"
```

Both should point at `…monitoring.svc.cluster.local`, not at `prometheus:9090` / `jaeger:4317`. If they still point at the chart defaults, `deploy/monitoring/otel-demo-values.yaml` was not applied — re-run `task demo`.

If Tempo is empty but Prometheus has data, only the trace exporter is misrouted; the TraceAgent will return error Evidence and `slowest_self_ms` assertions will fail.

### Cluster resources exhausted

The full stack (monitoring + Loki + Tempo + demo + agent) needs roughly 8GB RAM and 6 CPU cores. The Taskfile starts Colima with 6 CPU / 12 GiB by default; override with `COLIMA_CPUS` and `COLIMA_MEMORY`.

To shrink the demo, disable services no test touches by adding them to `deploy/monitoring/otel-demo-values.yaml`:

```yaml
components:
  accounting: {enabled: false}
  fraud-detection: {enabled: false}
  quote: {enabled: false}
```

Do not disable `load-generator` — with no traffic, nothing produces telemetry and every test times out.

Note that `test_email_memory_leak_triggers_restart` is deliberately an OOM. On a memory-tight machine it can evict neighbouring pods; run it on its own if the suite gets flaky:

```bash
task e2e-test-one -- tests/e2e/test_resource_faults.py
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
          LLM_BASE_URL: ${{ secrets.LLM_BASE_URL }}
          LLM_MODEL: ${{ vars.LLM_MODEL }}
      - run: task telemetry-check
      - run: task e2e-test
      - run: task teardown
        if: always()
```

No `.env` is needed in CI — environment variables take precedence over the file, and every task reads them the same way.

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