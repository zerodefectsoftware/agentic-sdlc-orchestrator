# Architecture Overview

**System:** A governed, agentic SDLC orchestrator, demonstrated by building and evolving a
URL shortener service.

**Status:** Design. Implementation in progress. This document is the authoritative
description of the orchestrator's architecture and the registry for decisions D1–D15.

---

## 1. What this system is

The deliverable is **not** a URL shortener. It is an orchestration layer that takes a
requirement and produces a **reviewable engineering outcome** — a change set plus the
evidence needed to approve it.

> The orchestrator decides *what work happens next, who does it, whether the result is
> acceptable, and whether a human must sign off* — and records all of it.

Four responsibilities: **sequencing, assignment, acceptance, escalation.** Everything else
is machinery serving those.

The URL shortener is the target codebase — the workload the orchestrator drives through an
SDLC. It exists to make the system falsifiable: without a real, running, tested artifact at
the end, gate results are unverifiable claims.

### Two nested lifecycles

Conflating these is the primary failure mode when reading this repo:

| | |
| --- | --- |
| **Outer** | Engineers + Claude Code build the orchestrator. Not the deliverable. |
| **Inner** | The orchestrator drives requirements → design → implementation → testing → documentation → release readiness over the shortener. **This is the deliverable.** |

Consequence: the shortener's own architecture is an *output* of runs, recorded in run
artifacts and its own decision records. It is deliberately not described here.

### Scope of generality

**General across products. Specialized to software delivery.**

The control plane knows what a requirement is, that tests gate implementation, that docs
follow code. It knows nothing about short codes, redirects, or click counts. Everything
target-specific lives in a **target profile** (§4.6), so retargeting is a config change.

The architectural guarantee: `orchestrator` never imports `shortener` (D3). Generality is
therefore checkable, not merely claimed.

---

## 2. Components

```
┌───────────────────────────────────────────────────────────────┐
│  CONTROL PLANE  — deterministic                               │
│                                                               │
│  planner · scheduler · gate evaluator · policy engine ·       │
│  retry/rollback controller · lineage recorder · metrics       │
└───────────────────────────────────────────────────────────────┘
          │ dispatches work                ▲ results + evidence
          ▼                                │
┌───────────────────────────────────────────────────────────────┐
│  WORKERS                                                      │
│                                                               │
│  LLM agents          deterministic tools       humans         │
│  (judgment work)     (verification)            (authorization)│
│  non-deterministic   trusted                   authoritative  │
└───────────────────────────────────────────────────────────────┘
          │ read / write (scoped)
          ▼
┌───────────────────────────────────────────────────────────────┐
│  TARGET — shortener source, tests, docs, contracts            │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│  STORES — run state · artifacts · lineage · audit log         │
└───────────────────────────────────────────────────────────────┘
```

**The orchestrator is not itself an agent.** It is a governor, and governance you cannot
trust is worthless. If a model decides whether a gate passed or whether something needs
approval, the guardrails are themselves probabilistic. Non-determinism belongs in the
workers; the control plane stays deterministic.

Closest familiar analogue: **GitHub Actions, where the jobs are agents instead of shell
scripts** — plus the governance that becomes necessary once jobs stop being deterministic.

| GitHub Actions | Here |
| --- | --- |
| Workflow YAML | Plan graph |
| Job | Node |
| `needs:` | Dependency edges |
| Required status checks | Exit gates |
| Environment protection rules | Human approval checkpoints |
| Matrix jobs → joining job | Parallel branches → sync barrier |
| Re-run failed jobs | Bounded retries |
| Run history / artifacts | Audit trail + evidence bundle |

### 2.1 What we build vs what we buy (D17)

"Hand-rolled orchestrator" describes one layer, not the whole system. The principle is
**build what's graded, buy what isn't** — §4.4 is a list of control-plane properties, so
that is what we write; everything else is a dependency.

| Capability | Provider | Ours? |
| --- | --- | --- |
| Agent loop, file/bash tools, permissions, subagents, sessions | Claude Agent SDK | bought |
| Schema-constrained artifact generation | Anthropic SDK + Pydantic | bought |
| Graph algorithms — topological order, cycle detection, transitive descendants | networkx | bought |
| Test execution, linting (the gate evaluators) | pytest, ruff | bought |
| API/schema definitions, OpenAPI generation | FastAPI + Pydantic | bought |
| Persistence for run state, lineage, audit | SQLAlchemy + SQLite | bought |
| Plan parsing, artifact validation | PyYAML, jsonschema | bought |
| CLI, human approval prompts | Typer, rich | bought |
| **Scheduler, gates, policy, lineage, invalidation, re-planning, metrics** | — | **ours** |

The last row is roughly **1,200–1,500 lines**. Everything above it is imported, and each
dependency displaces work that would otherwise be done worse and score nothing.

Invalidation illustrates the line: marking every transitively dependent artifact stale is
`networkx.descendants()`. The *semantics* of what invalidation means — which approvals
revert, which gates re-open — is ours, because that is the governed behaviour being graded.

Two things break when workers are models, and those breakages are the whole design:

1. **Self-reports are unreliable.** A script exiting 0 succeeded; an agent saying
   "implementation complete" has merely asserted something. Gates must be evaluated by
   something other than the producer (D4).
2. **The work is not knowable in advance.** Requirements surface ambiguities; tests fail in
   ways that demand repair. The graph mutates during execution (§6).

---

## 3. Three graphs, deliberately separate

Collapsing these into one mutable object is how audit-grade traceability becomes impossible
— you overwrite the history you needed.

| Graph | Role | Mutability |
| --- | --- | --- |
| **Plan graph** | Intent — stages, dependencies, gates | Authored, versioned |
| **Run graph** | Execution — node states, attempts, inserted nodes | Mutates constantly |
| **Lineage graph** | Evidence — artifact ← decision ← inputs ← (agent, prompt, model) | Append-only |

Nesting is the fourth sense of "graph": a node may **expand into a subgraph**. `impl` is not
one node — it fans out per module and joins. Composition is how decomposition depth becomes
structural rather than narrated.

### What is *not* a node

- **Cross-cutting services** — policy engine, lineage store, metrics, audit log. Available
  to every node; modeling them as nodes would give edges from everything to everything.
- **Dynamically inserted nodes** *are* nodes — they simply weren't in the template (§6).
  Fan-out children, repair nodes, and **escalations** all arrive this way.

Nothing executes outside the graph; plenty exists outside it.

