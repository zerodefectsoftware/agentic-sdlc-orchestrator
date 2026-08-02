# The orchestrator, explained

How the system works: the components, how they interact, and how data moves between them.
`docs/architecture.md` is the *why* — reasoning, costs accepted, decision registry.

> **A plan says what to do. The scheduler decides what can run now. A worker does the work
> and reports observations. A gate judges those observations. The store writes down what
> happened.** No component does two of those jobs.

---

## 1. Components

```
        plans/*.yaml            config/target.<name>.yaml
        (what to do)            (which codebase, which commands)
              │                          │
              └────────────┬─────────────┘
                           ▼
                  ┌──────────────────┐
                  │      LOADER      │  parse · validate · resolve placeholders
                  └────────┬─────────┘
                           │  a validated plan graph
                           ▼
    ┌──────────────────────────────────────────────────────┐
    │                    SCHEDULER                         │
    │        pick a wave  ·  dispatch  ·  record           │
    └───┬────────────┬────────────┬───────────┬─────────────┘
        │            │            │           │
        ▼            ▼            ▼           ▼
   ┌─────────┐  ┌─────────┐  ┌────────┐  ┌──────────┐
   │ WORKERS │  │  GATES  │  │ POLICY │  │  STORE   │
   │         │  │         │  │        │  │+ LINEAGE │
   │ do the  │  │ judge   │  │ decide │  │ write it │
   │  work   │  │   it    │  │ what's │  │   down   │
   │         │  │         │  │  next  │  │          │
   └────┬────┘  └─────────┘  └────────┘  └────┬─────┘
        │                                     │
        ▼                                     ▼
   the target repo                    runs/<id>/  +  SQLite
```

| Component | Owns | Does **not** | Built with |
| --- | --- | --- | --- |
| **Loader** | Plan schema, validation, placeholder resolution | Execute anything | YAML + Pydantic (strict — an unknown key is a load error) |
| **Scheduler** | Readiness, waves, dispatch, invalidation | Judge work or decide consequences | networkx for the DAG; a thread pool for dispatch |
| **Workers** | Running a node: model call, subprocess, coding session | Return a verdict on its own work | Anthropic SDK; `subprocess`; a coding-agent runtime |
| **Gates** | Facts → PASS / FAIL / ERROR | Do work or decide consequences | Expression evaluator + a registry of named predicates |
| **Policy** | What a verdict means: retry, fix, escalate, stop | Evaluate anything | Plain functions over the plan's declarations |
| **Store + Lineage** | Runs, attempts, gate records, artifacts, approvals | Any judgment at all | SQLAlchemy + SQLite; artifact bodies as files |
| **CLI** | Starting, observing, approving, resuming | Any of the above | Typer + Rich |

No orchestrator component imports the target. Retargeting is a config change, not a code
change.

---

## 2. One node, end to end

This is the whole system. Everything else is a variation.

```
 ┌── SCHEDULER ──────────────────────────────────────────────────────────┐
 │  1  ready?           every dependency finished?                       │
 │  2  gather material  the requirement, upstream artifacts,             │
 │                      and any human decision behind it                 │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
 ┌── WORKER ─────────────────────────────────────────────────────────────┐
 │  3  do the work      returns  facts + artifacts                       │
 │                      never    "it passed"                             │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
 ┌── SCHEDULER ──────────────────────────────────────────────────────────┐
 │  4  verify           run the node's declared checks — separately.     │
 │                      Their facts beat the node's own on a collision.  │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
 ┌── GATE ───────────────────────────────────────────────────────────────┐
 │  5  judge            PASS   performed, held                           │
 │                      FAIL   performed, did not hold                   │
 │                      ERROR  could not be performed                    │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
 ┌── POLICY ─────────────────────────────────────────────────────────────┐
 │  6  consequence      proceed · retry · insert a fix · escalate · stop │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
 ┌── STORE + LINEAGE ────────────────────────────────────────────────────┐
 │  7  write it down    attempt · gate record · artifacts · status       │
 └───────────────────────────────────────────────────────────────────────┘
```

**Steps 3, 4 and 5 are three different actors on purpose.** What wrote the code is not what
ran the linter, and neither is what decided the lint result was acceptable. That rule shapes
the whole design.

---

## 3. The two kinds of data

The distinction that makes the rest legible.

| | **Material** | **Facts** |
| --- | --- | --- |
| Is | *what you work from* | *what was observed* |
| Shape | name → body | dotted key → value + provenance |
| Example | `intake.register` → the JSON | `pytest.exit_code` → 1, from a tool |
| Flows | scheduler → worker | worker → scheduler → gate |
| Read by | the agent or tool doing the work | the gate deciding if it was acceptable |

Conflating them would let a worker gate itself. Every fact therefore carries where it came
from:

| Provenance | Meaning | Admissible to a gate |
| --- | --- | --- |
| tool | a command's exit code or parsed output | yes |
| validator | a check run over an artifact | yes |
| derived | computed deterministically by the engine | yes |
| human | a recorded decision | yes |
| **agent** | **a model's own assertion** | **no** |

