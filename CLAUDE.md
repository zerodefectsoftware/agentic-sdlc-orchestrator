# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**A live greenfield run is parked mid-flight — resume it, do not start a new one.**
Run `610c782beb9a4ea6bc7c8d06444eb432`, status BLOCKED, in `runs/`. Everything through
`tests-acceptance` is green and cost real model calls; only implementation is outstanding:

| Node | State | Artifact worth reading |
| --- | --- | --- |
| intake → normalize-clarification | passed | `intake.register` v1 → v3 (analyst → policy → human answers) |
| design | passed | `design.spec@v2` — v1 was **rejected**, reasons in the approval trail |
| tests-acceptance | passed | 38 tests, 20 criteria, RED gate held |
| scaffold, impl | **pending** | re-run these |
| tests, docs, security, release-readiness, accept | pending | |

To continue: `.venv/bin/orchestrator resume 610c782beb9a4ea6bc7c8d06444eb432`. Needs
`ANTHROPIC_API_KEY` in `.env` (already present) and `ORCHESTRATOR_WORKER=live`. Expect one
long implementer session — the whole target, up to 200 turns and an hour.

`target/` is back to `target/tests/` (conftest + the agent-written suite) with no
`shortener/` package: the previous wave's module code had no lineage behind it (a scheduler
bug, since fixed) and was deleted. `scaffold` recreates the packages.

The `impl:*` fan-out children and their escalation are SKIPPED — they belong to a plan shape
that no longer exists (D23: greenfield uses a single implementer, brownfield keeps the
fan-out).

`docs/observing-a-run.md` says where every node's output lands and what to read at each step.

**Known gaps, in the order they will bite:**

- The scope guard has never been observed *refusing* a write in a live run. Every write so
  far has been in scope, so D7's enforcement is demonstrated only by construction.
- A code-agent session records nothing until it ends, and mid-wave state is invisible while
  a wave is open. Fine for a demo, wrong for audit-grade observability.
- Nothing has run past `impl`: `tests`, `docs`, `security`, release readiness and the
  evidence bundle are all unexercised live.
- Brownfield and ambiguous have never been run at all.

## Commands

Python 3.13 via `uv`. All commands run from the repo root; `.venv/bin/` prefixes avoid
needing an activated shell.

```bash
uv venv --python 3.13          # create the venv (once)
uv pip install -e ".[dev]"     # install project + dev extras, editable

.venv/bin/pytest                                  # orchestrator suite
.venv/bin/pytest target/tests                     # target suite (agent-written)
.venv/bin/pytest tests/test_architecture_invariants.py          # one file
.venv/bin/pytest tests/test_architecture_invariants.py::test_orchestrator_never_imports_the_target
.venv/bin/pytest -k invariant                     # by name pattern
.venv/bin/pytest --cov=src --cov-report=term-missing

.venv/bin/ruff check .         # lint (target/ excluded — gated separately)
.venv/bin/ruff check --fix .   # lint + autofix
.venv/bin/ruff format .        # format

cd target && ../.venv/bin/uvicorn shortener.main:app --reload    # target on :8000
```

`testpaths = ["tests"]` means a bare `pytest` runs **only our suite**; the target's tests are
run explicitly, because they are agent-written and gated rather than owned. The editable
install covers `orchestrator` only — the target is not packaged (D3), and puts itself on the
path via `target/tests/conftest.py`. A `ModuleNotFoundError` for `orchestrator` means the
editable install is stale; re-run `uv pip install -e ".[dev]"`.

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
