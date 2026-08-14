# Lesson 005: Live infrastructure has truths no code review finds

**Theme:** Verification — running the real thing
**Where this bit us:** `deploy/monitoring/kube-prometheus-values.yaml`, `tests/e2e/helpers.py` (flagd fault injection)
**Cost of finding it late:** every alert rule silently unable to fire; every fault-injection test silently unable to inject a fault

## What happened

Two infrastructure facts, each invisible until queried against a running cluster, each one that would have quietly broken the entire e2e suite if not caught:

**Prometheus was dropping the label every alert rule needed.** Prometheus' OTLP receiver drops resource attributes by default. Every metric the demo services' own SDKs emitted directly — `http_server_*`, `rpc_server_*` — arrived with no `service_name` label at all: from Prometheus' point of view, anonymous. No alert rule scoped to a service could ever match them. (The collector-generated spanmetrics were unaffected, because there `service.name` is an explicit dimension rather than a resource attribute — which is exactly why the gap was easy to miss: *some* metrics looked fine.) This wasn't a bug in the demo, or in this project's code — it's a Prometheus OTLP-receiver default, documented, correct-as-designed, and completely orthogonal to anything a code review of the alert rules would surface. Confirmed by querying the running Prometheus directly and seeing the empty label set; fixed with `otlp.promoteResourceAttributes` in the Helm values.

**Fault injection wasn't actually injecting anything, and the failure mode was a Kubernetes mechanics detail.** `kubectl patch` on the flagd feature-flag ConfigMap took server-side-apply field ownership of `.data`. The *next* `helm upgrade` of the demo then failed outright — "conflict occurred while applying object... conflict with kubectl-patch" — meaning the very first fault injected during any session broke every subsequent redeploy until someone deleted the ConfigMap by hand. No amount of reading the Taskfile or the test code would have predicted this; it only surfaced by actually flipping a flag and then trying to redeploy.

## Root cause

Both are true facts about real systems that exist nowhere in application code, nowhere in a test file, and nowhere a static review would look: one is a default baked into Prometheus' OTLP ingestion path, the other is a consequence of how Kubernetes' server-side-apply tracks field ownership across two different tools writing to the same object. Neither is documented anywhere a reviewer of *this* codebase would naturally end up. They're properties of the infrastructure this codebase runs on top of, and the only way to learn them is to run on top of it.

## The fix

- `otlp.promoteResourceAttributes: [service.name, ...]` in the Prometheus Helm values.
- The demo redeploy task now drops the flagd ConfigMap before `helm upgrade`, handing ownership back to Helm rather than fighting the SSA conflict.

Both are one-line fixes. Neither was findable without a live cluster.

## The generalizable lesson

**"It passed code review" and "it works" are different claims whenever the system under review is only half your own code.** The other half — the managed service, the Helm chart's defaults, the Kubernetes control plane's semantics — has behavior that's fully documented, entirely reasonable, and completely absent from the surface you're reviewing. This isn't specific to agents; it's true of any project with real infrastructure underneath it. What makes it sharper for agentic systems specifically: the *cost* of discovering these late is higher, because the symptom (an alert that never fires, a test that times out) looks identical to a dozen other explanations, and a real-LLM investigation is expensive to run repeatedly while narrowing down which one it is.

If you're building an agent that talks to real infrastructure: budget time to run the actual thing, end to end, against the actual dependencies, before trusting that a design which reads correctly *is* correct. The bugs that live review can't find are exactly the ones a live run exists to catch.