"The agent says the schema is valid" cannot pass a gate. "A validator ran over the agent's
artifact and the schema held" can. The artifact is the *subject* of a check, never its author.

---

## 4. Where things are written

Two stores, split on purpose.

```
   SQLite — identity + history          Files — bodies
   ──────────────────────────           ──────────────────────────────
   runs                                 runs/<id>/artifacts/
   node executions                        intake.register/v1   ← analyst
   attempts     worker, model, effort     intake.register/v2   ← re-emitted
   gate records verdict, every check      intake.register/v3   ← human answer folded in
   artifacts    name, version, hash       design.spec/v1       ← rejected
   artifact inputs  which fed which       design.spec/v2       ← approved
   approvals    who decided, when         design.modules/v1
```

The database says an artifact exists and which attempt produced it. The disk holds what it
said — so a reviewer reads it with `cat`, and `v1` beside `v2` shows a re-derivation at a
glance where two hashes would not. Nothing is ever overwritten.

Artifacts are named `<node>.<output>`, versioned per run, monotonic. Three routes produce one:

| Producer | How the name is decided |
| --- | --- |
| Agent | Declared outputs. One model response is split — an output matching a schema field is that field, any other name gets the whole response |
| Coding agent | Declared output files: the session writes a real file, the engine reads it back |
| Python task | **The task names it**, and may re-emit an upstream artifact under its original name |

That last row is how a policy node with no declared outputs produces `intake.register@v2`.
It is also what re-derivation depends on (§7). Cost: the plan alone does not tell you every
producer of an artifact.

---

## 5. The wave loop

No join node, no barrier primitive, no dependency resolver beyond declared dependencies.
Execution runs in **waves** — collect everything that can run now, run it together, record
all of it, repeat.

Greenfield, as it actually ran. ⏸ blocks the run until a person answers:

```
 ┌────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────────┐
 │ intake │──▶│  triage  │──▶│  clarify-  ⏸ │──▶│  normalize-  │──┐
 └────────┘   └────┬─────┘   │  with-human  │   │ clarification│  │
                   │         └──────────────┘   └──────────────┘  │
                   │            ▲  woken by 4 HIGH ambiguities    │
                   └────────────┘                                 │
                   │                                              │
                   └──────────────────┬───────────────────────────┘
                                      ▼
                                 ┌────────┐   ┌───────────────┐   ┌──────────┐
                                 │ design │──▶│ design-     ⏸ │──▶│ scaffold │─┐
                                 └────────┘   │ approval      │   └──────────┘ │
                                              └───────────────┘                │
                    ┌──────────────────────────────────────────────────────────┘
                    ▼
             ┌────────────┐   ┌────────┐
             │   tests-   │──▶│  impl  │──┐
             │ acceptance │   └────────┘  │
             └────────────┘               │
                    ┌────────────────────-┘
                    ▼      ONE wave — three nodes, dispatched in parallel
           ┌────────┬────────┬──────────┐
           │ tests  │  docs  │ security │
           └────┬───┴───┬────┴────┬─────┘
                └───────┼─────────┘
                        ▼          the join IS the wave boundary
               ┌──────────────────┐
               │ release-readiness│
               └─────────┬────────┘
                         ▼
                  ┌────────────┐
                  │  accept  ⏸ │
                  └────────────┘
```

| Rule | Consequence |
| --- | --- |
| Dependencies are the only ordering primitive | Parallelism and synchronization both fall out of them; neither has its own construct |
| Workers run concurrently, **state is recorded on one thread** | A run's recorded history is identical whichever thread finished first |
| Every outcome of a wave is recorded, even after something blocks the run | The wave already executed — files are on disk. Stopping early would leave code with no attempt and no gate verdict behind it. Five implementer sessions were lost that way |
| A skipped node counts as satisfied | One graph serves both the escalated and the clean path, with no branch construct |

---

## 6. When a gate says no

```
                      ┌─────────┐
                      │  PASS   │──▶ proceed  ─▶ if an escalation condition holds,
                      └─────────┘                 wake the node the plan named
                      ┌─────────┐
                      │  FAIL   │──▶ attempts left  ──▶ RETRY
                      └─────────┘    repair declared ──▶ INSERT a fix node
                                     otherwise       ──▶ ESCALATE, run blocks
                      ┌─────────┐
                      │  ERROR  │──▶ ESCALATE. Never a fix node.
                      └─────────┘
```

**Three verdicts, not two** — the load-bearing choice:

| Collapsing ERROR into… | Failure it causes |
| --- | --- |
| PASS | A missing fact or unimplemented check reports green. Governance becomes decorative |
| FAIL | A broken evaluator looks like broken code, so the repair loop retries a harness problem forever, burning model calls |

Both block the run. Only ERROR says *investigate the harness, not the work* — which is also
why an ERROR cannot insert a fix node. A fix is a code change; a missing check is exactly as
missing on the second attempt.

