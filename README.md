# Agentic SDLC Orchestrator

A governed orchestration layer that takes a requirement and produces a **reviewable
engineering outcome** — a change set plus the evidence needed to approve it.

> The orchestrator decides *what work happens next, who does it, whether the result is
> acceptable, and whether a human must sign off* — and records all of it.

**The URL shortener in `target/` is not the deliverable.** It is the target codebase
the orchestrator drives through an SDLC, and it exists to make the system falsifiable:
without a real, running, tested artifact at the end, gate results are unverifiable claims.

---

## Status

A greenfield run completed end to end and was accepted on 2026-08-02.

| | |
| --- | --- |
| Architecture and decision registry (D1–D25) | ✅ `docs/architecture.md` |
| Orchestrator engine — graph, gates, policy, lineage | ✅ **497 tests**, deterministic — no test calls a model |
| Greenfield, end to end | ✅ run `610c782beb9a4ea6bc7c8d06444eb432` — 20 nodes, evidence bundle **RELEASABLE** |
| Target produced by that run | ✅ 8 modules, ~1,550 lines, **86 tests passing, 93.64% coverage** |
| Ambiguous | ◐ proven through requirements: 15 ambiguities from one sentence, 5 escalated to a human |
| Brownfield | ⬜ written and audited against the current engine, **not executed** |

What live execution found — roughly thirty defects, none visible to the test suite — is in
[`docs/engineering-summary.md`](docs/engineering-summary.md) §5. It is the most useful
evidence here.

---

## Setup

Python **3.13 or newer** and [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.13          # create the virtualenv
uv pip install -e ".[dev]"     # orchestrator + test tooling
```

That runs the whole test suite, every stub and replay run, and every read-only command.
**No API key is needed for any of it.**

To execute real model work — the `agent` and `codeagent` node kinds — add the live extra
and a credential:

```bash
uv pip install -e ".[dev,live]"
cp .env.example .env           # then set ANTHROPIC_API_KEY
```

`.env` holds credentials, paths and the worker switch (`stub` · `replay` · `live`). Model
and effort are deliberately *not* there: they are per-node fields in the plan graph (D16),
so a run's cost profile lives in the artifact describing the run.

### Verify the install

```bash
.venv/bin/pytest                       # 497 passed
.venv/bin/ruff check .                 # clean
.venv/bin/orchestrator preflight       # validates the plan and every check it names
```

`preflight` refuses a plan naming a predicate the engine cannot supply, or a check whose
parameters a node does not declare — before anything executes.

---

## Run what was already built

```bash
.venv/bin/pytest target/tests                                    # 86 passed
cd target && ../.venv/bin/uvicorn shortener.main:app --reload
curl -i http://127.0.0.1:8000/health
```

API docs at `http://127.0.0.1:8000/docs` — generated from the Pydantic models, not written.

### Read the completed run

Everything here reads recorded state. Nothing executes, nothing costs anything.

```bash
.venv/bin/orchestrator runs
.venv/bin/orchestrator status   610c782beb9a4ea6bc7c8d06444eb432   # node by node
.venv/bin/orchestrator metrics  610c782beb9a4ea6bc7c8d06444eb432   # success rate, retries, MTTR
.venv/bin/orchestrator evidence 610c782beb9a4ea6bc7c8d06444eb432   # the reviewable bundle
.venv/bin/orchestrator why design.spec 610c782beb9a4ea6bc7c8d06444eb432

cat runs/610c782beb9a4ea6bc7c8d06444eb432/artifacts/intake.register/v3
```

---

## Start a new run

**A run is destructive to its target** — it writes code, tests and documentation into the
directory its profile names. Point a new run at a *different* target so it cannot overwrite
the accepted implementation:

```bash
.venv/bin/orchestrator run \
  --plan plans/greenfield.yaml \
  --requirement requirements/greenfield.md \
  --target config/target.shortener-demo.yaml
```

Check where it *would* write first — this executes nothing:

```bash
.venv/bin/orchestrator run --target config/target.shortener-demo.yaml --dry-run
```

| Profile | Writes to | Use for |
| --- | --- | --- |
| `config/target.shortener.yaml` | `target/shortener` | the accepted run — **do not point a new run here** |
| `config/target.shortener-demo.yaml` | `target/shortener_demo` | a fresh greenfield run |
| `config/target.ratelimit.yaml` | `target/ratelimit` | the ambiguous scenario |

The target is named in exactly one file. Nothing in `src/orchestrator/` knows what a
shortener is (D3), so retargeting is a config change, not a code change.

### Watch it, answer it, stop it

A run stops at human checkpoints and exits — it does not hold a terminal. Follow it from a
second one:

```bash
.venv/bin/orchestrator watch <run-id> -v                      # nodes, gates, scope denials
.venv/bin/orchestrator watch <run-id> --decide --by "you"     # ...and answer checkpoints here

.venv/bin/orchestrator approve <run-id> <node> --by "you" --note "why"
.venv/bin/orchestrator reject  <run-id> <node> --by "you" --note "why"
.venv/bin/orchestrator stop    <run-id> --by "you" [--force]  # --force kills the driver
```

Greenfield stops three times: **clarify-with-human** (answer the ambiguities),
**design-approval** (approve the contract before it is built), **accept** (accept the change
set against the evidence bundle). A full run takes 60–90 minutes.

### Correcting a run

```bash
.venv/bin/orchestrator retry      <run> <node> --by you --why "fixed the generator"
.venv/bin/orchestrator recheck    <run> <node>                # re-run checks, not the work
.venv/bin/orchestrator invalidate <run> <node>... --by you --why "the gate was wrong"
.venv/bin/orchestrator rollback   <run>                       # restore the baseline, then verify it
```

`recheck` exists because an ERROR means the harness failed, not the work — repeating a
twelve-minute agent session over a missing parameter is the waste that verdict exists to
prevent.

**Before demonstrating any of this, read [`docs/demo.md`](docs/demo.md)** — what to show,
in what order, and what will bite you.

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
