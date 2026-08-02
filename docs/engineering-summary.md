# Final engineering summary

**Greenfield ran end to end and was accepted on 2026-08-02.** Figures below are from that
run, `610c782beb9a4ea6bc7c8d06444eb432`, and from the 492-test suite as committed.

---

## 1. What was built

An **agentic SDLC orchestrator**: a requirement goes in, a reviewable engineering outcome
comes out, and every step is gated, attributed and replayable. A URL shortener is the
workload it drives — the demo, not the deliverable.

| Layer | Size | Role |
| --- | --- | --- |
| Orchestration layer | ~7,900 lines | gates, policy, lineage, evidence, metrics, workers — the graded differentiator |
| Graph runtime | ~1,700 lines | plan model, loader, wave scheduler |
| Target (shortener) | 8 modules, 1,557 lines | the SDLC's workload — 86 tests, 93.64% coverage |
| Tests | 492 | all deterministic; no test calls a model |

Three plan files describe three scenarios. The engine is fixed; **a scenario is data**
(D16), and brownfield/ambiguous are deltas over greenfield rather than copies (D19).

---

## 2. Plan and rationale

The brief's §4.4 is a list of control-plane properties, so the design starts from them and
every decision traces back to one:

| Requirement | How it is met | Decision |
| --- | --- | --- |
| Explicit dependency graph, entry/exit gates | YAML plan, six node kinds, gate per node | D16 |
| Non-linear, parallel with synchronization | Wave scheduler; **the join is the wave boundary**, not a node | D1 |
| Dynamic re-planning on upstream change | Re-derived artifact ⇒ descendants STALE, bound approvals revert | D10 |
| Cross-stage context and decision lineage | Artifacts versioned per run, `artifact_inputs` edges, `orchestrator why` | D8 |
| Human approval checkpoints | A human is a **node**, not a status; decision text becomes material | D13 |
| Failure controls | Bounded retries → fix node → escalate / safe-stop / rollback | D14 |
| Policy guardrails | Write ceiling, per-node scope, frozen paths, no agent waiver | D6, D7, D15 |
| Audit-grade observability, reliability metrics | Attempts, gate records, evidence bundle, `orchestrator metrics` | D9 |

**The load-bearing idea is that a producer never judges its own work** (D4). A worker returns
*facts and artifacts, never a verdict*; the engine runs the checks the plan names; a gate
reads those facts. An agent's self-report is inadmissible as evidence, by type.

**The second is three verdicts, not two.** PASS / FAIL / **ERROR** — "the check could not be
performed" is neither of the others. Folding ERROR into PASS makes governance decorative;
folding it into FAIL makes the repair loop retry a harness fault forever. This distinction
earned itself repeatedly in live running (§5).

---

## 3. Artifacts produced

| Artifact | What it is |
| --- | --- |
| `intake.register` v1→v3 | 5 requirements, 20 acceptance criteria, 13 ambiguities — 9 self-disposed as assumptions, 4 escalated to a human, all 13 disposed by v3 |
| `design.spec` v1→v4 | v1 **rejected** at the checkpoint; v4 is 8 modules, 33 elements, 6 endpoints, 48 exports, acyclic |
| Stub packages | 24 functions, every body `raise NotImplementedError`, verified by AST |
| `tests-acceptance.suite` | 86 tests over 20 criteria, authored **before** implementation, RED gate held |
| `*.changeset` | per node: files written, writes denied, scope declared |
| Evidence bundle | assembled and **RELEASABLE** — every gate held, every approval current |

Every artifact is on disk under `runs/<id>/artifacts/<name>/vN`, readable with `cat`, with
its producing attempt recorded in SQLite.

---

## 4. Risks, trade-offs and validation

### Accepted trade-offs

| Choice | Cost accepted |
| --- | --- |
| Hand-rolled graph runtime (D1) | We own the scheduler **and its bugs**. Two of the seven defects found in live running were durable-resume bugs — precisely what LangGraph's checkpointer or Temporal would have supplied. Documented rather than glossed |
| Architect writes stubs, not a generator (D25) | Two artifacts from one author can disagree, so an audit checks every promised name; a code agent could implement instead of declare, so every body is parsed. Rules kept, not states made impossible |
| Declarative plan DSL (D16) | Expressiveness ceiling; dynamic fan-out needed an explicit construct |
| Bash disallowed for code agents | A scope guard cannot parse a shell command, so agents cannot run their own tests. Verification is a separate node — which D4 wanted anyway |
| Scope guard detects, does not prevent | Prevention belongs to the runtime; this catches what the runtime cannot |

