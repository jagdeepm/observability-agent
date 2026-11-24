# Lesson 006: An end-to-end failure doesn't say which layer failed, so prove the harness before debugging the agent

**Theme:** Testing agentic systems
**Where this bit us:** `tests/e2e/helpers.py` (`set_flag`, `wait_for_incident`, `assert_rca`), `tests/e2e/test_error_faults.py`
**Cost of finding it late:** 9 E2E attempts at 4–10 minutes each, against a real LLM, to get one pass. Three agent-side changes were made before anyone had seen the evidence the failing assertion was about, and one of them was wrong.

## What happened

Two E2E tests failed with symptoms that looked like agent bugs. The cart test reported `Expected computed signals ['error_count'] on the RCA evidence, got none`. The product-catalog test hit a 600s timeout. Both read as "the agent didn't find the errors", so the debugging went into the agent.

Two fixes went into the agent first. The RCA report now included exploratory evidence, and LogQL filters became case-insensitive. Both were real bugs, but neither made the test pass. A third change filtered incidents by title, and it made things worse: two more runs timed out. Only the fourth step, dumping every evidence item when the assertion fails, showed what was actually going on. The first run with that dump explained the failure. The cart test passed on the next run, in 95s.

The product-catalog test then showed the same pattern. Its timeout wasn't the agent being slow. The alert never fired and no incident was ever opened, because the fault injection did nothing. `set_flag()` changed the flag's `defaultVariant`, but `productCatalogFailure` ships a `targeting` rule that outranks the default and resolves to `off` for every product. With the fault "on" for about 4 minutes, product-catalog produced zero error spans and zero error log lines. The test could not have passed, whatever the agent did.

Once the fault actually worked, the evidence dump exposed the rest in a single run. The test picked up `frontend-proxy: HighErrorRate`, not the product-catalog incident, because the fault cascades upstream and the first alert names the incident. On that incident none of the six evidence sources returned anything usable:

- **Prometheus:** the LLM-written query used metric names that don't exist.
- **Loki:** no lines for that service.
- **Tempo:** the LLM-written TraceQL was rejected with `400 Bad Request`.
- **Kubernetes:** every query failed with `CERTIFICATE_VERIFY_FAILED`.
- **Deployment and documentation:** both unconfigured.

## Root cause

The test made one assertion at the far end of a long chain: fault injection, then telemetry, then alert rule, then grouping alerts into an incident, then six LLM-written queries, then LLM hypothesis confirmation, then the RCA report. A failure anywhere in that chain produced the same message. The test also resolved the incident during cleanup, so no evidence was left afterwards to say where in the chain it broke. Each guess therefore cost a full 4–10 minute run with the real LLM.

The test's own assumptions were also untested. It assumed "inject a fault" means errors appear, and that "the first new incident" is about the faulted service. Faults cascade, so the second assumption was wrong for both tests, and the first was wrong for one of them.

## The fix

- `assert_rca()` prints the incident id, title, hypothesis count and every evidence item (source, query, summary, signals) when the signal assertion fails. That is commit `fcbc4ba`, together with the two agent fixes.
- `set_flag()` drops a flag's `targeting` rule, so the chosen variant applies. There is a unit test in `tests/test_e2e_helpers.py`.
- Still to do: a fault check with no agent (inject the fault, assert the expected error metric and alert appear), live checks per agent, and picking the incident by its linked alerts, not by arrival order.

## The generalizable lesson

**When an end-to-end test fails, the first question is not "what's wrong with the system" but "which layer failed, and can this test even tell me?"** A long pipeline checked by one assertion at the end is a smoke test, not a diagnostic. If the only feedback is pass/fail, every failure starts another round of guessing, and with a real LLM in the loop each round is slow, costs money, and isn't repeatable. The harness is code too, so it needs its own tests. A fault injector that injects nothing, or an oracle that checks the wrong incident, produces failures that look exactly like agent bugs.

If you're building an agent:

- **Test the layers bottom-up, fastest first.** First check that the fault actually breaks something, with no agent and no LLM. Then check that each tool works against the live backend. Only then run the full investigation.
- **Make every E2E failure self-describing.** Dump the full evidence before cleanup.
- **Don't change the agent until you've seen what the failing assertion was looking at.**
