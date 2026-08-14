#!/usr/bin/env python3
"""Verify the configured LLM endpoint before spending 20 minutes on a cluster.

Checks three things in order, because they fail for different reasons:

  1. the endpoint answers and the key is accepted   (/v1/models)
  2. LLM_MODEL is one of the names it offers        (a typo here is silent
     until the agent's first call fails mid-investigation)
  3. structured output works through the agent's own provider

Step 3 is the one that matters. The agent never asks for free text — every call
goes through `generate_structured` with a Pydantic schema, which needs tool
calling or JSON-schema support all the way through the proxy to the model. An
endpoint can pass steps 1 and 2 and still be useless for this.

Usage: task llm-check          run all three checks
       task llm-models         just list what the endpoint offers (--list)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "openai:gpt-4o")


def fail(msg: str) -> None:
    print(f"  FAIL — {msg}")
    sys.exit(1)


def list_models() -> list[str]:
    import httpx

    if not API_KEY:
        fail("LLM_API_KEY is not set. Run 'task env-init' and fill in .env.")

    url = f"{BASE_URL}/v1/models" if BASE_URL else "https://api.openai.com/v1/models"
    try:
        resp = httpx.get(url, headers={"Authorization": f"Bearer {API_KEY}"}, timeout=30)
    except Exception as exc:
        fail(f"cannot reach {url} — {type(exc).__name__}: {exc}")

    if resp.status_code == 401:
        fail(f"{url} rejected the key (HTTP 401). Check LLM_API_KEY.")
    if resp.status_code != 200:
        fail(f"{url} returned HTTP {resp.status_code}: {resp.text[:200]}")

    return sorted(m["id"] for m in resp.json().get("data", []))


async def check_structured_output() -> None:
    from observability_agent.agents.metrics import PromQLOutput
    from observability_agent.infrastructure.llm_provider import PydanticAIProvider

    llm = PydanticAIProvider()
    started = time.monotonic()
    out = await llm.generate_structured(
        prompt="Query: what is the p95 latency for the cart service?\nService: cart\nTranslate to PromQL.",
        output_schema=PromQLOutput,
        system_prompt="You are a Prometheus/PromQL expert. Translate natural language into valid PromQL.",
    )
    elapsed = time.monotonic() - started
    print(f"  OK — structured output in {elapsed:.1f}s")
    print(f"       promql: {out.promql}")
    if elapsed > 20:
        print(f"       NOTE: {elapsed:.0f}s per call. An investigation makes ~10 —")
        print("       raise E2E_TIMEOUT in .env or the e2e tests will time out.")


def main() -> None:
    print(f"Endpoint: {BASE_URL or '(provider default)'}")
    print(f"Model:    {MODEL}\n")

    if "--list" in sys.argv:
        for name in list_models():
            print(f"  {name}")
        return

    print("[1/3] endpoint reachable and key accepted")
    models = list_models()
    print(f"  OK — {len(models)} models available")

    print("\n[2/3] configured model is offered")
    # "provider:model" is stripped before the request, the same way the provider does.
    wire_name = MODEL.split(":", 1)[-1] if ":" in MODEL and "/" not in MODEL else MODEL
    if wire_name in models:
        print(f"  OK — {wire_name}")
    else:
        print(f"  FAIL — {wire_name!r} is not offered by this endpoint.")
        print("  Available:")
        for name in models:
            print(f"    {name}")
        sys.exit(1)

    print("\n[3/3] structured output through the agent's provider")
    asyncio.run(check_structured_output())

    print("\nLLM is ready. Next: task e2e-setup")


if __name__ == "__main__":
    main()