That rule has teeth: an escalation could easily have been a run status with a note attached,
and instead inserts a `human` node (§6). The payoff is uniformity — every human interaction,
whether a planned checkpoint, a clarification, or an unplanned handoff, appears in the graph
and in the evidence bundle with a decider, a timestamp, and lineage.

---

## 4. Core abstractions

### 4.1 Node contract

```
Node
  id
  stage         lifecycle phase (§4.9) — a label, not an ordering constraint
  kind          one of the six node kinds (§4.7)
  inputs        artifact refs it consumes
  worker        resolved from kind + config
  outputs       artifact refs it must produce (schema-constrained)
  entry_gate    preconditions
  exit_gate     acceptance predicate, evaluated by a non-producer
  autonomy      AUTO | REVIEW | APPROVE | FORBIDDEN
  retry_budget  int
  write_scope   paths this node may modify
  model         model id (model-backed kinds only)
  effort        low | medium | high — reasoning depth for this node
```

`model` and `effort` are per-node because nodes differ enormously in how much reasoning they
justify: structured extraction in `intake` does not need the depth that `design` or `impl`
does. Putting them in the plan file makes **cost and latency a property of the configuration
rather than of the code** — tunable per node, visible in one place, and adjustable without
touching the engine.

```yaml
- id: intake
  kind: agent
  model: claude-opus-5
  effort: medium          # structured extraction — depth adds little
- id: design
  kind: agent
  model: claude-opus-5
  effort: high            # architectural judgment — depth pays
```

The same lever bounds a runaway repair loop: a retry can re-run at higher effort rather than
simply re-rolling the same dice (§6).

### 4.2 Gates

A gate is a **predicate over artifacts**, not a step. Entry gates check preconditions; exit
gates check acceptance.

**D4 — the producer never evaluates its own exit gate.** Wherever possible the evaluator is
a real tool: `pytest`, `ruff`, a schema validator, a traceability matrix. An agent's output
is a *proposal* until something that is not an agent has checked it.

**Where a gate's facts come from.** `ruff.exit_code == 0` on a node that never ran ruff is
not a check — it is a check that ERRORs, which is the correct verdict and a useless one to
ship. So a node declares the checks the engine runs after its work:

```yaml
- id: scaffold
  kind: derive
  run: py:orchestrator.derive.scaffold_from_design
  verify:                                        # run by the engine, not the node
    - "sh:{target.commands.lint}"                # → ruff.exit_code
    - py:orchestrator.gates.imports_resolve      # → imports.resolve
  gate:
    all:
      - "imports.resolve == true"
      - "ruff.exit_code == 0"
```

This is D4 made operational rather than aspirational. The node that wrote the code is not
the tool that judges it, and a fact a check observed wins over one the node reported. Checks
go through the same `Worker` as everything else (D18), so a replayed run stays hermetic
instead of shelling out; a check that cannot run makes the node ERROR rather than FAIL,
because it did not say no — it did not answer.

Some facts have no tool to produce them. The API contract exists only inside the design
artifact, and its author cannot vouch for it, so `contract_is_valid` is a **predicate**: the
engine reads the recorded artifact and checks it. The rule is the same either way — the
evaluator is never the producer.

Two gates are traceability matrices, and they are the most convincing artifacts the system
produces — cheap to compute, impossible to fake:

- **Requirement → design** (G3), checked in both directions. The reverse direction catches
  gold-plating: a design element mapping to no requirement means the agent invented work.
- **Acceptance criterion → test** (G7). Proves nothing was dropped between requirement and
  verification.

### 4.3 Autonomy policy

Declarative, not scattered conditionals. Risk class × action class → disposition.

| Class | Meaning |
| --- | --- |
| `AUTO` | Agent proceeds; result gated, logged |
| `REVIEW` | Proceeds; flagged for human attention, non-blocking |
| `APPROVE` | Blocks until a human authorizes |
| `FORBIDDEN` | Denied; escalates |

Policy overrides node defaults. Examples: a HIGH security finding forces `APPROVE` on a node
whose default is `REVIEW`; a breaking OpenAPI change forces `APPROVE` regardless of stage.

**Agents may never waive a security finding (D15).** Accepted risk requires a human, with
rationale, recorded. Segregation of duties.

### 4.4 Write scoping

Each node receives write access only to its own paths (D7). This is blast-radius
containment, not merely conflict avoidance: an agent that decides the cleanest fix is to
edit a neighbouring module is denied and escalates.

**During repair loops, `tests/` is write-protected (D6).** Unconstrained, the cheapest path
to a green suite is to weaken an assertion or delete the test, and every agent eventually
finds that path. Immutable tests convert "make the gate pass" into "make the code correct."

### 4.5 Artifacts and lineage

All run output lands under `runs/<run_id>/artifacts/`, each file registered with the node,
prompt, and model version that produced it. Lineage is causal and queryable, not
chronological — the question it must answer is *"why does this line of code exist, who
approved it, and on what evidence?"*

### 4.6 Target profile

Everything target-specific is configuration:

```yaml
target:
  root: src/shortener
  test_cmd: .venv/bin/pytest
  lint_cmd: .venv/bin/ruff check
  language: python
```

### 4.7 The plan is data, not code (D16)

The engine implements a small fixed set of **node kinds**; the plan graph is a YAML
document that composes them. Adding a stage is an edit to data, not to the scheduler.

| Kind | Worker | Determinism |
| --- | --- | --- |
| `agent` | Model call with a role prompt and an enforced output schema | Non-deterministic |
| `codeagent` | Coding-agent session, write-scoped to declared paths | Non-deterministic |
| `tool` | Command execution; exit code and output are the result | Deterministic |
| `derive` | Generation from a contract (models from OpenAPI, docs from schema) | Deterministic |
| `human` | Approval or clarification checkpoint | Authoritative |
| `fanout` | Materializes N children from an upstream artifact | Structural |

**`run:` names its execution scheme.** The `tool` and `derive` kinds cover two different
execution models, and deciding between them by asking "does this string import?" is how a
shell command that happens to look like a module path produces a baffling failure:

```yaml
run: "py:orchestrator.evidence.assemble"    # an importable callable
run: "sh:{target.commands.test_cov}"        # a shell command from the target profile
```

Six kinds cover the entire greenfield graph. The brownfield additions —
`impact-analysis`, `baseline-capture` — introduce **no new kinds**; they are new YAML
entries composing existing ones. That is the test of whether the factoring is right.

```yaml
- id: impl
  kind: fanout
  from: design.artifacts.modules          # runtime path into an upstream artifact
  template:
    kind: codeagent
    write_scope: "src/shortener/{item.path}"
    retry_budget: 2
  gate:
    all: ["ruff.exit_code == 0", "pytest.exit_code == 0"]
```

