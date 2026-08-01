# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Toolchain skeleton only. `src/shortener` has a health endpoint; `src/orchestrator` is an
empty package. No shortener features and no orchestration engine exist yet — the brief at
`docs/Assignment Agentic-Proficient Software Engineer.pdf` is still the only spec.

Git is initialized on `main` with no commits yet. Per the user's global rules, Claude
commits only — the user handles push, remote setup, and destructive ops.

## Commands

Python 3.13 via `uv`. All commands run from the repo root; `.venv/bin/` prefixes avoid
needing an activated shell.

```bash
uv venv --python 3.13          # create the venv (once)
uv pip install -e ".[dev]"     # install project + dev extras, editable

.venv/bin/pytest                                  # full suite
.venv/bin/pytest tests/test_health.py             # one file
.venv/bin/pytest tests/test_health.py::test_health_returns_ok   # one test
.venv/bin/pytest -k health                        # by name pattern
.venv/bin/pytest --cov=src --cov-report=term-missing

.venv/bin/ruff check .         # lint
.venv/bin/ruff check --fix .   # lint + autofix
.venv/bin/ruff format .        # format

.venv/bin/uvicorn shortener.main:app --reload     # dev server on :8000
```

The editable install is what puts `shortener` and `orchestrator` on the path — tests import
`from shortener.main import app`, not via relative paths. A `ModuleNotFoundError` in pytest
almost always means the editable install is stale; re-run `uv pip install -e ".[dev]"`.

## Stack and why

| Choice | Rationale |
| --- | --- |
| Python 3.13 | `asyncio` gives parallel DAG branches with synchronization directly, no extra runtime |
| FastAPI + Uvicorn | Pydantic models double as the API/schema definitions the brief asks for; OpenAPI generated, not written |
| SQLAlchemy 2 + SQLite | Single-file DB keeps the prototype runnable end-to-end with zero setup; the ORM keeps a Postgres swap open |
| pytest + httpx | `TestClient` covers integration tests through the real ASGI app, not mocks |
| ruff | Lint + format in one tool; the orchestrator can shell out to it as a machine-checkable quality gate |
| **Hand-rolled DAG engine** | No LangGraph/CrewAI — see D1 |

**`docs/architecture.md` is the authoritative design and holds the decision registry
(D1–D15).** Read it before making architectural changes; add new decisions there, not here.

The rules from it that bite most often while coding:

- `orchestrator` must never import `shortener` (D3) — generality has to stay checkable.
- Exit gates are evaluated by a non-producer, preferably a real tool (D4). An agent
  reporting its own success is not a gate.
- Acceptance tests are authored before implementation, by a different agent, and must fail
  first (D5). `tests/` is write-protected during repair loops (D6).
- Derive rather than generate whenever a contract exists (D8).

## What is being built

Two layers, and conflating them is the main failure mode:

1. **The product** — a URL shortener service: shorten/resolve APIs, redirect handling,
   click analytics, and reliability features (rate limiting, collision handling, expiry).
2. **The agentic orchestration layer** — the actual subject of the assignment. The
   shortener is the *workload* that the orchestrator drives through an SDLC; it is the
   demo, not the deliverable. The brief names orchestration the "critical differentiator"
   and weights evaluation accordingly.

When scoping work, ask which layer a task belongs to. Polishing shortener features at the
expense of orchestration depth scores badly against the stated criteria.

## Orchestration requirements that constrain the design

These come from §4.4 of the brief and are non-negotiable design inputs — architecture
choices should be traceable to them:

- **Explicit dependency graph** over SDLC stages (requirements → architecture/design →
  implementation → testing → documentation → release readiness), with **entry/exit gates**
  per node. Not a linear chain of prompts.
- **Non-linear, stateful execution**: parallel paths with synchronization points, plus
  **dynamic re-planning** when an upstream output changes (downstream nodes must invalidate
  and re-run under governance).
- **Cross-stage context and decision lineage** preserved — every artifact traceable to the
  decision and stage that produced it.
- **Human approval checkpoints** on high-impact actions; agents run under explicit autonomy
  boundaries.
- **Failure controls**: bounded retries, fallback, rollback, safe-stop.
- **Policy guardrails** for security, compliance, and change control, enforced at gates.
- **Audit-grade observability** plus reliability metrics: success rate, retry/rollback
  frequency, MTTR, end-to-end latency.

## Required deliverables

The submission is judged on all five; missing ones are missing points:

- Working prototype, runnable end-to-end
- Architecture overview (components, orchestration model, control flow, key decisions)
- **Three worked scenarios** — greenfield, brownfield, ambiguous — each showing
  decomposition, orchestration, and validation. The ambiguous one must show ambiguity
  being *identified and normalized*, not silently resolved.
- Setup instructions
- Testing approach, limitations, and trade-offs
- Final engineering summary: plan/rationale, artifacts, risks/trade-offs/validation,
  assumptions, limitations

The brownfield scenario needs a real prior state to enhance/refactor, so sequence the work
so an earlier version of the shortener exists to modify.

## Working conventions

- Documentation lives under `docs/`. The assignment PDF there is the source of truth for
  scope — re-read it before making scoping decisions rather than relying on this summary.
- Decisions worth defending in review get a sequential ID and go in the registry at
  `docs/architecture.md` §9 — rationale *and* cost accepted, so the "clarity and
  defensibility of decisions" criterion has something concrete to point at.
- Assumptions made in the face of ambiguity get written down explicitly rather than
  resolved silently — the brief grades assumption-surfacing directly.
