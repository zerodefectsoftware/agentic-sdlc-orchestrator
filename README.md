# Agentic SDLC Orchestrator

A governed orchestration layer that takes a requirement and produces a **reviewable
engineering outcome** — a change set plus the evidence needed to approve it.

> The orchestrator decides *what work happens next, who does it, whether the result is
> acceptable, and whether a human must sign off* — and records all of it.

**The URL shortener in `src/shortener/` is not the deliverable.** It is the target codebase
the orchestrator drives through an SDLC, and it exists to make the system falsifiable:
without a real, running, tested artifact at the end, gate results are unverifiable claims.

---

## Status

Design complete; implementation in progress.

| | |
| --- | --- |
| Architecture and decision registry (D1–D15) | ✅ `docs/architecture.md` |
| Toolchain, verified runnable | ✅ health endpoint + passing test |
| Orchestrator engine — graph, gates, policy, lineage | 🚧 in progress |
| Scenario runs — greenfield / brownfield / ambiguous | ⬜ not started |

This README describes what runs today. It will grow as the engine lands.

---

## Quick start

Requires Python 3.13 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"

.venv/bin/pytest                # orchestrator suite
.venv/bin/pytest target/tests   # target suite (agent-written)
.venv/bin/ruff check .          # lint (target excluded — it is gated separately)
```

Drive the orchestrator:

```bash
orchestrator preflight                   # validate the plan; refuse to start if a check is missing
orchestrator run                         # requirement in, stops at the first checkpoint
orchestrator status                      # node by node, with what is blocking
orchestrator approve <run> <node> --by you
orchestrator evidence --write            # the reviewable bundle
orchestrator metrics                     # success rate, retries, MTTR
orchestrator why design.spec             # trace an artifact to what produced it
```

No API key is needed for `stub` or `replay` runs — see [`.env.example`](.env.example).

Serve the target:

```bash
cd target && ../.venv/bin/uvicorn shortener.main:app --reload
curl localhost:8000/health      # → {"status":"ok"}
```

Run a single test:

```bash
.venv/bin/pytest tests/test_architecture_invariants.py::test_orchestrator_never_imports_the_target
```

---

## Why this exists

When a human writes 200 lines, review effort is proportionate to what a human could
plausibly get wrong. When an agent writes 2,000 lines an hour, **review capacity becomes the
bottleneck**, and the only way through it is machine-checkable evidence attached to every
change.

So the scarce resource stops being code generation and becomes *justified confidence*. This
system is built around that premise: agents produce, tools verify, humans authorize.

The nearest familiar analogue is **GitHub Actions, where the jobs are agents instead of shell
scripts** — plus the governance that becomes necessary once jobs stop being deterministic.

| GitHub Actions | Here |
| --- | --- |
| Workflow YAML | Plan graph |
| Job | Node |
| Required status checks | Exit gates |
| Environment protection rules | Human approval checkpoints |
| Matrix jobs → joining job | Parallel branches → sync barrier |
| Run history / artifacts | Audit trail + evidence bundle |

---

## Design in one page

Full detail in **[`docs/architecture.md`](docs/architecture.md)**. The load-bearing ideas:

**The orchestrator is not an agent.** It is a deterministic control plane. If a model decides
whether a gate passed, the guardrails are themselves probabilistic.

**Three separate graphs** — plan (intent, versioned), run (execution, mutates), lineage
(evidence, append-only). Collapsing them makes audit-grade traceability impossible, because
you overwrite the history you needed.

**Gates are evaluated by non-producers.** An agent's output is a proposal until a real tool
— `pytest`, `ruff`, a schema validator, a traceability matrix — has checked it.

**Tests come before implementation, written by a different agent**, and the suite must
*fail* against the scaffold before implementation begins. A suite that passes against an
empty scaffold asserts nothing. `tests/` is write-protected during repair loops, because the
cheapest path to a green suite is to weaken an assertion.

**Approvals are bound to artifact versions.** If an upstream artifact is re-derived, prior
approval reverts to pending — approval of a superseded artifact is not approval.

**The graph flows uphill when it must.** A late-stage security finding whose root cause is an
early design decision invalidates that decision and re-derives downstream. That is the
difference between orchestration and a pipeline.

---

## Three scenarios

One plan template, three traversals — which is the generality argument.

| Scenario | Requirement | Primarily demonstrates |
| --- | --- | --- |
| **Greenfield** | Build the shortener: core APIs, analytics, reliability | Decomposition, parallel execution with synchronization, red→green gating |
| **Brownfield** | *"Click counts are far lower than actual traffic. Investigate and fix."* | Codebase reasoning, regression safety, rollback, cross-run lineage |
| **Ambiguous** | *"Add rate limiting to protect the service."* | Blocking rather than guessing, calibrated escalation, re-planning |

The brownfield bug is real and its diagnosis requires reading lineage from the greenfield
run: the redirect returns `301 Permanent`, browsers cache it, repeat visits never reach the
service, analytics undercount.

The ambiguous requirement hides a missing prerequisite — *should authenticated users get
higher limits?* There is no authentication. Surfacing that changes scope.

---

## Testing approach

Three populations, deliberately different in what they prove:

| Layer | Author | Weight |
| --- | --- | --- |
| Orchestrator unit tests | human-authored, run against **recorded worker fixtures** | The engine must be trustworthy; it is not agent-written. Because every worker sits behind one interface, these tests never call a model — gates, invalidation, retries, and rollback are all deterministic |
| Target acceptance tests | agent, from acceptance criteria, pre-implementation | **The gate.** Must go red → green |
| Target regression tests | inherited from prior runs | Must stay green → green |

The strongest quality signal is **acceptance-criterion → test traceability**, not line
coverage. Coverage is trivially gamed by assertion-free tests; it is checked because it is
cheap and conventional, and it is not the quality argument.

---

## Limitations and trade-offs

Stated plainly, because several are structural rather than unfinished work.

- **Governance overhead is disproportionate for one shortener** — and that is honest. CI/CD
  is absurd overhead for one deploy and pays for itself at the fiftieth.
- **Coverage is gameable.** Mutation testing is the correct answer and is not implemented.
- **Metrics are descriptive, not statistical.** Across three runs, success rate and MTTR are
  instrumentation. No significance is claimed.
- **Running agent-written tests is arbitrary code execution.** Mitigated by subprocess
  isolation in a dedicated venv; container isolation is correct and is not implemented.
- **Gate quality ceiling.** The system is only as good as its checkable predicates.
  Subjective quality — design taste, naming, maintainability — is not gateable and is
  deliberately routed to human approval.
- **The endpoint is release readiness, not deployment.** A reviewable outcome is the goal.
- Single target language; synchronous CLI approvals; single-node SQLite state; whole-tree
  rollback granularity.

Full risk register in [`docs/architecture.md`](docs/architecture.md) §10.

---

## Repo layout

```
src/orchestrator/    the deliverable — control plane (engine, gates, policy,
                     workers, lineage, state, metrics, cli)
plans/               plan graphs (YAML) — the SDLC, as data
prompts/ schemas/    agent role prompts; artifact contracts
config/              target profiles
requirements/        prose scenario inputs — what a run consumes
fixtures/            recorded worker outputs, for replay runs and engine tests
target/              the target codebase — written by runs, not by hand
tests/               orchestrator tests (never agent-written)
runs/                per-run artifacts, lineage, evidence bundles (gitignored)
```

Full map and the rules that govern each directory: [`docs/repo-layout.md`](docs/repo-layout.md).

`orchestrator` never imports the target, no node may write outside `target/`, and the
target's tests live in a separate tree from ours. All three are enforced by
`tests/test_architecture_invariants.py` rather than left as claims.

---

Built as an interview assignment. The assignment brief is intentionally not published here —
it is classified internal to the issuing organization.