**The boundary, held deliberately:** *data describes structure; code provides node kinds
and gate predicates.* Gate expressions are a deliberately tiny expression language with an
escape hatch to a registered predicate function. Pushing further — conditionals, loops,
variables in YAML — reinvents a programming language badly. The line is drawn where it is
because it can be defended, not because the DSL ran out of features.

**Why this factoring is the design's main asset:** the plan graph is the part most likely
to change under requirements nobody anticipated. Keeping it declarative means unforeseen
extensions — a compliance-review stage, a performance-budget gate, a second target
language — arrive as configuration against an unchanged, already-tested engine. The
engine's surface stays small enough to reason about precisely because the interesting
variation lives outside it.

It also makes the system legible: a reviewer can read the SDLC directly out of one file,
rather than inferring it from control flow. **Appendix A** is the complete greenfield plan,
followed by the entire brownfield delta.

### 4.8 The Worker interface (D18)

Every node kind resolves to a **worker**, and all workers implement one interface:

```
Worker.run(node, inputs, scope) -> artifacts
```

`scope` carries the write permissions from §4.4. Nothing else in the engine knows whether
the work behind it was a model call, a subprocess, or a human.

This single seam does three jobs:

**1. It makes the engine testable.** The hard question for a system like this is *how do you
unit-test a scheduler whose workers are non-deterministic?* The answer is that the engine's
tests never call a model. Worker outputs are recorded once, then replayed — so gate
evaluation, invalidation cascades, retry budgets, rollback, stale-approval detection, and
the security→design re-plan are all covered by fast, deterministic tests.

**2. It makes runs reproducible.** A `replay` worker serves recorded artifacts, so a
scenario can be re-run identically — for a demo, for debugging, or to isolate an engine bug
from a model-behaviour change.

**3. It isolates the runtime choice.** Swapping a node's worker (D17) is a config change,
not a refactor.

Three worker implementations:

| Worker | Backs | Used in |
| --- | --- | --- |
| `LiveWorker` | Real model calls and subprocesses | Real runs |
| `ReplayWorker` | Recorded artifacts, keyed by node + input hash | Engine tests, demos |
| `StubWorker` | Fixed responses, including forced failures | Testing retry, rollback, safe-stop |

`StubWorker` is what makes the failure paths testable at all: a rollback that has never been
exercised is a claim, not a control.

The interface must be designed in from the start. Retrofitting it once model calls are
scattered through node implementations is painful, and the cost of adding it up front is
close to zero.

### 4.9 Stages, and the crosswalk to the brief

Every node declares a **stage** — the lifecycle phase its work belongs to. Three consumers:
lifecycle coverage becomes a check rather than a claim, metrics group by phase (where
retries concentrate is more useful than a run-wide rate), and the evidence bundle has a
natural table of contents.

**A stage is a label, not an ordering constraint.** Execution order comes from `needs`
edges. `tests-acceptance` is VERIFICATION work that runs *before* IMPLEMENTATION — the
test-first inversion (D5) breaks the linear phase model on purpose, and a taxonomy that
forbade that would be describing a different system.

| Stage | Greenfield nodes |
| --- | --- |
| `requirements` | intake, ambiguity-triage, clarify-with-human |
| `design` | design, design-approval |
| `implementation` | scaffold, impl |
| `verification` | tests-acceptance, tests, security |
| `documentation` | docs |
| `release` | release-readiness, accept |

#### Why not the brief's six verbatim

§4.4 names requirements, architecture/design, implementation, testing, documentation, and
release readiness. Ours differs in exactly one place, and the difference is deliberate:

| Brief | Ours | Note |
| --- | --- | --- |
| requirements | `requirements` | — |
| architecture/design | `design` | — |
| implementation | `implementation` | — |
| testing | `verification` | **Renamed.** The brief has no slot for security work, and folding a security scan into "testing" would misdescribe it. `verification` covers both. |
| documentation | `documentation` | — |
| release readiness | `release` | — |

One-to-one otherwise, so lifecycle coverage against the brief is checkable: `Plan.missing_stages`
is empty for the greenfield plan, and a test asserts it.

---

## 5. Orchestration model — greenfield

```
requirement text
      │
      ▼
   intake ──G1──► ambiguity triage ──G2──► design ──G3──► ◆ APPROVE: design ◆
                       │   ▲                                        │
                       ▼   │                                        ▼
              ◆ HUMAN: clarify ◆                                scaffold
                                                                    │ G4
                    ┌───────────────────────────────────────────────┤
                    ▼                                               │
            tests:acceptance ──G5 (RED: suite must FAIL)────────────┤
                                                                    │
                    ┌──────────┬──────────────┬─────────────────────┤
                    ▼          ▼              ▼                     ▼
                impl:api  impl:storage  impl:analytics      impl:reliability
                    └──────────┴──────┬───────┴─────────────────────┘
                          G6 per module: ruff · module unit tests
                                   ● SYNC
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
                  tests              docs             security
                  G7 GREEN           G8               G9
                    └──────────────────┼──────────────────┘
                                   ● SYNC
                                       ▼
                             release readiness (deterministic)
                                       │ G10
                                       ▼
                          ◆ APPROVE: accept change set ◆
```

### Node summary

| Node | Worker | Autonomy | Exit gate — evaluated by |
| --- | --- | --- | --- |
| intake | analyst agent | AUTO | G1 schema validator: every requirement has ≥1 testable AC |
| ambiguity triage | policy + human | APPROVE if severity ≥ high | G2 no ambiguity without a disposition |
| design | architect agent | AUTO → APPROVE | G3 R↔design matrix complete; OpenAPI validates |
| scaffold | codegen (derived) + agent | AUTO | G4 imports resolve; `ruff` clean |
| tests:acceptance | test agent (**≠ implementer**) | AUTO | G5 **suite must fail** against scaffold |
| impl:\* | implementer agents ×N | AUTO | G6 `ruff` + module unit tests |
| tests | — | AUTO | G7 suite passes; AC→test matrix complete; coverage ≥ 80% |
| docs | agent + derivation | AUTO | G8 setup instructions execute in a clean venv |
| security | scanners + agent | REVIEW → APPROVE on HIGH | G9 no unapproved HIGH findings |
| release readiness | deterministic | APPROVE | G10 §5.4 |

### 5.1 The fan-out is derived

`impl:*` nodes are **materialized at runtime from the design artifact's module
decomposition**. The graph's shape depends on an upstream agent's output — non-static
planning, and decomposition that is structural rather than described.