A repair inserts **an edge, not just a node**: the failed node is made to depend on its own
fix, so it re-enters only after the repair has run. The fix inherits the failed node's frozen
paths — it may change the code, never the tests judging it.

---

## 7. Re-derivation

The hard requirement: when an upstream output changes, downstream work must invalidate and
re-run under governance.

```
   a node re-emits an artifact  →  version 2
                  │
                  ▼
   ┌──────────────────────────────────────────────────────┐
   │ every downstream node that had PASSED   →   STALE     │
   │ every approval bound to the old version →   PENDING   │
   └──────────────────────────────────────────────────────┘
                  │
                  ▼
       release gate blocks on stale approvals
```

| Detail | Why |
| --- | --- |
| Only version 2 onward invalidates | The first production of an artifact is the graph running forwards, not a change to something already consumed |
| Approvals bind to **versions**, not nodes | A human cannot be recorded as having blessed something they never saw |

This is why a normalization step exists after every clarification checkpoint. Without it the
run stops, a person answers, the answer sits in the audit trail, and every downstream node
keeps working from the same unresolved input. Re-emitting is what turns a human answer into
state.

---

## 8. How a human enters the graph

A human is a **node**, not a status flag.

```
   scheduler reaches a human node
        │
        ├─ records an approval request, bound to the artifact versions it covers
        ├─ node → BLOCKED,  run → BLOCKED
        └─ the process exits
                     │        ... minutes or days ...
                     ▼
        an approval command records who decided, when, and what they said
                     │
                     ▼
        the run resumes from exactly where it stopped, and downstream
        nodes receive the decision text as material
```

| Property | Consequence |
| --- | --- |
| A blocked run persists entirely in the store | The process can exit; a different one resumes it — including runs that inserted nodes at runtime |
| Optional nodes start **skipped**, not pending | A checkpoint with no dependencies would otherwise be "ready" immediately, and every run would stop to ask a question nobody raised |
| Waking a checkpoint wakes what processes it | Otherwise the run stops, gets an answer, and then behaves as though nobody had answered |
| The decision becomes material | The answer is context that crosses stages, not an audit-log entry |

---

## 9. Blast radius

Every node that writes declares where. Three layers, checked at different times.

```
   target profile   ceiling: target/**            checked at LOAD time —
        │                                         a plan outside it will not load
        ▼
   plan node        scope: target/<pkg>/**        where this node may write
        │
        ▼
   plan node        frozen: target/tests/**       immutable even though in scope
```

| Layer | Enforces |
| --- | --- |
| Ceiling | An agent cannot reach the orchestrator that governs it |
| Scope | One node's mistake stays inside one directory |
| Frozen paths | During a repair, the cheapest route to a green suite is a weakened test — this closes it |

Honest limitation: the guard **detects** rather than **prevents**. Prevention belongs to the
runtime executing the work; this layer exists so a violation is still caught when that
runtime cannot enforce it, as with an arbitrary subprocess.

---

## 10. The worker seam

One interface, and the scheduler cannot tell the implementations apart. A worker returns
facts and artifacts; never a verdict.

| Mode | Used for |
| --- | --- |
| **stub** | Tests — scripted results, no calls |
| **replay** | Deterministic demos from recorded fixtures, no latency, no cost |
| **live** | Real models and subprocesses |

Live is itself a router, not a doer:

```
   node kind                dispatched to
   ─────────                ─────────────
   agent          ────────▶ model call, schema-constrained, no filesystem
   codeagent      ────────▶ coding session, write-scoped
   tool     ──┐
   derive   ──┴───────────▶ a command:  shell → subprocess
                                        python → import and call
   human    ──┐
   fanout   ──┴───────────▶ nobody — the scheduler handles these itself
```

Which is why kind lives on the node, not the worker: the plan says what sort of work this
is, and the runtime decides what can perform it.

This seam is what makes a non-deterministic system testable. Worker outputs are recorded
once and replayed, so gate evaluation, invalidation, retry budgets, rollback and stale-approval
detection are all covered by fast tests that never call a model. The mode is recorded on
every attempt — a bundle that could not tell a live result from a stub would not be evidence.

---

## 11. Reading a real run

```bash
orchestrator status  <run_id>            # node by node
orchestrator metrics <run_id>            # success rate, retries, MTTR, latency
orchestrator why     <artifact> <run_id> # trace an artifact to the decision behind it
```

`docs/observing-a-run.md` says where every node's output lands.

---

## 12. What this does not do yet

So the diagrams are not read as claims:

| Gap | Effect |
| --- | --- |
| A coding session records nothing until it ends | Mid-wave state is invisible. Fine for a demo, wrong for audit-grade observability |
| Input references resolve per **node**, not per artifact | Naming one output of a two-output node does not exclude the other |
| A checkpoint's "presents" list is declared but never rendered | You read the artifact from disk instead |
| Content hashes cover raw bytes | Re-serializing an unchanged artifact mints a version, which can stale an approval for no reason |
| Nothing has run past implementation live | Testing, docs, security, release readiness and the evidence bundle are unexercised end to end |