### Validation

- **465 deterministic tests.** The `Worker` seam (D18) makes a non-deterministic system
  testable: gate evaluation, invalidation cascades, retry budgets and stale-approval
  detection are all covered without a model call.
- **Architecture invariants are tested**, not asserted: `orchestrator` never imports the
  target; generated schemas match their models; the appendix cannot drift from the plan.
- **Live running is the real validation**, and it is where everything below came from.

---

## 5. What live running actually found

Seven defects, none visible to the test suite, all from first contact with a real run.
This is the most useful evidence in the submission and is not presented as embarrassment:

| Defect | Layer | Consequence if unfound |
| --- | --- | --- |
| Fan-out reported PASS having done nothing on resume | runtime | **A node that did no work recording success**, and every gate downstream reasoning from it |
| STALE nodes never re-entered the graph | runtime | Re-planning was a one-way door onto a wedged run |
| `invalidate` did not cascade to consumers | governance | A run completing around a change nobody applied |
| Cascade missed blocked and failed nodes | governance | Wedged run; a checkpoint waiting on a superseded question |
| No way to re-gate without re-doing work | governance | A 12-minute agent session repeated because a plan omitted a param |
| Preflight ignored check params | validation | Three gate checks ERRORing after the expensive part |
| Gate linted the whole target | plan | The architect failed on a suite it is frozen out of |

**The pattern is worth stating plainly:** the unit tests covered the *happy* graph. Every
defect was in re-entry — resume, invalidate, re-check — which is the part of an orchestrator
that only exercises when something has already gone wrong.

Two of them are direct evidence for what D1 traded away. The correct production answer for
durable execution is Temporal; it was rejected on time budget, and these are the bill.

---

## 6. Assumptions

Recorded rather than resolved silently — the register carries all 13 with dispositions:

| # | Assumption | Consequence accepted |
| --- | --- | --- |
| A1 | **301 permanent redirects** (human decision) | Cached redirects never reach the service, so click counts under-report. Chosen knowingly, recorded in the design |
| A2 | **No authentication or multi-tenancy** | `DELETE` is open; anyone can read anyone's analytics. The single largest open question |
| A5 | No deduplication by long URL | Two campaigns never share a click count |
| A7 | Soft delete; codes never reissued | Analytics survive deletion |
| A8 | No IP storage, no geolocation | Analytics are referrer + user-agent only |
| A12 | No malware/phishing screening | Creation rejects only self-referential hosts |

---

## 7. Limitations

**Observability.** Fixed during this work, and the fix is instructive: the whole run used to
be one transaction, so nothing was visible until the process exited. A wave is now the commit
boundary, nodes report RUNNING before dispatch, and `orchestrator watch` follows a live run
from another terminal. Most of the day's debugging cost was paid to *not* having this.

What remains: a code-agent session still records nothing until it ends, so a wave in flight
shows which node is running but not what it is doing.

**Coverage of the graph.** Greenfield is proven end to end: 20 nodes, 63 attempts, 11
incidents all recovered, 0 unrecovered. The ambiguous scenario is proven through
requirements — 15 ambiguities surfaced from one sentence, 5 escalated, a human checkpoint
taken. **Brownfield has never been executed**: its plan is written and was audited against
the current engine (two real defects found and fixed — see §5), but no run exists, and a
plan that has not run is a design, not a result.

**Scale.** SQLite, single node, whole-tree rollback. Fine for a prototype, wrong for a fleet.

**Fixtures drift.** The replay worker records real sessions; recordings age as models change.

---

## 8. If this continued

1. **Per-wave commits and turn-level streaming** — make a run watchable. Every problem above
   was harder to diagnose because a run is a black box while it executes.
2. **Durable execution underneath** (Temporal or equivalent), keeping the governance layer.
   Two of seven defects disappear by construction.
3. **Type-checking gate.** Types are in the contract and the stubs; nothing enforces them.
4. **Content-addressed artifacts**, enabling per-node rollback instead of whole-tree.