Parallelism is safe because scaffold froze the interfaces first; modules never negotiate.

### 5.2 Test-first inversion (D5)

If the implementer writes its own tests, G6 is self-certification in costume — the agent
that misunderstood the requirement encodes the same misunderstanding in its tests, and they
pass beautifully.

So: a **separate agent** derives the acceptance suite from acceptance criteria, before
implementation, and the implementer cannot edit it.

The **red gate (G5)** is the point of leverage. A suite that passes against an empty scaffold
asserts nothing about new behaviour. Requiring `fail → pass` is machine-checkable proof that
the tests exercise what was built. Cost: one extra `pytest` run.

### 5.3 Test layers and trust

| Layer | Author | Trust |
| --- | --- | --- |
| Unit | implementer, own module | Low — convenience, not evidence |
| Acceptance / integration | separate agent, from ACs | **High — this is the gate** |
| Security | from security requirements | High |

Line coverage is the weakest of G7's three checks — trivially gamed by assertion-free tests.
Included because it is cheap and conventional; it is not the quality argument. The stronger
answer is mutation testing, deliberately deferred (§9).

### 5.4 Release readiness

**Deterministic. No agent.** The final gate must never be a judgment call; a model deciding
"this is ready" reintroduces probabilistic governance at the last possible moment (D9).

Assembles the **evidence bundle**: change set diff · requirement register with every
ambiguity disposition · both traceability matrices · every gate result (evaluator, verdict,
timestamp) · test results and coverage · security findings and dispositions · decision
records · lineage export · run metrics · human approvals with *artifact version approved*.

**G10:** all upstream gates green · no unapproved HIGH findings · every artifact traceable to
a producing node · no node in a non-terminal state · **no stale approvals** (D10).

The stale-approval check is the strongest governance control in the system. If `design` was
re-run after sign-off, the human approved a document that no longer exists; the approval
reverts to pending and G10 blocks. Systems that capture approval as a boolean and never
revisit it do not have human-in-the-loop — they have a human-shaped speed bump.

**The system does not deploy (D12).** Objective §1 asks for a reviewable outcome, not a
shipped one. Release *readiness* is the correct endpoint.

---

## 6. Failure handling and re-planning

### Controls

**A gate reaches a verdict; policy decides the consequence.** That separation is what lets
a HIGH security finding force an approval on a node whose default is `REVIEW`, and it keeps
the evaluator ignorant of retry budgets.

The three verdicts get three different responses, and conflating the last two is a real
trap:

| Verdict | Meaning | Response |
| --- | --- | --- |
| `PASS` | The check ran and held | Proceed |
| `FAIL` | The check ran and did not hold | Repair loop — the work may be wrong, and a fix node might fix it |
| `ERROR` | The check **could not be performed** | **Escalate. No fix node.** |

**`ERROR` never enters the repair loop.** A missing fact, an unimplemented predicate, or a
broken evaluator is exactly as missing on the second attempt. Running repair against it
burns the retry budget, spends model calls, and delays the real signal — which is that the
*harness* needs attention, not the work. The plan schema enforces this: `on_error` cannot
declare a fix node, so it cannot be misconfigured.

The one exception is genuinely transient harness failure — a test command that crashed
before recording an exit code — for which `on_error.retries` exists, defaulting to `0`.

### Escalation inserts a node

When a budget is exhausted, the run does not park in a status. It **inserts a `human` node**
— `escalate:<node>#<attempt>` — depending on the node that escalated, inheriting its stage,
and presenting the gate record plus that node's outputs.

Three consequences:

- **Uniformity.** Planned checkpoints, clarifications, and unplanned handoffs are all `human`
  nodes. One mechanism, one audit shape, one CLI surface.
- **Attribution.** Inheriting the stage means metrics attribute the handoff to the phase whose
  work stalled, rather than to a category of its own.
- **Waivers stay human.** For a node with `may_waive: false` (D15), this node is the only
  route by which a finding can be waived at all — and a person is doing it, on the record.

Approve means the human accepts the state and the run proceeds past the failed node; reject
stops the run.

| Control | Behaviour |
| --- | --- |
| **Bounded retry** | Re-run with failure output as added context. Never the identical prompt — that is hoping the sampler is kinder. Budget per node, typically 2. |
| **Fallback** | Degraded path on exhaustion: narrower scope, simpler approach, or stronger model. |
| **Rollback** | Restore working tree to captured baseline; re-run suite to *confirm* green; mark run rolled-back, evidence retained. |
| **Safe-stop** | Halt with state persisted and resumable. No partial writes to the target. |

### Invalidation semantics

**Retry ≠ re-plan.** Retry re-runs a node. Re-planning changes the graph or invalidates
downstream work because upstream truth changed.

When a node's output changes, every transitively dependent artifact is marked stale and
re-derived, and **any human approval covering a stale artifact reverts to pending.**

Three triggers, one per scenario:

| Trigger | Example |
| --- | --- |
| Gate failure | G7 red → insert `fix` node scoped to the failing module → re-enter G7 |
| **Upstream finding** | G9 flags code enumeration → root cause is a *design decision* → invalidate design → re-approve → re-derive downstream |
| **Human revision** | Clarification answer changed after design sign-off → same cascade |

The second is the most important demonstration in the build: a late-stage node forcing
re-planning of an early stage, dragging an approval back to pending with it. Reviewers expect
work to flow downhill; a controlled flow *uphill* is what distinguishes this from a pipeline.

---

## 7. Scenario paths

One spine, two deltas, three traversals. `plans/greenfield.yaml` is the SDLC;
`brownfield.yaml` and `ambiguous.yaml` say what they change about it and inherit the rest
(D19). Nothing in `src/orchestrator/` distinguishes them.

### Greenfield
Full fan-out, triage passes through. Demonstrates decomposition, parallelism with
synchronization, red→green gating, artifact generation.

### Brownfield
Requirement: *"Click counts are far lower than actual traffic. Investigate and fix."* Root
cause is a decision from a prior run — `301 Permanent` redirects are browser-cached, so
repeat visits never reach the service and analytics undercount. Diagnosis requires reading
**cross-run lineage**: why is this a 301 → decision record → the assumption behind it.

Adds:

