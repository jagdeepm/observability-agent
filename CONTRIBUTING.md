# Contributing

Thanks for your interest. This is a small project — issues and PRs are welcome.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v -m "not e2e"
```

Requires Python ≥ 3.12. The unit suite is fully mocked — no network, no cluster, runs in under a second.

## Before opening a PR

- `pytest tests/ -v -m "not e2e"` passes.
- New behaviour has a test. Bug fixes have a test that fails without the fix.
- Keep the diff to the change you're making.

## Design constraints

Two decisions shape most of the codebase — please read the relevant ADR in [`docs/adr/`](docs/adr/) before working against them:

- **The coordinator is a rule-based state machine; the LLM only fills content** ([ADR-003](docs/adr/003-rule-based-coordinator-llm-content.md)). Control flow stays deterministic and testable.
- **Everything external is mocked in tests** ([ADR-005](docs/adr/005-mock-everything-testing.md)). No test may require a network call or a live cluster, except those marked `@pytest.mark.e2e`.

New observability sources should follow the existing agent protocol ([ADR-004](docs/adr/004-agent-protocol-pattern.md)): natural language → domain query language → MCP client → LLM summary → `Evidence`.

## End-to-end tests

The e2e suite deploys against a real Kind cluster running the OpenTelemetry Astronomy Shop demo, flips the demo's own fault flags, and asserts the agent investigates them. It takes 15–20 minutes on first run. See [`docs/e2e-testing.md`](docs/e2e-testing.md).

```bash
task env-init   # creates .env — put your LLM settings there
task all
```

`.env` holds credentials and is gitignored. Never commit it; `.env.example` is the only env file in the repo, and it carries placeholders only.

Chart versions for the demo and Tempo are pinned in `Taskfile.yml`. Upstream renames services and feature flags between majors, which silently breaks the alert rules and the fault helpers — bump the pin deliberately, and re-run `task telemetry-check` afterwards.

## Reporting a security issue

Please open a regular issue only for non-sensitive problems. For anything security-relevant, contact the maintainer directly rather than filing publicly.
