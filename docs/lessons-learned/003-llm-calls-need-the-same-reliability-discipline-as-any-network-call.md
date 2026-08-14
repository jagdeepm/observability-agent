# Lesson 003: LLM calls need the same reliability discipline as any other network call

**Theme:** Reliability engineering for agents
**Where this bit us:** `infrastructure/llm_provider.py`, `infrastructure/scheduler.py`
**Cost of finding it late:** a 40+ minute hang with zero diagnostic signal

## What happened

Every infrastructure client in this codebase sets an explicit `httpx` timeout — Prometheus, Loki, Tempo, Kubernetes, GitHub, all `timeout=30.0`. The one client that talks to the LLM didn't. It was easy to not notice, because "the LLM might legitimately take a while" feels like a reason to leave it *unbounded* rather than a reason to bound it *generously*.

Live-reproduced: an investigation got stuck on a single LLM call for over 40 minutes. `kubectl exec` into the pod and read `/proc/net/tcp` directly — one connection to the LiteLLM proxy, state `ESTABLISHED`, genuinely open, genuinely waiting. Not a crash, not an exception, not a log line. The coordinator had no way to distinguish "still thinking" from "will never return," because nothing was watching the clock.

A second, related gap: the Scheduler retried *every* failure the same way — up to three times, re-queued at the front of the FIFO queue. That's the right policy for a transient network blip. It's actively harmful for a failure that's going to happen identically every time (a malformed config, later: an exhausted budget) — three guaranteed-useless retries, each one head-of-line-blocking every other incident waiting behind it.

## Root cause

The LLM call was treated as conceptually different from every other network call in the system — slower, more important, somehow exempt from the defensive patterns applied everywhere else. It isn't. It's an HTTP request to a service outside your process, subject to the same failure modes as any other: it can hang, the proxy can be overloaded, the network can drop a packet. The fact that its *normal* latency is higher (seconds, not milliseconds) doesn't mean its *unbounded* latency is acceptable — it means the timeout needs to be generous, not absent.

## The fix

- `PydanticAIProvider`'s `httpx.AsyncClient` now sets `timeout=httpx.Timeout(120.0, connect=10.0)` — generous enough that a legitimately large prompt still finishes, bounded enough that a hang becomes a caught exception within two minutes instead of running forever.
- The Scheduler gained a specific `except LLMBudgetExceededError` clause, checked *before* the general `except Exception`, that skips the retry-and-requeue path entirely for failures that are going to recur identically — flag the incident, move on, don't block the queue behind three doomed attempts.

## The generalizable lesson

**"This call is important and might take a while" is an argument for a generous timeout, not for no timeout.** Every external dependency in an agentic system — the model included — needs the same defensive posture: a bound on how long you'll wait, and a retry policy that knows the difference between "might succeed on attempt two" and "will fail the same way every time until something external changes." The LLM call is usually the newest, least-battle-tested integration in the system, and it's tempting to treat it as a special case exempt from patterns you'd apply without thinking anywhere else. It isn't special. Apply the patterns.

If you're building an agent: grep your codebase for every place you construct an HTTP client, and confirm the one talking to the model has a timeout too. It's an easy one to miss precisely because it feels different.