| Addition | Purpose |
| --- | --- |
| `baseline-capture` node | Runs the existing suite and snapshots the tree **before anything writes**. Gate: `baseline_is_green` — a red baseline is a stop, not a caveat, because nothing downstream could then distinguish a regression from an inherited failure. Also supplies the rollback target (D21). |
| `codebase-map` node | Parses the target with `ast` into files and symbols. Derived, not described — it is the ground truth the next node is checked against. |
| `impact-analysis` node | §4.3 of the brief: impacted modules, contract diff, invalidated prior decisions, regression surface, risk. Gate: `every_referenced_symbol_exists` — every symbol it names is checked against the map. Fluent analysis of code that does not exist is the characteristic brownfield failure, and it reads as thorough right up until someone checks. |
| Restricted fan-out | `impl` fans out over `impact-analysis.affected_modules`: in brownfield the blast radius is discovered, not declared. |
| Regression gate | Two populations, opposite requirements. New tests **red→green** (the RED gate means something sharper here: a test that does not reproduce the reported defect has not reproduced anything). Existing tests **green→green**, as a set difference against the baseline's failures — `pytest.exit_code == 0` cannot express this. |
| Breaking-change escalation | `has_breaking_contract_change` on the analysis activates an optional human node. An agent deciding on its own that a break is acceptable is exactly the decision this system does not delegate (D13, D15's sibling). |
| Approval widened | `design-approval` binds to the impact analysis as well as the design: approving a design without the blast radius it rests on is approving half the decision (D10). |
| **Rollback path** | `orchestrator rollback <run>` restores the snapshot and then **verifies** it. Exercised here; greenfield has nothing to restore to. |
| `scaffold` removed | Deriving package stubs from a design would overwrite the live code the run exists to change. |

### Ambiguous
Requirement: *"Add rate limiting to protect the service."* Unstated: scope (per IP / key /
link), threshold, window, response shape, whether the redirect hot path is included, shared
vs in-process state. And: *should authenticated users get higher limits* — **there is no
authentication**, so the ambiguity surfaces a missing prerequisite that changes scope.

**G2 blocks.** The system stops rather than silently choosing defaults. A linear prompt chain
given the same input emits a plausible rate limiter embodying five unstated decisions that
nobody ever learns about.

Clarification questions are decision-shaped — question, why it matters, options with
consequences, recommended default:

```
A3 · Rate limit scope
   Why: determines whether redirects (hot path) take a state lookup
   Options:
     a) POST /shorten only — protects the abuse vector, redirects stay fast
     b) both endpoints     — fuller protection, latency on every redirect
   Recommend: (a). No auth exists, so per-IP limiting on redirects would
              penalize shared-NAT users.
```

**Identified, escalated, normalized — three distinct steps.** `intake` extracts questions
instead of resolving them; `ambiguity-triage` decides which are worth a person's time, at a
threshold this plan lowers to MEDIUM *in data*; `normalize-clarification` folds the answer
back into the register as a structured, attributable disposition. Without the third step,
clarification is theatre: the run stops, a person answers, and design works from the same
unresolved register. The answer reaches downstream nodes because the scheduler passes a
`human` node's decision to whatever depends on it.

**Not everything escalates (D13).** Low-severity ambiguities receive agent-proposed
assumptions with rationale, recorded and propagated into lineage, the evidence bundle, and a
"Known assumptions" section of the README. Only high-severity blocks.

This is the calibration point of controlled autonomy: *a system that asks forty questions is
as useless as one that asks none.* The severity threshold is a governance knob.

---

## 8. Observability and metrics

Lineage answers *why*; the audit log answers *what happened when*; metrics answer *did the
controls work*.

| Metric | Definition |
| --- | --- |
| Success rate | Nodes passing exit gate on first attempt ÷ total node executions |
| Retry frequency | Retries ÷ node executions, per run |
| Rollback frequency | Rollbacks ÷ runs |
| MTTR | Mean over incidents of (gate went red → same gate went green) |
| E2E latency | Run start → release-ready, **with human wait time reported separately** |

Human wait time is separated because it dominates and is not a system property.

**Across three scenario runs these are instrumentation, not statistics.** No significance is
claimed.

---

## 9. Decision registry

| ID | Decision | Rationale | Cost accepted |
| --- | --- | --- | --- |
| **D1** | No orchestration framework; hand-rolled engine over `asyncio`. Alternatives evaluated below | Orchestration is the graded artifact; a framework supplies ~40% (state, checkpointing, interrupts) but obscures which design is ours. Gates, policy, lineage, invalidation — the other 60% — must be built regardless | We own the scheduler and its bugs |
| **D16** | The plan graph is declarative YAML over six node kinds (§4.7); the engine is fixed | Extension without engine change — new stages, gates, and scenarios are configuration. Keeps the engine small and makes the SDLC readable from one file | A declarative DSL has an expressiveness ceiling; dynamic fan-out needs an explicit construct |
| **D17** | **Build what's graded, buy what isn't.** Hand-roll the control plane; use existing libraries for worker runtimes, agent harnesses, and every non-graded capability | §4.4 is a list of control-plane properties — that is the differentiator. Reimplementing file/bash tooling, permission layers, and agent loops is a week of undifferentiated work that would consume the time the graded part needs | A dependency on the agent harness's permission model; D6/D7 enforcement is only as good as what it exposes |
| **D18** | One `Worker` interface behind every node kind, with live / replay / stub implementations | The only way to test a scheduler whose workers are non-deterministic; also makes runs reproducible and the runtime choice swappable (§4.8) | Recorded fixtures drift from real model behaviour and need periodic refresh |
| **D19** | Scenario plans are **deltas** over one base plan (`extends` / `insert_after` / `override` / `remove`), composed before validation | D16 claims a scenario is data. Three near-identical 180-line plan files would make that technically true and practically false — the second one to drift would prove it. Composing on raw mappings means a scenario is validated exactly like an authored plan; there is no second, weaker path into the scheduler | Reading a scenario means reading two files; a delta can express a graph its author did not picture |
| **D20** | The target profile is the only place the target is named, and `{target.*}` resolves at load time | D3 says the orchestrator never imports the target; this is what keeps that survivable. Retargeting is a config change, and a resolved plan means `--dry-run` shows the command that will actually run | A plan is not fully readable without its profile |
| **D21** | The baseline stores file **bodies**, not a reference to them | A rollback that can only name the state it wanted is a rollback in the documentation only. Content also means it works on a dirty tree and needs no VCS | Snapshot size grows with the target; fine at this scale, wrong for a large repo |
| **D22** | A node declares `verify:` — checks the engine runs after the work, whose facts the gate then reads | The alternative is a gate expression naming a fact nothing produces (which ERRORs) or a node reporting on its own output (which D4 forbids). Also the only shape that works for fan-out children, where a per-module check cannot be a separate node | The plan carries commands as well as intent; a check is only as good as the tool behind it |
| **D2** | SQLite backs orchestration state, not just target data | Safe-stop resumability and reliability metrics need durable, queryable run state | Single-node only |
| **D3** | `orchestrator` never imports `shortener`; target specifics in a config profile | Makes generality checkable rather than claimed | Some indirection |
| **D4** | Exit gates evaluated by a non-producer, preferably a real tool | Agent self-reports are assertions, not evidence | Gates limited to machine-checkable properties |
| **D5** | Acceptance tests authored by a different agent, before implementation, with a red gate | Prevents encoding the same misunderstanding in code and tests; proves tests exercise new behaviour | One extra test run; stricter authoring |
| **D6** | `tests/` write-protected during repair loops | The cheapest green suite is a weakened test; every agent finds that path | Genuinely wrong tests need human intervention |
| **D7** | Module-scoped write permissions per node | Blast-radius containment; enables safe parallel writes | Cross-module fixes must escalate |
| **D8** | Derive rather than generate wherever possible (models from OpenAPI, API docs from contract) | Derived artifacts cannot hallucinate and need no gate | Less flexibility in generated shape |
| **D9** | Release readiness is deterministic — no agent | A model judging "ready to ship" reintroduces probabilistic governance at the last step | Cannot capture subjective quality; covered by human approval |
| **D10** | Approvals are bound to artifact versions and revert to pending on upstream change | Approval of a superseded artifact is not approval | Re-approval churn during re-planning |
| **D11** | CLI control surface for the orchestrator, not HTTP | Demonstrates every required capability at a fraction of the cost | No remote/multi-user operation |
| **D12** | Endpoint is release readiness; no deployment | Brief asks for a reviewable outcome, not a shipped one | Deploy-time risks unmodelled |
| **D13** | Escalate only ambiguities above a severity threshold | Over-escalation destroys the economics of human oversight | Mis-tuned threshold can let a consequential assumption through |
| **D14** | Documentation gate executes the setup instructions in a clean environment | Doc gates that check for headings are vacuous | Slower gate; needs an isolated env |
| **D15** | Agents may not waive security findings | Segregation of duties | Every HIGH finding costs human time |

### Alternatives evaluated for D1

| Option | What it supplies | Why not |
| --- | --- | --- |
| LangGraph | Typed state, checkpointing, `interrupt` for human-in-loop, conditional routing | ~40% of §4.4, but a reviewer cannot tell which design is ours — the weakest sentence in a defense of the graded differentiator |
| CrewAI | Role-based agent teams | Abstraction is agents-with-roles, not artifacts-through-gates. Poorest fit |
| n8n | Visual workflow engine, JSON-defined graphs | Legitimate category; deliverable becomes a workflow export, which reads as integration rather than engineering. No gates-as-predicates, no lineage, no version-bound approvals |
| GitHub Actions | DAG, required checks, environment approvals, audit history, retries | Closest existing analogue — and the reason the Actions mapping in §2 is a specification rather than a metaphor. Fails on the two hardest requirements: workflows are static, so no runtime fan-out and no re-planning |
| Temporal | Durable execution — safe-stop, resume, retries done properly | The correct production answer for the failure-control layer; too heavy to stand up in the available time |

---

## 10. Risks, trade-offs, limitations

### Risks

| Risk | Mitigation |
| --- | --- |
| **Non-deterministic workers** make runs irreproducible | The `Worker` seam (§4.8): recorded fixtures replay a run identically, and engine tests never call a model. Deterministic gates; model version captured in lineage |
| **Prompt injection** via requirement text or target file contents | Agents receive scoped read access; generated code is never executed outside the test sandbox; policy evaluation never consults model output |
| **Arbitrary code execution** — running agent-written tests is executing untrusted code | Subprocess isolation in a dedicated venv. Container isolation is the correct answer and is not implemented |
| **Gate quality ceiling** — the system is only as good as its checkable predicates | Subjective quality (design taste, naming, maintainability) is not gateable and is deliberately routed to human approval |
| **Hand-rolled scheduler defects** (D1) | Scheduler covered by its own unit tests; kept small |
| **Rollback granularity** is whole-tree, not per-node | Acceptable at prototype scale; per-node would need content-addressed artifact storage |

### Trade-offs

- **Governance overhead is disproportionate for one shortener, and that is honest.** CI/CD
  is absurd overhead for one deploy and pays for itself at the fiftieth. The economics flip
  harder with agents: when generation is cheap, the bottleneck moves from writing code to
  *justifying* it, and the only way through is machine-checkable evidence per change.
- **Thin target, deep orchestrator.** Shortener surface is kept minimal — every extra
  endpoint adds demo runtime and no orchestration depth.
- **Determinism over capability** at every gate. Some quality dimensions become invisible to
  the system as a result.

### Limitations

- Single target language (Python) and a single target profile exercised
- Metrics are descriptive at n=3, not statistical
- Coverage is gameable; **mutation testing is the correct answer and is not implemented**
- Human approval is synchronous CLI; no async approval queue or multi-approver workflow
- SQLite state store is single-node
- No deployment, no runtime/production observability of the target
- Fallback strategies are shallow — scope narrowing only

---

## 11. Coverage of brief §4.4

| Required capability | Mechanism | Built or bought | Demonstrated by |
| --- | --- | --- | --- |
| Explicit dependency graph, entry/exit gates | §3, §4.1–4.2, §4.7 | **ours** (graph algorithms: networkx) | All |
| Sequential + parallel with synchronization | impl fan-out; tests ∥ docs ∥ security | **ours** (concurrency: `asyncio`) | Greenfield |
| Cross-stage context and decision lineage | §4.5 lineage graph | **ours** (storage: SQLAlchemy) | All; cross-run in brownfield |
| Human approval checkpoints | §4.3 policy; design + accept gates | **ours** (prompt UI: Typer/rich) | All |
| Bounded retries | §6 | **ours** | Greenfield, brownfield |
| Fallback | §6 | **ours** | Greenfield |
| Rollback | baseline-capture + restore | **ours** (tree snapshot: `git`) | **Brownfield** |
| Safe-stop | §6; red-baseline refusal | **ours** (durable state: SQLite) | **Brownfield** |
| Policy guardrails (security, compliance, change control) | §4.3, §4.4, D15, breaking-change approval | **ours**; write-scope *enforcement* from the Agent SDK permission layer | Brownfield, greenfield |
| Audit-grade observability | §8, evidence bundle | **ours** | All |
| Reliability metrics | §8 | **ours** | All |
| Dynamic re-planning | §6 invalidation | **ours** (descendant computation: networkx) | **Ambiguous** (human revision), **greenfield** (G9 → design) |
| Controlled autonomy | §4.3, D13 | **ours** | **Ambiguous** |

Every §4.4 capability is ours. Dependencies supply mechanism — graph maths, storage,
concurrency, a permission primitive — never the governed behaviour itself. That is the
build/buy line (§2.1) applied to the graded requirements one by one.

**Lifecycle coverage** — the brief's "coordinates the full SDLC lifecycle across
requirements, architecture/design, implementation, testing, documentation, release
readiness" — is the one item above that a document can only assert. It is now checkable:
every node declares a stage (§4.9), `Plan.missing_stages` is empty for the greenfield plan,
and a test says so.

---

## Appendix A — Worked plan graph

The engine is fixed; this file is the SDLC. `plans/greenfield.yaml`:

```yaml
plan: greenfield
version: 1
description: Requirement → reviewable change set, from an empty target.

defaults:
  model: claude-opus-5
  effort: high
  retry_budget: 2
  autonomy: AUTO

nodes:

  # ── Understand ──────────────────────────────────────────────────────────
  - id: intake
    kind: agent
    stage: requirements
    role: analyst
    output_schema: schemas/requirement_register.json
    outputs: [register]              # gates read intake.register
    effort: medium                    # structured extraction; depth adds little
    gate:                             # G1
      all:
        - predicate: schema_valid
        - predicate: every_requirement_has_testable_ac

  - id: ambiguity-triage
    kind: tool                        # policy evaluation, not a judgment call
    stage: requirements
    needs: [intake]
    run: py:orchestrator.policy.triage_ambiguities
    retry_budget: 0                   # pure function: a second run cannot disagree
    escalate_when:
      predicate: has_high_severity_ambiguity
    on_escalate: clarify-with-human
    gate:                             # G2 — the policy ran, not that nothing needs a human.
      all:                            # An open HIGH ambiguity is this node working, and
                                      # gating on its absence would fail exactly then.
        - predicate: every_ambiguity_is_disposed_or_escalated

  - id: clarify-with-human
    kind: human
    stage: requirements
    optional: true                    # only instantiated when triage escalates
    autonomy: APPROVE
    presents: [intake.artifacts.register]

  - id: normalize-clarification
    kind: tool
    stage: requirements
    needs: [clarify-with-human]
    optional: true                    # activated with the checkpoint it processes
    run: py:orchestrator.policy.normalize_clarification
    params:
      checkpoint: clarify-with-human
    inputs: [intake.artifacts.register]
    outputs: [register]
    # Without this, clarification is theatre: the run stops, a person answers,
    # and the answer sits in the audit trail while design works from the same
    # unresolved register. Re-emitting `intake.register` is also what makes it
    # stateful — downstream consumers invalidate, bound approvals go stale (D10).
    gate:                             # G2b
      all:
        - "clarification.resolved > 0"
        - predicate: no_ambiguity_without_disposition

  # ── Design ──────────────────────────────────────────────────────────────
  - id: design
    kind: agent
    stage: design
    role: architect
    needs: [ambiguity-triage, normalize-clarification]   # SKIPPED when nothing escalated
    inputs: [intake.artifacts.register]
    output_schema: schemas/design.json
    outputs: [spec, modules]          # gates read design.spec;
                                      # impl fans out over design.modules
    gate:                             # G3
      all:
        - predicate: contract_is_valid          # a predicate, not an expression:
                                                # only the artifact holds the contract,
                                                # and its author cannot vouch for it
        - predicate: requirement_design_matrix_complete   # both directions
        - predicate: no_unmapped_design_elements          # catches gold-plating

  - id: design-approval
    kind: human
    stage: design
    needs: [design]
    autonomy: APPROVE
    binds_to: [design.artifacts.spec]
    # binds_to implements D10: if either artifact is re-derived, this
    # approval reverts to pending and G10 blocks.

  # ── Build ───────────────────────────────────────────────────────────────
  - id: scaffold
    kind: derive                      # deterministic generation (D8), no model call
    stage: implementation
    needs: [design-approval]
    inputs: [design.artifacts.spec]   # design-approval is a human node: it
                                      # produces no artifact to inherit
    run: py:orchestrator.derive.scaffold_from_design
    params:
      root: "{target.root}"
    outputs: [manifest]
    write_scope: ["{target.root}/**"]
    verify:                           # the engine runs these; the node does not
      - "sh:{target.commands.lint}"
      - py:orchestrator.gates.imports_resolve
    gate:                             # G4
      all:
        - "imports.resolve == true"
        - "ruff.exit_code == 0"

  - id: tests-acceptance
    kind: codeagent
    stage: verification
    role: test-author                 # deliberately NOT the implementer (D5)
    outputs: [suite]                  # gates read tests-acceptance.suite
    needs: [scaffold]
    inputs: [intake.artifacts.register, design.artifacts.spec]
    write_scope: ["{target.tests_root}/**"]
    verify:
      - "sh:{target.commands.test}"   # a code agent cannot report its own red
    gate:                             # G5 — the RED gate
      all:
        - "pytest.exit_code != 0"     # must FAIL against the scaffold
        - predicate: every_ac_has_a_test

  - id: impl
    kind: fanout
    stage: implementation
    needs: [tests-acceptance]
    from: design.artifacts.modules    # graph shape derived at runtime
    template:
      kind: codeagent
      role: implementer
      inputs: [design.artifacts.spec, intake.artifacts.register]
      write_scope: ["{target.root}/{item.path}/**"]   # D7 blast radius
      verify:
        - "sh:{target.commands.lint_path} {target.root}/{item.path}"
      gate:                           # G6, per module. Scoped to this module on
        all:                          # purpose: a whole-suite check here is
          - "ruff.exit_code == 0"     # unsatisfiable until the last child lands,
                                      # so every earlier one would fail for work
                                      # that was never its own. G7 is where the
                                      # suite has to be green.

  # ── Verify (parallel, then join) ────────────────────────────────────────
  - id: tests
    kind: tool
    stage: verification
    needs: [impl]
    run: "sh:{target.commands.test_cov}"
    params:
      coverage_report: coverage.json   # written by the command above
    verify:
      - py:orchestrator.gates.report_coverage
    freeze_paths: ["{target.tests_root}/**"]  # D6: immutable during repair
    gate:                              # G7 — the GREEN gate
      all:
        - "pytest.exit_code == 0"
        - "coverage.percent >= {target.thresholds.coverage_min}"
        - predicate: ac_test_matrix_complete
    on_fail:
      insert: fix
      scoped_to: failing_module
      max_attempts: 2
      then: escalate

  - id: docs
    kind: codeagent
    stage: documentation
    role: technical-writer
    outputs: [readme]                 # gates read docs.readme
    needs: [impl]
    inputs: [design.artifacts.spec, intake.artifacts.register]
    effort: medium
    write_scope: ["target/README.md", "target/docs/**"]
    gate:                             # G8 — executable documentation
      all:
        - predicate: setup_steps_execute_in_clean_venv
        - predicate: documented_endpoints_match_openapi

  - id: security
    kind: tool
    stage: verification
    needs: [impl]
    run: py:orchestrator.gates.security_scan
    outputs: [report]                 # gates read security.report
    autonomy: REVIEW
    escalate_when:                    # policy overrides the node default
      predicate: has_high_severity_finding
    may_waive: false                  # D15: agents never waive findings
    gate:                             # G9
      all:
        - predicate: no_unapproved_high_findings

  # ── Release readiness ───────────────────────────────────────────────────
  - id: release-readiness
    kind: derive                      # deterministic by design (D9)
    stage: release
    needs: [tests, docs, security]    # sync barrier — needs is the join
    emits: evidence_bundle            # assembled by the engine: it reads four stores
    gate:                             # G10
      all:
        - predicate: all_upstream_gates_green
        - predicate: no_unapproved_high_findings
        - predicate: no_ambiguity_without_disposition   # nothing ships with an open question
        - predicate: lineage_complete
        - predicate: no_node_in_nonterminal_state
        - predicate: no_stale_approvals

  - id: accept
    kind: human
    stage: release
    needs: [release-readiness]
    autonomy: APPROVE
    presents: [release-readiness.artifacts.evidence_bundle]
```

Two forms of gate appear, as described in §4.7: **expressions** over tool results
(`pytest.exit_code == 0`) and **named predicates** for semantics no expression should carry
(`no_stale_approvals`). The engine registers the predicates; the plan composes them.

### The brownfield delta, in full

`plans/brownfield.yaml`, quoted here in the parts that carry the argument. Adding a
scenario means adding **nodes and overrides — no new node kinds, no engine change**:

```yaml
plan: brownfield
extends: greenfield

rollback:
  restore_from: baseline-capture          # the node holding the snapshot
  verify_with: "{target.commands.test}"   # a restore nobody checked is a second change

insert_after:
  intake:
    - id: baseline-capture                # before anything writes
      kind: tool
      run: py:orchestrator.policy.capture_baseline
      params: {command: "{target.commands.test}", root: "{target.root}"}
      outputs: [snapshot]
      gate:
        all:
          - predicate: baseline_is_green  # a red baseline is a stop, not a caveat

    - id: codebase-map                    # parsed, not described
      kind: derive
      run: py:orchestrator.derive.map_codebase
      params: {root: "{target.root}"}
      outputs: [map]

    - id: impact-analysis
      kind: agent
      role: codebase-analyst
      inputs: [codebase-map.artifacts.map]
      outputs: [report, affected_modules]
      escalate_when:
        predicate: has_breaking_contract_change
      on_escalate: breaking-change-review
      gate:
        all:
          - predicate: every_referenced_symbol_exists   # checked against the map

    - id: breaking-change-review
      kind: human
      needs: []                           # placed, not spliced — see below
      optional: true

override:
  impl:
    from: impact-analysis.artifacts.affected_modules    # narrower fan-out
  tests:
    run: py:orchestrator.policy.verify_no_regression
    inputs: [baseline-capture.artifacts.snapshot]
    gate:
      all:
        - "tests.exit_code == 0"
        - predicate: no_pre_existing_test_regressed
        - predicate: ac_test_matrix_complete

remove:
  - scaffold          # deriving stubs would overwrite the code being changed
```

Four new nodes, four overrides, one removal. The scheduler, gate evaluator, policy engine,
lineage recorder, and metrics collector are untouched — the claim in D16, stated as a diff
rather than as an assertion.

**Three composition rules earn their keep here.**

*Splicing rewires.* An inserted node goes **between** the anchor and everything that
depended on it. Without the rewiring, `baseline-capture` would run *alongside* the work it
is supposed to precede, which is the one thing a baseline must not do.

*A node that declares its own `needs` is placed, not spliced.* `breaking-change-review` is
an escalation target: it has to hang off the graph rather than sit in it, or every run
would wait for a question nobody raised.

*Override is shallow, and remove runs last.* A deep merge of a `gate` would silently keep
base checks a scenario meant to replace. And removal is validated against the **final**
shape, so a scenario can re-point a node's dependents and then drop it — which is exactly
what "this scenario does not scaffold" looks like.

`{target.*}` placeholders resolve from the target profile at load time, so `--dry-run`
prints the command that would actually run. `{item.path}` is left alone: it belongs to
fan-out materialisation, and is resolved per item at runtime.

### The ambiguous delta

`plans/ambiguous.yaml` changes four things and adds one node. Three of them are the
scenario:

| Change | Why |
| --- | --- |
| `intake` at `effort: high`, gate requires `ambiguities.total > 0` | The work here is noticing what the requirement does not say. An intake that finds nothing has not read it |
| `ambiguity-triage` with `params: {threshold: medium}` | The escalation threshold is the calibration knob of controlled autonomy (D13). A vague requirement is worth more interruptions than a clear one — and the knob is visible in data, not buried in Python |
| `clarify-with-human` with `optional: false` | Greenfield asks only when triage escalates; this plan *always* stops. There is no defensible reading to proceed from |
| **`normalize-clarification`** (new node) | The step that makes clarification more than theatre |
| `release-readiness` gate adds `no_ambiguity_without_disposition` | An assumption that survives to release must have been surfaced, not inherited |

Normalization is the part worth defending. Without it the run stops, a person answers, and
every downstream node keeps working from the same unresolved register — the answer lives in
the audit trail and changes nothing. So a human decision is now **material**: the scheduler
passes `<node>.decision` to anything that depends on a `human` node, and
`normalize-clarification` folds the answer back into `intake.register` as a structured,
attributable disposition per question. Re-emitting an artifact an upstream node already
produced is what makes this stateful rather than advisory: downstream consumers invalidate
and approvals bound to the old version go stale (D10).

Answers are matched by ambiguity id (`A1: 302, so browsers do not cache the redirect`).
An unmatched line is recorded against every still-open question rather than discarded —
losing what a person said is worse than attributing it a little too widely. Questions the
answer did not touch stay undisposed, which is what the gate is looking for.
